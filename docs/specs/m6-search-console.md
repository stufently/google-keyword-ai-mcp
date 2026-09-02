# Спека: Google Search Console (M6)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `6 из 9` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M5 приняты: каркас, HTTP-слой, кеш, нормализация, Autocomplete, веерное
расширение, Google Trends, Google Ads. 178 зелёных тестов. **Читай существующий
код и опирайся на его типы, не переписывая их.**

Эта веха добавляет последний источник данных — Search Console. В отличие от
остальных он показывает не прогноз и не относительный интерес, а **реальные
показы, клики, CTR и позиции** уже существующего сайта.

**Чего в этой вехе НЕТ:** пайплайн `research` и runs (M7), scoring и
кластеризация (M8), Claude Skill и README (M9). Не реализовывай их.

## Проверенные факты

Снято 2026-09-02 с установленных в дереве библиотек и их статических
discovery-документов. По памяти ничего не переписывать.

### API

`google-api-python-client` 2.200.0 везёт статический discovery-документ
`searchconsole.v1.json`, **revision 20260805**. Значит клиент строится
**без обращения в сеть**:

```python
build("searchconsole", "v1", credentials=..., static_discovery=True)
```

Параметр `static_discovery` у `googleapiclient.discovery.build` существует —
проверено интроспекцией сигнатуры.

Ресурсы документа: `searchanalytics`, `sitemaps`, `sites`, `urlInspection`,
`urlTestingTools`. Нужны только первые два из них: `sites` и `searchanalytics`.

- `sites().list()` → объект с `siteEntry`, каждый элемент — `WmxSite` с полями
  ровно `siteUrl` и `permissionLevel`;
- `searchanalytics().query(siteUrl=..., body=...)`, HTTP POST по пути
  `webmasters/v3/sites/{siteUrl}/searchAnalytics/query`.

Scopes: `https://www.googleapis.com/auth/webmasters` и
`.../webmasters.readonly`. Нам достаточно **readonly**.

### Поля запроса `SearchAnalyticsQueryRequest`

```
startDate (обязателен), endDate (обязателен), dimensions,
dimensionFilterGroups, aggregationType, rowLimit, startRow, type, dataState
```

🚨 Дословно из документа: **`rowLimit` — «Must be a number from 1 to 25,000
(inclusive)», по умолчанию 1000**; `startRow` — «Zero-based index of the first
row», по умолчанию 0. Это и есть механизм постраничной выборки.

`type` пришёл на смену `searchType`: «Type of report: search type, or either
Discover or Gnews», по умолчанию `web`. Использовать надо `type`.

`dataState` — `full` либо `all` (второе включает неполные свежие данные).

### Форма строки ответа

`ApiDataRow` имеет ровно четыре метрики и массив ключей:

```
{"keys": ["...", "..."], "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
```

🚨 **`keys` — это массив строк, позиционно соответствующий `dimensions`
запроса.** Имён измерений в ответе нет: если запрошено
`dimensions=["query","page"]`, то `keys[0]` — запрос, `keys[1]` — страница.
Разбор обязан опираться на порядок запрошенных измерений, а не угадывать.

### Лимит суточной выдачи

Google ограничивает выгрузку через API примерно **50 000 строк в сутки на
property и тип поиска**. Это **верхняя граница выдачи, а не гарантия
полноты**: упершись в неё, ответ обязан нести флаг усечения, а не выглядеть
полным.

### Авторизация без интерактивного входа

В дереве есть `google-auth` и `google-auth-oauthlib`. Оба нужных загрузчика
работают из файла, без браузера — проверено интроспекцией:

- `google.oauth2.service_account.Credentials.from_service_account_file(filename, **kwargs)`
  — для JSON сервисного аккаунта (`"type": "service_account"`);
- `google.oauth2.credentials.Credentials.from_authorized_user_file(filename, scopes=None)`
  — для JSON авторизованного пользователя (`"type": "authorized_user"`).

