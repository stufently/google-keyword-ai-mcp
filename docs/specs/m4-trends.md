# Спека: Google Trends (M4)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `4 из 9` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M3 приняты: каркас, HTTP-слой с ретраями, троттлер, кеш, нормализация,
провайдер Autocomplete и веерное расширение. 99 зелёных тестов. **Читай этот
код и опирайся на его типы, не переписывая их.**

Эта веха добавляет второй источник — Google Trends. Он даёт то, чего нет у
автокомплита: динамику интереса во времени, географию и растущие запросы.

**Чего в этой вехе НЕТ:** Google Ads (M5), Search Console (M6), пайплайн
`research` и runs (M7), scoring и кластеризация (M8). Не реализовывай их.

## Проверенные факты

Всё проверено вживую 2026-09-02 запросами с хоста; ответы сохранены в
`tests/fixtures/trends/` и являются твоими golden-фикстурами.

### Официального API нет

Официальный Google Trends API на 2026-09 — закрытая alpha, доступа нет.
Библиотеки-обёртки мертвы: `pytrends` заархивирован 17.04.2025, `trendspy` без
коммитов с 25.12.2024. Тянуть их нельзя, и новых зависимостей добавлять нельзя.
Клиент пишем свой на уже имеющемся `httpx`.

### Рабочая цепочка из трёх шагов

1. **Прогрев сессии.** `GET https://trends.google.com/_/TrendsUi/data/batchexecute`
   отвечает **405** и ставит cookie `NID`. Без этой cookie первый же
   содержательный запрос получает 429. Ответ 405 — ожидаемый и НЕ является
   ошибкой.
2. **Виджеты.** `GET https://trends.google.com/trends/api/explore` с параметрами
   `hl`, `tz`, `req`, где `req` — JSON вида

   ```json
   {"comparisonItem":[{"keyword":"недвижимость","geo":"RU","time":"today 12-m"}],
    "category":0,"property":""}
   ```

   Возвращает `{"widgets": [...]}`. У каждого виджета есть `id`, `token`,
   `request`, `type`. Наблюдались id: `TIMESERIES`, `GEO_MAP`,
   `RELATED_TOPICS`, `RELATED_QUERIES` — все четыре присутствуют даже когда
   данных нет.
3. **Данные виджета.** `GET https://trends.google.com/trends/api/widgetdata/<путь>`
   с параметрами `hl`, `tz`, `req` (сериализованный `widget["request"]`) и
   `token` (`widget["token"]`). Пути: `multiline` для `TIMESERIES`,
   `comparedgeo` для `GEO_MAP`, `relatedsearches` для `RELATED_QUERIES` и
   `RELATED_TOPICS`.

Заголовки обязательны: `User-Agent`, `accept-language`, `referer`
(`https://trends.google.com/trends/explore`). Между вызовами виджетов
выдерживать паузу порядка 800 мс.

### 🚨 Префикс ответа РАЗНЫЙ у разных эндпоинтов

Проверено побайтово на сохранённых фикстурах:

| Эндпоинт | Префикс перед JSON |
|---|---|
| `/trends/api/explore` | `)]}'` + перевод строки |
| `/trends/api/widgetdata/*` | `)]}',` + перевод строки |

То есть срез фиксированной длины (`payload[5:]`) на обоих проходит только по
случайности: у `explore` он снимает ровно префикс, у `widgetdata` оставляет
перевод строки, который `json.loads` терпит. **Так делать нельзя.** Снимать
префикс надо честно: убрать ведущие пробелы, снять префикс, если строка
начинается с `)]}'`, отбросить всё до первого `{`, и только потом разбирать.

### Формы данных (из фикстур)

`multiline` → `{"default": {"timelineData": [...], "averages": [...]}}`. Точка:

```json
{"time":"1756598400","formattedTime":"31 авг. – 6 сент. 2025 г.",
 "formattedAxisTime":"31 авг. 2025 г.","value":[98],"hasData":[true],
 "formattedValue":["98"]}
```

`comparedgeo` → `{"default": {"geoMapData": [...]}}`. Строка:

```json
{"geoCode":"RU-SAK","geoName":"Сахалинская область","value":[100],
 "formattedValue":["100"],"maxValueIndex":0,"hasData":[true]}
```

`relatedsearches` → `{"default": {"rankedList": [{"rankedKeyword": [...]}, {...}]}}`.
**Список ровно из двух элементов: индекс 0 — TOP, индекс 1 — RISING.**

