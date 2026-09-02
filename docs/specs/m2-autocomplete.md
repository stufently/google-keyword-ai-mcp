# Спека: HTTP-слой, кеш и Autocomplete (M2)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `2 из 9` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Веха M1 принята: в дереве уже есть `config.py`, `errors.py`, `logging.py`,
`market.py`, `envelope.py`, `storage/` (движок SQLite с PRAGMA и forward-only
миграциями, таблица `cache_entries`), `usecases/doctor.py`, `cli/main.py`,
`mcp/server.py` и 32 зелёных теста. **Читай этот код и опирайся на его типы, не
переписывая их.**

Эта веха добавляет первый настоящий источник данных и инфраструктуру, которая
ему нужна, — вертикальным срезом: HTTP-клиент с ретраями, троттлер, логика
кеша, нормализация ключевых слов и провайдер Google Autocomplete с одной
командой `suggest`.

**Чего в этой вехе НЕТ:** веерного расширения (`expand`, алфавиты,
intent-модификаторы, рекурсия) — это M3; Google Trends — M4; Google Ads — M5;
Search Console — M6. Не реализовывай их и не создавай под них файлы.

## Проверенные факты

Проверено вживую 2026-09-02 запросами с этого хоста. По памяти не переписывать.

### Endpoint автокомплита

Основной:

```
https://www.google.com/complete/search?client=chrome&ie=utf-8&oe=utf-8&q=<запрос>&hl=<язык>&gl=<страна>
```

Запасной:

```
https://suggestqueries.google.com/complete/search?client=firefox&ie=utf-8&oe=utf-8&q=<запрос>&hl=<язык>&gl=<страна>
```

Оба отвечают `200`, `content-type: text/javascript; charset=UTF-8`.

**`ie=utf-8&oe=utf-8` обязательны.** Без них ответ приходит не в UTF-8, и
кириллица превращается в мусор — проверено.

**`client=chrome` строго лучше запасного, и по двум причинам сразу.**
Для `q="аренда квартиры", hl=ru, gl=TH` он вернул
`аренда квартиры бангкок / паттайя / пхукет`, тогда как `client=firefox` на тех
же параметрах — `москва / киев / минск`, то есть **`gl` он учитывает заметно
хуже**. Кроме того, chrome отдаёт релевантности, а firefox нет.

**Формат ответа `client=chrome`** — JSON-массив из пяти элементов:

```json
["аренда квартиры",
 ["аренда квартиры бангкок", "аренда квартиры паттайя", "..."],
 ["", "", ""],
 [],
 {"google:clientdata": {"bpc": false, "tlw": false},
  "google:suggestrelevance": [1252, 1251, 1250, 850, 601],
  "google:suggesttype": ["QUERY", "QUERY", "QUERY"]}]
```

Элемент 0 — эхо запроса, элемент 1 — подсказки, элемент 4 — словарь
метаданных. `google:suggestrelevance` — параллельный подсказкам список чисел.

**Формат ответа `client=firefox`** — массив из четырёх элементов:

```json
["аренда квартиры", ["аренда квартиры москва", "..."], [], {"google:suggestsubtypes": [[512]]}]
```

Релевантностей в нём НЕТ.

Отсюда требования к разбору: длина массива разная у двух клиентов, поэтому
опираться на индексы 0 и 1 и на наличие ключа в словаре метаданных, а не на
фиксированную длину. Списка релевантностей может не быть, он может быть короче
списка подсказок — обрабатывать это, а не падать.

🚨 **`google:suggestrelevance` — это НЕ частотность и НЕ объём поиска.** Это
внутренний вес ранжирования подсказок Google. В моделях и в выводе он
называется `relevance` и нигде не должен превращаться в число запросов.

Пустой `q=` возвращает `200`, а не ошибку.

### Что уже есть в дереве

- `Envelope[T]` с `to_wire()`, `Completeness`, `SCHEMA_VERSION = "1.0.0"` в
  `envelope.py`. Любой вывод команды заворачивается в него.
- `Market` в `market.py` с методом `autocomplete_params()`, который уже
  возвращает `{"hl": язык, "gl": страна}`.
- Таксономия ошибок в `errors.py`: `NetworkError`, `RateLimitError`,
  `ProviderUnavailableError`, `ApiError`, `InvalidConfigurationError` и другие.