Интерактивный OAuth-флоу в этой вехе НЕ реализуется: он требует браузера,
которого нет ни в песочнице, ни на сервере. Поддерживаем ровно два файла выше и
описываем в документации, как их получить.

### Что уже есть в дереве

- `Settings.search_console_credentials_path: Path | None` уже объявлено.
- `Market.gsc_country()` уже возвращает alpha-3 в нижнем регистре (`"rus"`).
- `SqliteCache`, `build_cache_key`, `AsyncRateLimiter`,
  `InterProcessRateLimiter`, `Provider`, `ProviderInfo`, `Envelope`,
  `Completeness`, таксономия ошибок.
- Провайдер `google_ads` показывает образец: блокирующий клиент вызывается
  через `anyio.to_thread.run_sync`, сервис подставляется тестам через
  `service_factory`. **Сделай так же**: `googleapiclient` синхронный.
- Инструменты MCP регистрируются **синхронными** функциями.
- Сети в песочнице нет, кредов Search Console нет ни у кого. **Все тесты — на
  подделке сервиса.**

## Что делать

### 1. Расширить `src/google_keyword_ai/config.py`

- `search_console_row_limit: int = 25000` — валидация `1..25000`
  включительно, иначе `InvalidConfigurationError`;
- `search_console_daily_row_cap: int = 50000` — порог, при достижении которого
  выдача считается усечённой;
- `search_console_cache_ttl_seconds: int = 21600`;
- `search_console_rate_limit_per_second: float = 5.0`;
- `gsc_opportunity_min_impressions: int = 100`;
- `gsc_opportunity_min_position: float = 5.0`;
- `gsc_opportunity_max_position: float = 30.0`;
- `gsc_opportunity_max_ctr: float = 0.02`.

Валидация: положительные значения; `gsc_opportunity_min_position <
gsc_opportunity_max_position`; `0 < gsc_opportunity_max_ctr <= 1`.

🚨 Магических чисел в бизнес-логике быть не должно: пороги берутся ТОЛЬКО из
настроек.

### 2. `src/google_keyword_ai/providers/search_console.py`

Модели:

- `class SearchAnalyticsRow` (pydantic): `keys: dict[str, str]`,
  `clicks: int`, `impressions: int`, `ctr: float`, `position: float`.
  `keys` — словарь «измерение → значение», собранный сопоставлением
  `dimensions` запроса и массива `keys` ответа;
- `class SearchAnalyticsPage` (pydantic): `rows: list[SearchAnalyticsRow]`,
  `truncated: bool`, `truncation_reason: str | None`;
- `class SiteProperty` (pydantic): `site_url: str`, `permission_level: str`.

`class SearchConsoleProvider(Provider)`:

- конструктор принимает `settings`, `cache`, `rate_limiter` и
  **`service_factory: Callable[[], object] | None = None`** — как у
  `GoogleAdsProvider`, чтобы тесты подставляли подделку;
- `info` → `name="search_console"`, `official=True`, `stability="stable"`;
- `is_available()` → `True`, только если `search_console_credentials_path`
  задан И файл существует;
- `def load_credentials(self)` — читает JSON, смотрит поле `type`:
  `service_account` → `from_service_account_file`, `authorized_user` →
  `from_authorized_user_file`, иное значение или отсутствие поля →
  `InvalidConfigurationError` с внятным текстом. Scope — readonly;
- `def build_service(self)` — `build("searchconsole", "v1", credentials=...,
  static_discovery=True, cache_discovery=False)`; если задана фабрика —
  используется она;
- `async def list_properties(self) -> list[SiteProperty]`;
- `async def query(self, site_url, *, start_date, end_date, dimensions, market=None, search_type="web", data_state="full", row_limit=None, dimension_filters=None) -> SearchAnalyticsPage`.

Метод `query` обязан:

1. проверять `is_available()`, иначе `ProviderUnavailableError`;
2. читать кеш ДО сети; ключ обязан включать **`account_scope` = `site_url`** —
   иначе владелец двух сайтов получит чужие данные;