```json
top:    {"query":"авито недвижимость","value":100,"formattedValue":"100",
         "hasData":true,"link":"/trends/explore?..."}
rising: {"query":"лучшие микрозаймы онлайн","value":9250,
         "formattedValue":"Сверхпопулярность","link":"/trends/explore?..."}
```

⚠️ У rising-элемента **нет ключа `hasData`** — модель обязана допускать его
отсутствие. `formattedValue` у rising может быть словом («Сверхпопулярность»,
в англоязычной локали «Breakout»), а не числом.

### 🚨 Пустой ответ — это норма, а не ошибка

Фикстуры `*_sparse.json` сняты для реального низкочастотного запроса
(«аренда квартиры паттайя», geo TH) и выглядят так:

```json
multiline:        {"default":{"timelineData":[],"averages":[]}}
relatedsearches:  {"default":{"rankedList":[{"rankedKeyword":[]},{"rankedKeyword":[]}]}}
```

При этом `explore` вернул все четыре виджета, а `comparedgeo` — 77 строк.
Парсер обязан это переваривать и отдавать пустой результат, а не падать.

### 🚨 Значения 0–100 нормализуются ВНУТРИ одного запроса

В каждом ответе за 100 принят максимум этого ответа. Числа из двух разных
запросов Trends несравнимы. Поэтому у любого набора трендовых данных есть
`normalization_scope` — идентификатор запроса, в рамках которого числа
сопоставимы. Сравнивать несколько ключей можно ТОЛЬКО запросив их одним
запросом (несколько элементов в `comparisonItem`).

### Что уже есть в дереве

- `build_client(settings, *, accept_language=None)` в `http.py` и
  `request_with_retries(...)` с ретраями, backoff и уважением `Retry-After`.
- `AsyncRateLimiter(rate_per_second)` в `ratelimit.py`.
- `SqliteCache` и `build_cache_key(provider, endpoint, params, account_scope, parser_version)`
  в `cache.py`.
- `Provider` и `ProviderInfo` в `providers/base.py`.
- `Envelope[T]`, `Completeness`; конверт с `completeness != complete` требует
  `completeness_reason`.
- `Market.trends_geo()` возвращает alpha-2 (`"RU"`).
- Инструменты MCP регистрируются **синхронными** функциями.
- Сети в песочнице нет: все тесты — на `respx` и фикстурах.

## Что делать

### 1. Расширить `src/google_keyword_ai/config.py`

Добавить поля (существующие не трогать):

- `trends_enabled: bool = True` — kill switch неофициального адаптера;
- `trends_pacing_seconds: float = 0.8`;
- `trends_cache_ttl_seconds: int = 21600`;
- `trends_circuit_breaker_failures: int = 3`;
- `trends_timezone_minutes: int = -180`.

Валидация: пауза и TTL положительные, порог размыкателя `>= 1`, иначе
`InvalidConfigurationError`.

### 2. `src/google_keyword_ai/providers/trends/__init__.py`

Пустой.

### 3. `src/google_keyword_ai/providers/trends/models.py`

Pydantic-модели. Все необязательные узлы — с умолчаниями, чтобы пустой ответ
не ронял разбор.

- `class TrendPoint`: `timestamp: datetime` (из строкового unix-времени),
  `formatted_time: str`, `values: list[int]`, `has_data: list[bool] = []`;
- `class GeoInterest`: `geo_code: str`, `geo_name: str`, `values: list[int]`,
  `has_data: list[bool] = []`;
- `class RelatedQuery`: `query: str`, `value: int`,
  `formatted_value: str`, `has_data: bool | None = None`;
- `class RelatedQueries`: `top: list[RelatedQuery] = []`,
  `rising: list[RelatedQuery] = []`;
- `class TrendsResult`: `keywords: list[str]`, `geo: str`, `timeframe: str`,
  `normalization_scope: str`, `timeline: list[TrendPoint] = []`,
  `geo_interest: list[GeoInterest] = []`,
  `related: RelatedQueries = RelatedQueries()`,
  `retrieved_at: datetime`, `source: str`.

`normalization_scope` — стабильный хеш от `(keywords, geo, timeframe, hl)`:
одинаковые запросы дают одинаковый scope, разные — разный. Использовать уже
существующий `build_cache_key` нельзя (у него другая роль); посчитать sha256 от
канонического JSON тех же полей и взять первые 16 hex-символов.

### 4. `src/google_keyword_ai/providers/trends/unofficial.py`