- `Settings` в `config.py` с префиксом окружения `GKAI_` и порядком
  приоритетов «окружение → `.gkai.toml` → пользовательский конфиг → умолчания».
- `storage/engine.py::open_database` и таблица `cache_entries` с колонками
  `key, provider, endpoint, account_scope, parser_version, payload, created_at,
  expires_at`.
- Инструменты MCP регистрируются на `MCPServer` из `mcp.server.mcpserver`,
  **функция инструмента синхронная** — SDK сам уводит её в поток через
  `anyio.to_thread.run_sync`; асинхронная блокировала бы event loop.
- `respx` установлен и предназначен для мокирования `httpx` в тестах. Сети в
  песочнице нет: **все тесты этой вехи обязаны быть на моках**.

## Что делать

### 1. Расширить `src/google_keyword_ai/config.py`

Добавить в `Settings` поля (не трогая существующие):

- `http_timeout_seconds: float = 10.0`;
- `http_max_attempts: int = 3`;
- `http_backoff_base_seconds: float = 0.5`;
- `http_user_agent: str` — значение по умолчанию
  `"google-keyword-ai/<версия> (+https://github.com/stufently/google-keyword-ai-mcp)"`;
- `autocomplete_rate_limit_per_second: float = 5.0`;
- `autocomplete_cache_ttl_seconds: int = 86400`;
- `cache_enabled: bool = True`.

Валидация: `http_max_attempts >= 1`, положительные таймаут и TTL, положительный
rate limit — иначе `InvalidConfigurationError`.

### 2. `src/google_keyword_ai/http.py`

Асинхронный HTTP-слой на `httpx.AsyncClient`.

- `build_client(settings: Settings) -> httpx.AsyncClient` — таймаут из
  настроек, заголовки `User-Agent` и `Accept-Language`, `follow_redirects=True`,
  пул соединений по умолчанию.
- `async def request_with_retries(client, method, url, *, params, settings, retryable_statuses=(429, 500, 502, 503, 504)) -> httpx.Response`:
  - до `http_max_attempts` попыток;
  - повтор ТОЛЬКО для `retryable_statuses` и для сетевых ошибок
    (`httpx.TransportError`); прочие 4xx повтору не подлежат;
  - задержка — экспоненциальная от `http_backoff_base_seconds`
    (`base * 2**(попытка-1)`) плюс случайный джиттер в пределах половины
    задержки;
  - если ответ 429 или 503 содержит заголовок `Retry-After` с числом секунд —
    ждать именно его, а не расчётную задержку;
  - паузы делать через `anyio.sleep`, чтобы их можно было подменить в тестах;
  - исчерпаны попытки на 429 → `RateLimitError`; на сетевой ошибке →
    `NetworkError`; на прочем неуспешном статусе → `ApiError`. У каждой ошибки
    в `details` положить `status_code` и `url` (без query-строки с секретами —
    их здесь нет, но правило общее).

### 3. `src/google_keyword_ai/ratelimit.py`

- `class AsyncRateLimiter` с параметром `rate_per_second: float`;
- метод `async def acquire(self) -> None` — выдерживает минимальный интервал
  `1 / rate_per_second` между выдачами, безопасен при конкурентных вызовах
  (`anyio.Lock`), спит через `anyio.sleep`;
- `rate_per_second <= 0` → `InvalidConfigurationError`.

В этой вехе троттлер внутрипроцессный. Межпроцессный понадобится Google Ads в
M5 — здесь его НЕ делать.

### 4. `src/google_keyword_ai/cache.py`

Логика поверх таблицы `cache_entries`, созданной в M1.

- `PARSER_VERSION: str = "1"` — версия разбора ответов; входит в ключ.
- `def build_cache_key(provider, endpoint, params: Mapping[str, str], account_scope: str, parser_version: str) -> str`
  — sha256 от канонического JSON (ключи отсортированы,
  `ensure_ascii=False`, разделители без пробелов), hex-строка.
  🚨 `account_scope` обязателен в ключе: без него пользователь с двумя
  аккаунтами получит из кеша чужие данные. Для анонимных источников вроде
  автокомплита это пустая строка.