3. **разбивать диапазон по дням**: для каждого дня отдельный запрос с
   `startDate == endDate == этот день`. Без этого крупный сайт не выбрать:
   лимит в 25 000 строк действует на запрос;
4. внутри дня выбирать страницами по `row_limit` (не больше 25 000) через
   `startRow`, пока не придёт неполная страница;
5. брать троттлер перед каждым запросом;
6. **вызывать блокирующий клиент через `anyio.to_thread.run_sync`**;
7. **выставлять `truncated=True`** и заполнять `truncation_reason`, если
   суммарно набрано `>= search_console_daily_row_cap` строк, и в этом случае
   прекращать выборку. Молча обрезать запрещено;
8. складывать результат в кеш.

Ошибки библиотеки (`googleapiclient.errors.HttpError`) переводить в нашу
таксономию по коду ответа: 401/403 → `AuthenticationError`, 429 →
`RateLimitError`, 5xx → `ApiError`, прочее → `ApiError`. Ловить именно
`HttpError`, а не `Exception`.

### 3. `src/google_keyword_ai/opportunities.py`

Чистая функция без сети — чтобы её можно было полностью проверить тестами.

- `class Opportunity` (pydantic): `query: str`, `page: str | None`,
  `clicks: int`, `impressions: int`, `ctr: float`, `position: float`,
  `kind: str`, `reason: str`;
- `def find_opportunities(rows: Sequence[SearchAnalyticsRow], settings: Settings) -> list[Opportunity]`.

Правила отбора, все пороги — из настроек:

- строка попадает в рассмотрение, если `impressions >= gsc_opportunity_min_impressions`
  и `gsc_opportunity_min_position <= position <= gsc_opportunity_max_position`;
- `kind="quick_win"`, если позиция в первой половине окна (то есть
  `position <= (min + max) / 2`) и `ctr <= gsc_opportunity_max_ctr`;
- `kind="content_expansion"` в остальных отобранных случаях;
- `reason` — человекочитаемое объяснение с фактическими числами;
- результат отсортирован по `impressions` по убыванию.

### 4. `src/google_keyword_ai/usecases/gsc.py`

- `class PropertiesData`: `provider: ProviderInfo`, `properties: list[SiteProperty]`;
- `class QueriesData`: `provider`, `site_url`, `start_date`, `end_date`,
  `dimensions: list[str]`, `rows: list[SearchAnalyticsRow]`, `truncated: bool`,
  `truncation_reason: str | None`;
- `class OpportunitiesData`: `provider`, `site_url`, `start_date`, `end_date`,
  `thresholds: dict[str, float]`, `opportunities: list[Opportunity]`,
  `truncated: bool`;
- `def run_gsc_properties(settings) -> Envelope[PropertiesData]`;
- `def run_gsc_queries(settings, site_url, *, days=28, start_date=None, end_date=None, dimensions=None, country=None, search_type="web", limit=None) -> Envelope[QueriesData]`
  — по умолчанию `dimensions=["query"]`, диапазон — последние `days` суток,
  **заканчивая позавчерашним днём**, потому что свежие сутки в Search Console
  ещё неполные;
- `def run_gsc_opportunities(settings, site_url, *, days=28, country=None, limit=None) -> Envelope[OpportunitiesData]`
  — запрашивает `dimensions=["query","page"]` и прогоняет через
  `find_opportunities`.

Все фасады синхронные, через `anyio.run`. При `ProviderUnavailableError`,
`AuthenticationError`, `RateLimitError`, `NetworkError`, `ApiError`,
`InvalidConfigurationError` — конверт с пустым результатом,
`completeness=EMPTY`, заполненными `errors` и `completeness_reason`, без
выброса исключения. Пустой ответ без ошибки → `EMPTY` с причиной
`"no search analytics data"`. **Усечённая выдача → `completeness=PARTIAL`** с
причиной и предупреждением.

### 5. Обёртки