- `WARMUP_URL = "https://trends.google.com/_/TrendsUi/data/batchexecute"`;
- `EXPLORE_URL = "https://trends.google.com/trends/api/explore"`;
- `WIDGETDATA_URL = "https://trends.google.com/trends/api/widgetdata"`;
- `REFERER = "https://trends.google.com/trends/explore"`;
- `WIDGET_PATHS = {"TIMESERIES": "multiline", "GEO_MAP": "comparedgeo",
  "RELATED_QUERIES": "relatedsearches", "RELATED_TOPICS": "relatedsearches"}`;
- `def strip_prefix(payload: str) -> str` — снимает ведущие пробелы, префикс
  `)]}'` в любом виде (с запятой и без) и всё до первого `{`; если `{` нет →
  `ApiError`. Именно эта функция закрывает разницу префиксов между
  эндпоинтами;
- `def parse_explore(payload: str) -> dict[str, dict[str, object]]` — виджеты
  по `id`; отсутствие `widgets` → `ApiError`;
- `def parse_timeline(payload)`, `parse_geo(payload)`,
  `parse_related(payload)` — разбор трёх форм из «Проверенных фактов».
  Отсутствие `default` → `ApiError`; отсутствие вложенных списков → пустой
  результат, НЕ ошибка. `rankedList` короче двух элементов — тоже не ошибка:
  чего нет, то пусто;
- `class UnofficialTrendsClient`:
  - конструктор от `Settings`, `httpx.AsyncClient`, `AsyncRateLimiter`;
  - `async def warm_up(self) -> None` — GET на `WARMUP_URL`; **405 считается
    успехом**; выполняется один раз на экземпляр;
  - `async def fetch(self, keywords: Sequence[str], *, geo: str, timeframe: str, hl: str) -> TrendsResult`
    — прогрев → explore → виджеты по очереди с паузой
    `trends_pacing_seconds` между ними;
  - **размыкатель:** считать подряд идущие неудачи; после
    `trends_circuit_breaker_failures` экземпляр помечается разомкнутым и все
    последующие вызовы немедленно поднимают `ProviderUnavailableError`, не
    трогая сеть. Успешный вызов сбрасывает счётчик.

Запросы к виджетам, которых нет в ответе `explore`, не выполнять. Отказ
отдельного виджета не убивает весь результат: соответствующая часть остаётся
пустой, а причина попадает в предупреждения (см. use-case). Отказ `explore`
или прогрева — это отказ всего запроса.

Никакого обхода защиты: ни прокси, ни подмены отпечатков, ни CAPTCHA.

### 5. `src/google_keyword_ai/providers/trends/official.py`

- `class OfficialTrendsAdapter` с `is_available(self) -> bool`, который
  **всегда возвращает `False`** с внятным пояснением: официальный API
  находится в закрытой alpha и доступа нет. Метод `fetch` поднимает
  `ProviderUnavailableError`.

Симулировать официальный API запрещено. Класс существует, чтобы место под него
было готово.

### 6. `src/google_keyword_ai/providers/trends/provider.py`

- `class GoogleTrendsProvider(Provider)`:
  - `info` → `name="trends"`, `official=False`, `stability="unofficial"`;
  - `is_available()` → `False`, если `settings.trends_enabled` выключен;
    иначе `True`;
  - `async def fetch(...)` — выбирает адаптер: официальный, если он доступен,
    иначе неофициальный; при выключенном kill switch поднимает
    `ProviderUnavailableError`;
  - результат кешируется через `SqliteCache` с TTL
    `trends_cache_ttl_seconds`, `account_scope=""`.

### 7. `src/google_keyword_ai/usecases/trends.py`

- `class TrendsData` (pydantic): `provider: ProviderInfo`, `result: TrendsResult`;
- `def run_trends(settings, keyword, *, language=None, country=None, timeframe="today 12-m") -> Envelope[TrendsData]`;
- `def run_trends_compare(settings, keywords: Sequence[str], *, language=None, country=None, timeframe="today 12-m") -> Envelope[TrendsData]`
  — все ключи уходят ОДНИМ запросом, иначе значения были бы несравнимы; пустой
  список или больше пяти ключей → `InvalidConfigurationError`.

Оба фасада синхронные, работают через `anyio.run`, и при
`RateLimitError`, `NetworkError`, `ApiError`, `ProviderUnavailableError`
возвращают конверт с `completeness=EMPTY`, заполненными `errors` и
`completeness_reason`, не выпуская исключение. Частично собранный результат
(например, есть таймсерия, но упал виджет географии) → `completeness=PARTIAL`,
причина и подробности в `warnings`. Пустая таймсерия без ошибок → `EMPTY` с
причиной `"no trend data"`.