- `class SqliteCache` с конструктором от `Engine` и `Settings`:
  - `get(key) -> bytes | None` — возвращает `payload`, если запись есть и не
    истекла; истёкшую запись удаляет и возвращает `None`;
  - `set(key, *, provider, endpoint, account_scope, parser_version, payload, ttl_seconds)`
    — upsert по первичному ключу (`INSERT ... ON CONFLICT(key) DO UPDATE`),
    `created_at` и `expires_at` в ISO-8601 UTC; `ttl_seconds is None` →
    `expires_at = NULL`;
  - при `settings.cache_enabled is False` `get` всегда возвращает `None`, а
    `set` ничего не пишет.
- Кешируются только успешные результаты. Ошибки авторизации и 429 не
  кешируются — обеспечивается тем, что вызывающий кладёт в кеш только успешный
  разобранный ответ.

### 5. `src/google_keyword_ai/normalize.py`

- `def normalize_keyword(text: str, *, collapse_punctuation: bool = False) -> str`:
  - Unicode-нормализация NFKC;
  - схлопывание любых пробельных последовательностей в один пробел, обрезка по
    краям;
  - `casefold()`;
  - при `collapse_punctuation=True` — замена пунктуации на пробелы с повторным
    схлопыванием.
  Стемминга и удаления стоп-слов НЕ делать: они склеивают разные интенты.
- `class KeywordCandidate` (pydantic): `raw: str`, `normalized: str`,
  `discovered_from: list[str]`, `relevance: int | None = None`.
- `def deduplicate(candidates: Iterable[KeywordCandidate]) -> list[KeywordCandidate]`
  — схлопывает по `normalized`, сохраняя ПЕРВЫЙ `raw`, объединяя
  `discovered_from` без потерь и в порядке появления, беря максимальную
  известную `relevance`. Порядок результата — порядок первого появления.

### 6. `src/google_keyword_ai/providers/__init__.py` и `providers/base.py`

- `class ProviderInfo` (pydantic): `name: str`, `official: bool`,
  `stability: Literal["stable", "unofficial"]`.
- `class Provider(abc.ABC)`: свойство `info -> ProviderInfo`, метод
  `is_available(self) -> bool`.

Ничего лишнего: базовый класс нужен, чтобы M4–M6 встраивались без переделки.

### 7. `src/google_keyword_ai/providers/autocomplete.py`

- `PRIMARY_ENDPOINT = "https://www.google.com/complete/search"` с
  `client=chrome`;
- `FALLBACK_ENDPOINT = "https://suggestqueries.google.com/complete/search"` с
  `client=firefox`;
- `class Suggestion` (pydantic): `text: str`, `relevance: int | None`,
  `source: str` (какой endpoint дал), `retrieved_at: datetime`;
- `def parse_response(payload: str) -> tuple[list[str], list[int | None]]` —
  разбор обоих форматов: подсказки берутся из элемента с индексом 1;
  релевантности — из `google:suggestrelevance` последнего элемента, если он
  словарь и ключ есть; если релевантностей нет или их меньше, чем подсказок,
  недостающие — `None`. Некорректный JSON → `ApiError`;
- `class AutocompleteProvider(Provider)`:
  - `info` → `name="autocomplete"`, `official=False`, `stability="unofficial"`;
  - `is_available()` → всегда `True` (сеть проверяется в момент запроса);
  - `async def suggest(self, query: str, market: Market, *, limit: int | None = None) -> list[Suggestion]`:
    порядок действий — **кеш → троттлер → HTTP**; при неуспехе основного
    endpoint'а (любая `GkaiError`) один раз пробуется запасной, и только потом
    ошибка выпускается наружу; успешный разобранный ответ кладётся в кеш с TTL
    `autocomplete_cache_ttl_seconds`; `limit` обрезает результат уже после
    разбора.

Провайдер не должен пытаться обходить блокировки: ни ротации прокси, ни
подмены отпечатков, ни решения CAPTCHA. При устойчивом 429 наружу уходит
`RateLimitError`.

### 8. `src/google_keyword_ai/usecases/suggest.py`

- `class SuggestData` (pydantic): `query: str`, `language: str`,
  `country: str`, `provider: ProviderInfo`, `suggestions: list[Suggestion]`;