- `cli/main.py`: `gkai gsc properties`, `gkai gsc queries <site_url>`,
  `gkai gsc opportunities <site_url>` с опциями `--days`, `--start-date`,
  `--end-date`, `--dimension` (повторяемый), `--country`, `--search-type`,
  `--limit`, `--format`. Существующие команды не менять.
- `mcp/server.py`: **синхронный** инструмент
  `find_gsc_opportunities(site_url, days=28, country=None, limit=None) -> Envelope[OpportunitiesData]`.
  Существующие инструменты не менять.
- `usecases/doctor.py`: у провайдера `search_console` брать `available` из
  `SearchConsoleProvider.is_available()`; `detail` — `"ready"`, если файл
  кредов задан и существует, `"missing credentials"`, если путь не задан, и
  `"credentials file not found: <путь>"`, если путь задан, а файла нет.

### 6. `docs/search-console.md`

Не длиннее 80 строк: какие два вида файлов кредов поддерживаются и как их
получить, почему интерактивного входа нет, что `rowLimit` максимум 25 000 и
поэтому выборка идёт постранично и по дням, что 50 000 строк в сутки — верхняя
граница выдачи, а не гарантия полноты, и как читать флаг усечения; что пороги
поиска возможностей настраиваются; что данные последних суток неполные и
поэтому окно заканчивается позавчерашним днём; упоминание, что для крупных
сайтов правильный следующий шаг — ежедневный bulk export в BigQuery (в этой
версии не реализован).

### 7. Тесты

- `tests/test_search_console_provider.py` (подделка сервиса) — `keys`
  сопоставляются с `dimensions` по порядку; диапазон разбивается по дням (три
  дня → три запроса); внутри дня работает постраничная выборка по `startRow`;
  достижение суточного порога ставит `truncated=True` и прекращает выборку;
  `row_limit` больше 25 000 отвергается; ключ кеша различает `site_url`;
  повторный вызов идёт из кеша; `HttpError` с кодами 403, 429 и 500
  переводится в `AuthenticationError`, `RateLimitError` и `ApiError`
  соответственно; блокирующий вызов уходит в поток (сравнить
  `threading.get_ident()`); без кредов — `ProviderUnavailableError`;
  `type` файла кредов не `service_account` и не `authorized_user` →
  `InvalidConfigurationError`.
- `tests/test_opportunities.py` — отбор по порогам из настроек; разделение
  `quick_win` и `content_expansion`; сортировка по показам; изменение порогов
  в настройках меняет результат (**магических чисел нет**); строка ниже порога
  показов и строка вне окна позиций отбрасываются.
- `tests/test_gsc_usecase.py` — конверт при успехе; `EMPTY` без кредов и при
  ошибке провайдера без выброса исключения; `PARTIAL` при усечении; окно по
  умолчанию заканчивается позавчерашним днём.