### 8. Обёртки

- `cli/main.py`: `gkai trends <keyword> [--language] [--country] [--timeframe] [--format]`
  и `gkai trends compare <k1> <k2> ... [те же опции]`. Существующие команды не
  менять.
- `mcp/server.py`: **синхронный** инструмент
  `analyze_trends(keywords: list[str], language=None, country=None, timeframe="today 12-m") -> Envelope[TrendsData]`
  — один ключ или несколько, всегда одним запросом. Существующие инструменты
  не менять.
- `usecases/doctor.py`: у провайдера `trends` статус `available` брать из
  `GoogleTrendsProvider.is_available()`, `detail` — `"ready (unofficial)"` при
  включённом и `"disabled by configuration"` при выключенном kill switch.

### 9. `docs/trends.md`

Не длиннее 70 строк: цепочка из трёх шагов, разница префиксов, почему 405 при
прогреве — это успех, что 0–100 нормализуются внутри запроса и сравнивать
можно только ключи из одного запроса, что такое `normalization_scope`, как
выключить провайдер kill switch'ем, зачем размыкатель, и что официальный API
недоступен.

### 10. Тесты

Использовать сохранённые фикстуры из `tests/fixtures/trends/`, а не выдуманные
строки.

- `tests/test_trends_parsing.py` — `strip_prefix` снимает ОБА префикса
  (`)]}'` и `)]}',`) и падает `ApiError` на мусоре; разбор
  `explore_popular.json` даёт четыре виджета; `multiline_popular.json` даёт 53
  точки; `comparedgeo_popular.json` — 83 строки; `relatedsearches_popular.json`
  — 25 top и 15 rising, причём у rising-элемента `has_data is None`;
  **все `*_sparse.json` разбираются без исключения и дают пустые списки**.
- `tests/test_trends_client.py` (на `respx`) — прогрев с ответом 405 считается
  успехом; пауза между виджетами выдерживается (подменять `anyio.sleep`);
  виджет, которого нет в `explore`, не запрашивается; отказ одного виджета
  оставляет остальные данные; размыкатель после N подряд неудач перестаёт
  ходить в сеть; повторный запрос берётся из кеша.
- `tests/test_trends_usecase.py` — `EMPTY` при отказе провайдера без выброса
  исключения; `PARTIAL` при частичном результате с предупреждением; `EMPTY`
  при пустой таймсерии; `normalization_scope` одинаков для одинаковых запросов
  и различен для разных; `run_trends_compare` шлёт ОДИН запрос со всеми
  ключами; kill switch выключает провайдера.
- Дописать в `tests/test_cli.py` проверки `gkai trends` и `gkai trends compare`
  на моках, в `tests/test_mcp_parity.py` — parity для `analyze_trends`
  (использовать фикстуру `thread_offload`), в `tests/test_cli.py` — что
  `doctor` показывает `trends` выключенным при `GKAI_TRENDS_ENABLED=false`.