- `def run_suggest(settings: Settings, query: str, *, language: str | None = None, country: str | None = None, limit: int | None = None) -> Envelope[SuggestData]`
  — синхронная функция-фасад: строит `Market` (при `None` берёт
  `default_language` / `default_country` из настроек), открывает БД, создаёт
  провайдера и выполняет асинхронную работу через `anyio.run`.
  При `RateLimitError`, `NetworkError`, `ApiError` или
  `ProviderUnavailableError` возвращает конверт с пустым списком,
  `completeness=Completeness.EMPTY`, заполненными `errors` и
  `completeness_reason` — исключение наружу НЕ выпускать. Пустой ответ Google
  при успешном запросе — тоже `EMPTY`, но с причиной «no suggestions», без
  `errors`.

### 9. Обёртки

- `src/google_keyword_ai/cli/main.py`: добавить команду
  `gkai suggest <query> [--language ru] [--country RU] [--limit 10] [--format json|table]`,
  печатающую конверт тем же `_print_envelope`. Существующие команды не менять.
- `src/google_keyword_ai/mcp/server.py`: добавить **синхронный** инструмент
  `suggest_keywords(query: str, language: str | None = None, country: str | None = None, limit: int | None = None) -> Envelope[SuggestData]`,
  вызывающий `run_suggest`. Инструмент `doctor` не менять.
- `src/google_keyword_ai/usecases/doctor.py`: у провайдера `autocomplete`
  заменить `detail` на `"ready"` (он теперь реализован). Остальные строки
  статусов не трогать.

### 10. `docs/autocomplete.md`

Короткий документ: какие endpoint'ы используются, что это неофициальный и
недокументированный источник без гарантий, зачем обязательны `ie/oe=utf-8`,
чем `client=chrome` отличается от `client=firefox` (гео и релевантности),
что `google:suggestrelevance` — не частотность, какой TTL кеша и как его
поменять. Не длиннее 60 строк.

### 11. Тесты

Файлы ровно такие:

- `tests/test_http.py` — повтор при 429 и 500, отсутствие повтора при 400,
  уважение `Retry-After`, исчерпание попыток → `RateLimitError` /
  `NetworkError` / `ApiError`. Паузы подменять монкипатчем `anyio.sleep`,
  чтобы тест не спал.
- `tests/test_ratelimit.py` — выдерживается минимальный интервал (замерять
  подменённым `anyio.sleep`, а не настоящими часами), `rate <= 0` → ошибка.
- `tests/test_cache.py` — ключ зависит от `account_scope` и `parser_version`
  (разные значения дают разные ключи), запись и чтение, истёкшая запись
  возвращает `None` и удаляется, `cache_enabled=False` отключает и чтение, и
  запись.
- `tests/test_normalize.py` — NFKC, схлопывание пробелов, casefold, кириллица,
  дедупликация с объединением `discovered_from` и максимумом `relevance`.
- `tests/test_autocomplete.py` — на `respx`: разбор формата chrome (с
  релевантностями) и firefox (без них), короче списка релевантностей,
  некорректный JSON → `ApiError`, падение основного endpoint'а уводит на
  запасной, второй запрос с теми же параметрами берётся из кеша и НЕ ходит в
  сеть.
- `tests/test_suggest_usecase.py` — конверт при успехе; при 429 —
  `completeness=empty`, непустые `errors`, исключение не выпущено; пустой
  ответ Google — `empty` без `errors`.
- Дописать в `tests/test_cli.py` проверку `gkai suggest` на моках и в
  `tests/test_mcp_parity.py` — parity для `suggest_keywords`.