- Дописать в `tests/test_cli.py` проверки новых команд на подделке и в
  `tests/test_mcp_parity.py` — parity для `find_gsc_opportunities`
  (использовать фикстуру `thread_offload`), а также что `doctor` показывает
  `search_console` недоступным без кредов.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** —
  `google-api-python-client`, `google-auth`, `google-auth-oauthlib` уже
  установлены как extra. Не хватает библиотеки — остановись и доложи.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/**` — читать, не изменять.
- `docs/superpowers/`, все `docs/specs/m1..m5*.md`, `docs/autocomplete.md`,
  `docs/expansion.md`, `docs/trends.md`, `docs/google-ads.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- Код вех M1–M5 менять НЕ нужно, кроме перечисленного в «Что делать»
  (`config.py`, `cli/main.py`, `mcp/server.py`, `usecases/doctor.py`).
  Существующие 178 тестов должны остаться зелёными без правок — кроме теста,
  который сейчас утверждает, что `doctor` показывает `search_console` с
  `detail="missing credentials"`: его текст эта веха уточняет, и обновить его
  можно. Если приходится править ЛЮБОЙ другой существующий тест — остановись и
  доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (pipeline, runs, scoring,
  clustering, skill).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/**`, `tests/**` (кроме
`tests/fixtures/**`), `docs/search-console.md`.

**Если разрешённого способа не остаётся** — остановись и доложи по разделу
«Контракт на невыполнимое». Создавать доказательства во временных каталогах
запрещено.

## Критерии приёмки

- **AC-001.** Линтер чист.
  Проверка: `.venv/bin/ruff check .`
- **AC-002.** Проверка типов в строгом режиме проходит.
  Проверка: `.venv/bin/mypy`
- **AC-003.** Весь тестовый корпус зелёный. **Критерий агрегирующий: при его
  провале остальные критерии всё равно выполнить и отчитаться по каждому.**
  Проверка: `.venv/bin/pytest -q`
- **AC-004.** `keys` ответа сопоставляются с запрошенными `dimensions` по
  порядку, а не по угадыванию имён.
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k keys`
- **AC-005.** Диапазон дат разбивается по дням: три дня дают три запроса.
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k daily`
- **AC-006.** Внутри дня работает постраничная выборка через `startRow`, а
  `row_limit` больше 25 000 отвергается.
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k paging`
- **AC-007.** Достижение суточного порога ставит `truncated=True` с причиной и
  прекращает выборку; молчаливого обрезания нет.
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k truncat`
- **AC-008.** `HttpError` с кодами 403, 429 и 500 переводится в
  `AuthenticationError`, `RateLimitError` и `ApiError`. **Все три кода
  проверяются поимённо.**
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k errors`
- **AC-009.** Ключ кеша различает `site_url`, блокирующий вызов уходит не в
  поток event loop, без кредов провайдер недоступен.
  Проверка: `.venv/bin/pytest -q tests/test_search_console_provider.py -k "cache or thread or credentials"`
- **AC-010.** Пороги поиска возможностей берутся из настроек: их изменение
  меняет результат, а `quick_win` и `content_expansion` различаются.
  Проверка: `.venv/bin/pytest -q tests/test_opportunities.py`
- **AC-011.** Use-case не выпускает исключения наружу, усечение даёт
  `partial`, а окно по умолчанию заканчивается позавчерашним днём.
  Проверка: `.venv/bin/pytest -q tests/test_gsc_usecase.py`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/specs/m3-expand.md docs/specs/m4-trends.md docs/specs/m5-google-ads.md docs/autocomplete.md docs/expansion.md docs/trends.md docs/google-ads.md tests/fixtures src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/storage src/google_keyword_ai/http.py src/google_keyword_ai/cache.py src/google_keyword_ai/normalize.py src/google_keyword_ai/expansion.py src/google_keyword_ai/market.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/providers/autocomplete.py src/google_keyword_ai/providers/expander.py src/google_keyword_ai/providers/trends src/google_keyword_ai/providers/google_ads.py`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` или parity зависнут, останови их, отчитайся `not_attempted` с
>    этой причиной и продолжай остальные критерии. Делать инструмент
>    асинхронным ЗАПРЕЩЕНО.
> 3. **Кредов Search Console нет ни у кого, включая принимающего.** Живой вызов
>    невозможен в принципе — не пытайся и не считай это блокирующим.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Мутационная проверка: сломать сопоставление `keys` с
  `dimensions`, разбиение по дням, постраничную выборку, флаг усечения, каждый
  из трёх переводов кода ошибки, участие `site_url` в ключе кеша, уход вызова в
  поток и каждый порог отбора возможностей.
- **HC-003.** Сборка wheel и `gkai doctor` из установленного пакета: провайдер
  `search_console` показан недоступным.
- **HC-004.** Проверить, что отсутствие кредов не ломает остальные команды:
  `gkai suggest`, `gkai expand`, `gkai trends` продолжают работать вживую.
- **HC-005.** Живой вызов `find_gsc_opportunities` по настоящему stdio —
  ожидается конверт с `empty` и причиной, а не падение.

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
  `tests/fixtures/**`), `docs/search-console.md`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/`, `.toolchain/` и `tests/fixtures/**`,
  обращаться к настоящим API Google, писать файлы во временные каталоги в
  качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