Тесты не должны ходить в сеть ни при каких условиях.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** — ни
  `pytrends`, ни `trendspy`, ни чего-либо ещё. Не хватает библиотеки —
  остановись и доложи.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/trends/**` — боевые ответы, снятые хозяином. **Читать, не
  изменять и не дописывать.** Нужна форма, которой в них нет, — остановись и
  доложи, а не сочиняй фикстуру.
- `docs/superpowers/`, `docs/specs/m1-scaffold.md`, `docs/specs/m2-autocomplete.md`,
  `docs/specs/m3-expand.md`, `docs/autocomplete.md`, `docs/expansion.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- Код вех M1–M3 менять НЕ нужно. `cli/main.py`, `mcp/server.py`,
  `usecases/doctor.py` и `config.py` правятся только так, как описано в «Что
  делать». Существующие 99 тестов должны остаться зелёными без правок: если
  приходится править существующий тест — контракт принятой вехи сломан,
  остановись и доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (google_ads, search_console,
  pipeline, scoring, clustering).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/**`, `tests/**` (кроме
`tests/fixtures/**`) и `docs/trends.md`.

**Если разрешённого способа не остаётся** — остановись и доложи по разделу
«Контракт на невыполнимое». Создавать доказательства во временных каталогах
запрещено.

## Критерии приёмки

- **AC-001.** Линтер чист.
  Проверка: `.venv/bin/ruff check .`
- **AC-002.** Проверка типов в строгом режиме проходит.
  Проверка: `.venv/bin/mypy`
- **AC-003.** Весь тестовый корпус зелёный, включая 99 тестов вех M1–M3.
  **Критерий агрегирующий: при его провале остальные критерии всё равно
  выполнить и отчитаться по каждому.**
  Проверка: `.venv/bin/pytest -q`
- **AC-004.** `strip_prefix` снимает оба реальных префикса — и `)]}'`, и
  `)]}',` — и отвергает мусор.
  Проверка: `.venv/bin/pytest -q tests/test_trends_parsing.py -k prefix`
- **AC-005.** Разбор боевых фикстур даёт ожидаемые объёмы: 4 виджета, 53 точки,
  83 гео-строки, 25 top и 15 rising, у rising `has_data is None`.
  Проверка: `.venv/bin/pytest -q tests/test_trends_parsing.py -k popular`
- **AC-006.** Все разреженные фикстуры разбираются без исключения и дают
  пустые списки.
  Проверка: `.venv/bin/pytest -q tests/test_trends_parsing.py -k sparse`
- **AC-007.** Прогрев с ответом 405 считается успехом, а не ошибкой.
  Проверка: `.venv/bin/pytest -q tests/test_trends_client.py -k warmup`
- **AC-008.** Размыкатель после порога подряд идущих неудач перестаёт ходить в
  сеть и поднимает `ProviderUnavailableError`.
  Проверка: `.venv/bin/pytest -q tests/test_trends_client.py -k circuit`
- **AC-009.** Отказ одного виджета не уничтожает остальные данные, а
  отсутствующий в `explore` виджет не запрашивается.
  Проверка: `.venv/bin/pytest -q tests/test_trends_client.py -k widget`
- **AC-010.** `normalization_scope` совпадает у одинаковых запросов и
  различается у разных, а `run_trends_compare` отправляет все ключи одним
  запросом.
  Проверка: `.venv/bin/pytest -q tests/test_trends_usecase.py -k "scope or compare"`
- **AC-011.** Kill switch выключает провайдер, отказ даёт `empty` без выброса
  исключения, частичный результат — `partial` с предупреждением.
  Проверка: `.venv/bin/pytest -q tests/test_trends_usecase.py -k "kill or empty or partial"`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/specs/m3-expand.md docs/autocomplete.md docs/expansion.md tests/fixtures src/google_keyword_ai/market.py src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/storage src/google_keyword_ai/http.py src/google_keyword_ai/cache.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/normalize.py src/google_keyword_ai/expansion.py src/google_keyword_ai/providers/autocomplete.py src/google_keyword_ai/providers/expander.py`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий; блокирующим не считать.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают** —
>    инструменты синхронные, SDK уводит их в поток, а пробуждение event loop из
>    чужого потока песочница не пропускает. Если `pytest -q` или тесты parity
>    зависнут, останови их, отчитайся `not_attempted` с этой причиной и
>    продолжай; остальные критерии выполни. Делать инструмент асинхронным
>    ЗАПРЕЩЕНО: в бою это заблокирует event loop.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Живой запрос к Google Trends: полная цепочка прогрев → explore →
  виджеты, сверить разбор с боевым ответом, а не только с фикстурой.
- **HC-003.** Мутационная проверка: по очереди сломать снятие префикса, приём
  405 как успеха, размыкатель, паузу между виджетами, чтение из кеша,
  различение top/rising, `normalization_scope` и отправку ключей одним
  запросом.
- **HC-004.** Сборка wheel и живой `gkai trends` из установленного пакета.
- **HC-005.** Живой вызов `analyze_trends` по настоящему stdio.

## Контракт на невыполнимое

Если два требования противоречат друг другу, два пина несовместимы или
требование невыполнимо в этом окружении — **ОСТАНОВИСЬ и доложи** в поле
`blocked_reason` финального отчёта, приложив доказательство (вывод команды,
текст ошибки резолвера). Обходить объявленную несовместимость запрещено:
`--no-deps`, `--force-reinstall`, вендоринг, подмена тулчейна, отключение
проверок, игнор constraints — всё это считается провалом задачи, а не
решением. Остановка с внятной причиной — правильный исход, а не поражение.

Если одна и та же команда упала дважды с той же ошибкой — не запускай её в
третий раз, не изменив подхода. Если подхода нет — остановись и доложи.

## Разрешения

- Создавать и править файлы в: `src/google_keyword_ai/**`, `tests/**` (кроме
  `tests/fixtures/**`), `docs/trends.md`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет. Все тесты — на `respx` и фикстурах.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/`, `.toolchain/` и
  `tests/fixtures/**`, обращаться к настоящим эндпоинтам Google, писать файлы
  во временные каталоги в качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