Тесты не должны ходить в сеть ни при каких условиях.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** — всё
  нужное (`httpx`, `anyio`, `respx`) уже установлено. Не хватает библиотеки —
  остановись и доложи, а не ставь.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `docs/superpowers/specs/2026-09-02-google-keyword-ai-mcp-design.md` и
  `docs/specs/m1-scaffold.md` — читать можно, править нельзя.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- Код вехи M1 (`market.py`, `envelope.py`, `errors.py`, `logging.py`,
  `storage/`) менять НЕ нужно; `config.py`, `cli/main.py`, `mcp/server.py` и
  `usecases/doctor.py` правятся только так, как описано в «Что делать».
  Существующие 32 теста должны остаться зелёными без изменений — если какой-то
  из них приходится править, значит ты сломал контракт M1: остановись и доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (алфавиты, модификаторы, trends,
  google_ads, search_console).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/**`, `tests/**` и
`docs/autocomplete.md` — только перечисленные пути.

**Если разрешённого способа не остаётся** — остановись и доложи по разделу
«Контракт на невыполнимое». Создавать доказательства во временных каталогах
(`/tmp` и подобных) запрещено: файл-улика обязан лежать в дереве работы.

## Критерии приёмки

Команды запускать из корня репозитория.

- **AC-001.** Линтер чист.
  Проверка: `.venv/bin/ruff check .`
- **AC-002.** Проверка типов в строгом режиме проходит.
  Проверка: `.venv/bin/mypy`
- **AC-003.** Весь тестовый корпус зелёный, включая 32 теста вехи M1.
  **Критерий агрегирующий: при его провале остальные критерии всё равно
  выполнить и отчитаться по каждому.**
  Проверка: `.venv/bin/pytest -q`
- **AC-004.** HTTP-слой повторяет запрос при 429 и 5xx, не повторяет при 400 и
  уважает `Retry-After`.
  Проверка: `.venv/bin/pytest -q tests/test_http.py`
- **AC-005.** Троттлер выдерживает интервал и отвергает неположительный rate.
  Проверка: `.venv/bin/pytest -q tests/test_ratelimit.py`
- **AC-006.** Ключ кеша различает `account_scope` и `parser_version`, истёкшая
  запись не возвращается, выключенный кеш не читает и не пишет.
  Проверка: `.venv/bin/pytest -q tests/test_cache.py`
- **AC-007.** Нормализация и дедупликация работают, `discovered_from`
  объединяется без потерь.
  Проверка: `.venv/bin/pytest -q tests/test_normalize.py`
- **AC-008.** Провайдер разбирает оба формата ответа, уходит на запасной
  endpoint при отказе основного и второй раз отвечает из кеша без обращения к
  сети.
  Проверка: `.venv/bin/pytest -q tests/test_autocomplete.py`
- **AC-009.** Отказ провайдера не выпускает исключение наружу: конверт
  получает `completeness=empty` и непустой `errors`.
  Проверка: `.venv/bin/pytest -q tests/test_suggest_usecase.py`
- **AC-010.** `gkai suggest` печатает конверт в stdout и MCP-инструмент
  `suggest_keywords` отдаёт тот же payload.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py tests/test_mcp_parity.py`
- **AC-011.** `gkai doctor --format json` по-прежнему работает, а провайдер
  `autocomplete` в нём помечен как `ready`.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py -k doctor`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md src/google_keyword_ai/market.py src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/storage`

> Примечание для исполнителя: коммит в этой песочнице невозможен —
> `.git` смонтирован только для чтения, и `git add` падает с
> `index.lock: Read-only file system`. Это ожидаемо и НЕ является провалом.
> Коммит делает принимающий. Ничего не предпринимай по этому поводу и не
> считай это блокирующим обстоятельством.

## Проверки принимающего

- **HC-001.** Прогон тестов и линтеров в чистом Docker-образе на CPython 3.14 и
  отдельно на 3.12 (нижняя граница `requires-python`).
- **HC-002.** Живой запрос к обоим endpoint'ам автокомплита с реальными
  параметрами (`hl=ru&gl=TH`) — проверить, что разбор совпадает с боевым
  ответом, а не только с фикстурой.
- **HC-003.** Мутационная проверка новых тестов: по очереди сломать повтор при
  429, уважение `Retry-After`, участие `account_scope` в ключе кеша, проверку
  срока годности записи, объединение `discovered_from`, чтение из кеша вместо
  сети, переход на запасной endpoint — каждый раз должен падать именно
  соответствующий тест.
- **HC-004.** Сборка wheel и запуск `gkai suggest` из установленного пакета.
- **HC-005.** Живой вызов `suggest_keywords` по настоящему stdio.

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

- Создавать и править файлы в: `src/google_keyword_ai/**`, `tests/**`,
  `docs/autocomplete.md` — только по путям из раздела «Что делать».
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет. Все тесты — на моках `respx`.
- Git: коммит невозможен (read-only `.git`), это ожидаемо; ветку не
  переключать, ребейз не делать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/` и `.toolchain/`, обращаться к
  настоящим эндпоинтам Google, писать файлы во временные каталоги в качестве
  доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
