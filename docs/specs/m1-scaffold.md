# Спека: каркас google-keyword-ai (M1)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `1 из 8` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Проект — инструмент исследования поискового спроса Google (CLI `gkai`,
MCP-сервер `google-keyword-ai`, Claude Skill поверх них). Общий дизайн на все
восемь вех лежит в `docs/superpowers/specs/2026-09-02-google-keyword-ai-mcp-design.md`
— прочитай его для контекста, но **реализуй только эту веху**.

Репозиторий пуст: есть манифест, залоченные зависимости, установленное
окружение и `src/google_keyword_ai/__init__.py` с одной строкой версии. Кода
нет.

Эта веха закладывает фундамент, на котором стоят все остальные: конфигурация,
единая модель «язык + регион», конверт ответа, таксономия ошибок, логи, движок
БД с миграциями, и две тонкие обёртки — CLI и MCP-сервер — с одной-единственной
командой `doctor`. Провайдеров (Autocomplete, Trends, Google Ads, Search
Console) в этой вехе НЕТ: они появятся в M2–M5. HTTP-слой, retry, кеш и
троттлинг тоже НЕ реализуются — они пишутся в M2 вместе с первым потребителем.
В этой вехе от кеша только таблица в БД.

## Проверенные факты

Всё ниже проверено вживую 2026-09-02 в этом самом окружении (`.venv` в дереве).
Ничего из этого не переписывай по памяти — память врёт именно здесь.

**Окружение.** CPython 3.14.3 вендорен в `.toolchain/`, `.venv/` собран им же.
Резолв всего набора зависимостей проверен, конфликтов нет.

**MCP SDK — версия 2.x, а не 1.x. Это главный источник ошибок по памяти:**

- `FastMCP` в 2.x УДАЛЁН. Импорт `mcp.server.fastmcp` выбрасывает
  `ModuleNotFoundError` с текстом про переименование. Правильно:
  `from mcp.server.mcpserver import MCPServer`.
- Конструктор: `MCPServer(name: str | None = None, ..., version: str = "")`.
- Регистрация инструмента: декоратор `@server.tool()`; сигнатура
  `tool(name=None, title=None, description=None, annotations=None, icons=None,
  meta=None, structured_output=None)`.
- Запуск: `server.run("stdio")` (синхронный) либо
  `await server.run_stdio_async()`.
- Низкоуровневый сервер доступен как `server._lowlevel_server`. Имя приватное,
  но публичного доступа к нему в 2.1.1 нет; он нужен только тестам.
- **Поля протокольных моделей в 2.x — snake_case**, а не camelCase:
  `tool.output_schema`, `result.structured_content`, `result.is_error`.
  Обращение к `outputSchema` даёт `AttributeError`.
- Если функция инструмента аннотирована возвратом pydantic-модели, SDK сам
  строит `output_schema` и заполняет `structured_content`. Проверено:
  `structured_content` побайтово равен `model.model_dump(mode="json")`.
- Тест клиент-сервер без сокетов делается так (сокеты в песочнице запрещены):

  ```python
  from mcp.shared.memory import create_client_server_memory_streams
  from mcp.client.session import ClientSession

  async with create_client_server_memory_streams() as ((cr, cw), (sr, sw)):
      async with anyio.create_task_group() as tg:
          low = server._lowlevel_server
          tg.start_soon(lambda: low.run(sr, sw, low.create_initialization_options(),
                                        raise_exceptions=True))
          async with ClientSession(cr, cw) as client:
              await client.initialize()
              result = await client.call_tool("doctor", {})
          tg.cancel_scope.cancel()
  ```

  Именно эта конструкция отработала; `create_connected_server_and_client_session`
  в `mcp.shared.memory` версии 2.1.1 НЕ существует.

**structlog по умолчанию пишет в stdout.** `structlog.PrintLoggerFactory()`
имеет сигнатуру `__init__(self, file: TextIO | None = None)`, и при `None`
берётся `sys.stdout`. Для этого проекта stdout обязан оставаться чистым JSON,
поэтому фабрику надо создавать явно: `PrintLoggerFactory(file=sys.stderr)`.

**Typer 0.27.2**, pydantic 2.13.5, pydantic-settings 2.15.0, SQLAlchemy 2.0.52,
mcp 2.1.1, anyio, structlog 26.1.0. Dev-группа: pytest 9.1.1, ruff 0.16.5,
mypy 2.3.1, respx 0.23.1.

**Стартовое состояние проверок — красное, и это нормально.** Прямо сейчас
`.venv/bin/ruff check .` проходит, а `.venv/bin/mypy` возвращает 2
(`There are no .py[i] files in directory 'tests'`) и `.venv/bin/pytest -q`
возвращает 5 (`no tests ran`) — просто потому, что кода и тестов ещё нет. Это
НЕ дефект конфигурации: чинить `pyproject.toml` не нужно и запрещено, обе
команды позеленеют сами, когда появятся файлы этой вехи.

**Консольные скрипты уже прописаны** в `.venv/bin/gkai` и
`.venv/bin/google-keyword-ai`; они указывают на
`google_keyword_ai.cli.main:main` и `google_keyword_ai.mcp.server:main`.
Эти пути менять нельзя — иначе скрипты сломаются.

**ISO-коды.** Search Console принимает страну как ISO 3166-1 alpha-3 в нижнем
регистре (`rus`, `tha`, `usa`), Autocomplete и Trends — alpha-2 (`RU`, `TH`).
Google Ads требует числовые criteria ID (`geoTargetConstants/2643`) — но их
таблица подключается в M4, здесь только место под неё.

## Что делать

Создать перечисленные ниже файлы. Других файлов не создавать.

### 1. `src/google_keyword_ai/errors.py`

Иерархия исключений с общим предком `GkaiError`:
`AuthenticationError`, `RateLimitError`, `ProviderUnavailableError`,
`InvalidConfigurationError`, `NetworkError`, `ApiError`, `PartialResultError`.

У `GkaiError` — поля `message: str` и `details: dict[str, object]`
(по умолчанию пустой словарь). Голых `except` в коде проекта быть не должно.

### 2. `src/google_keyword_ai/logging.py`

Настройка structlog. Функция `configure_logging(level: str) -> None`.

Требования:
- вывод идёт **только в stderr**: `PrintLoggerFactory(file=sys.stderr)`;
- формат — JSON-строки (`structlog.processors.JSONRenderer`);
- уровень берётся из аргумента, значения `debug|info|warning|error`;
- повторный вызов не должен дублировать процессоры.

### 3. `src/google_keyword_ai/market.py`

Единая модель «язык + регион». Класс `Market` (pydantic-модель, frozen):

- поля `language: str` (BCP-47 / ISO 639-1, нижний регистр, например `ru`),
  `country: str` (ISO 3166-1 alpha-2, ВЕРХНИЙ регистр, например `RU`);
- валидация в конструкторе: язык и страна приводятся к каноническому регистру;
  неизвестный код страны или языка → `InvalidConfigurationError`;
- `Market.parse(language: str, country: str) -> Market` — фабрика;
- свойство/метод `autocomplete_params() -> dict[str, str]` → `{"hl": язык,
  "gl": страна}`;
- `trends_geo() -> str` → alpha-2 (`"RU"`);
- `gsc_country() -> str` → alpha-3 в НИЖНЕМ регистре (`"rus"`);
- `ads_criteria_id() -> int` — в этой вехе **всегда** поднимает
  `ProviderUnavailableError` с сообщением, что таблица criteria ID подключается
  в M4. Метод существует, чтобы интерфейс не менялся позже.

Таблица alpha-2 → alpha-3 и список допустимых языков — константы в этом же
файле. Полный список стран мира не нужен: достаточно покрыть минимум
`RU, TH, US, GB, DE, FR, ES, IT, KZ, BY, UA, PL, TR, AE, CN, JP, IN, BR`, и
структура должна позволять дописать остальные строкой. Языки — минимум
`ru, en, th, de, fr, es, it, kk, uk, pl, tr, ar, zh, ja, hi, pt`.

### 4. `src/google_keyword_ai/envelope.py`

Конверт ответа — то, что отдают И CLI, И MCP.

```python
SCHEMA_VERSION = "1.0.0"

class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"

class Envelope[T](BaseModel):   # generic по полезной нагрузке
    schema_version: str = SCHEMA_VERSION
    data: T
    warnings: list[str] = []
    errors: list[str] = []
    completeness: Completeness = Completeness.COMPLETE
    completeness_reason: str | None = None
    run_id: str | None = None
```

Метод `to_wire(self) -> dict[str, object]` возвращает
`self.model_dump(mode="json")`. Это ЕДИНСТВЕННОЕ место сериализации в проекте:
CLI печатает `json.dumps(envelope.to_wire())`, MCP возвращает саму модель.

Требование: если `completeness` не `complete`, то `completeness_reason`
обязателен — иначе `InvalidConfigurationError` при валидации.

### 5. `src/google_keyword_ai/config.py`

Конфигурация на pydantic-settings. Класс `Settings`.

Поля минимум:
- `data_dir: Path` — каталог данных, по умолчанию по XDG
  (`$XDG_DATA_HOME/google-keyword-ai-mcp`, при отсутствии переменной —
  `~/.local/share/google-keyword-ai-mcp`);
- `log_level: str = "info"`;
- `default_language: str = "en"`, `default_country: str = "US"`;
- `google_ads_developer_token: SecretStr | None = None`;
- `google_ads_customer_id: str | None = None`;
- `google_ads_login_customer_id: str | None = None`;
- `google_ads_client_id: SecretStr | None = None`;
- `google_ads_client_secret: SecretStr | None = None`;
- `google_ads_refresh_token: SecretStr | None = None`;
- `search_console_credentials_path: Path | None = None`.

Префикс переменных окружения — `GKAI_` (то есть
`GKAI_GOOGLE_ADS_DEVELOPER_TOKEN` и т. д.).

**Порядок приоритетов, строго в этом порядке** (первый выигрывает):

1. переменные окружения `GKAI_*`;
2. `.gkai.toml` в текущем рабочем каталоге;
3. `$XDG_CONFIG_HOME/google-keyword-ai-mcp/config.toml`
   (при отсутствии переменной — `~/.config/google-keyword-ai-mcp/config.toml`);
4. значения по умолчанию.

Функция `load_settings(cwd: Path | None = None) -> Settings` — загрузка с этим
порядком. Функция `masked_dump(settings: Settings) -> dict[str, object]` —
словарь для показа человеку, где значение каждого секретного поля заменено на
`"***"` при непустом значении и `null` при пустом. Реальные значения секретов
не должны попадать в результат ни при каком поле.

### 6. `src/google_keyword_ai/storage/__init__.py`

Пустой (реэкспорт по вкусу).

### 7. `src/google_keyword_ai/storage/engine.py`

Создание движка SQLAlchemy 2 к SQLite.

- `database_path(settings: Settings) -> Path` → `<data_dir>/gkai.sqlite3`;
- `create_engine_for(settings: Settings) -> Engine` — создаёт каталог, если его
  нет, и движок;
- на каждое соединение через событие `connect` выставляются PRAGMA:
  `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`,
  `foreign_keys=ON`;
- `open_database(settings: Settings) -> Engine` — создаёт движок и применяет
  миграции (см. ниже).

### 8. `src/google_keyword_ai/storage/migrations.py`

Forward-only миграции без Alembic.

- `SCHEMA_VERSION: int` — текущая версия схемы, в этой вехе `1`;
- `MIGRATIONS: list[Callable[[Connection], None]]` — упорядоченный список, где
  индекс `i` переводит схему с версии `i` на `i+1`;
- `apply_migrations(engine: Engine) -> int` — читает `PRAGMA user_version`,
  применяет недостающие миграции по порядку, каждую в своей транзакции, ставит
  новое значение `user_version`, возвращает итоговую версию;
- повторный вызов на актуальной БД не делает ничего и не падает
  (идемпотентность);
- если `user_version` в файле БОЛЬШЕ `SCHEMA_VERSION` (база от более новой
  версии программы) — поднять `InvalidConfigurationError` с внятным текстом, а
  не пытаться работать.

Миграция №1 создаёт таблицу кеша `cache_entries`:

```
key           TEXT PRIMARY KEY   -- уже посчитанный хеш ключа
provider      TEXT NOT NULL
endpoint      TEXT NOT NULL
account_scope TEXT NOT NULL      -- customer id / property / "" для анонимных
parser_version TEXT NOT NULL
payload       BLOB NOT NULL
created_at    TEXT NOT NULL      -- ISO-8601 UTC
expires_at    TEXT               -- ISO-8601 UTC, NULL = без срока
```
плюс индекс по `expires_at`.

Логику кеша (запись, чтение, TTL, вычисление ключа) в этой вехе НЕ писать —
только таблица. Она появится в M2.

### 9. `src/google_keyword_ai/usecases/__init__.py` и `usecases/doctor.py`

Use-case слой: место, где живёт логика, общая для CLI и MCP.

Модель полезной нагрузки:

```python
class ProviderStatus(BaseModel):
    name: str
    available: bool
    detail: str          # человекочитаемая причина

class DoctorData(BaseModel):
    version: str                    # google_keyword_ai.__version__
    python_version: str
    data_dir: str
    database: str                   # "ok" либо текст ошибки
    schema_version: int
    providers: list[ProviderStatus]
```

Функция `run_doctor(settings: Settings) -> Envelope[DoctorData]`.

Что она проверяет в этой вехе:
- открывается ли БД и применяются ли миграции (`database`, `schema_version`);
- существует и доступен ли на запись `data_dir`;
- по каждому из четырёх провайдеров `autocomplete`, `trends`, `google_ads`,
  `search_console` — статус. Провайдеров ещё нет, поэтому статусы вычисляются
  так: `autocomplete` и `trends` → `available=True`, detail
  `"not implemented yet (M2/M3)"`; `google_ads` → `available=False`, detail
  `"missing credentials"` при отсутствии `google_ads_developer_token` и
  `"not implemented yet (M4)"` при наличии; `search_console` → `available=False`,
  detail `"missing credentials"` при отсутствии
  `search_console_credentials_path` и `"not implemented yet (M5)"` при наличии.

Если БД открыть не удалось, конверт получает `completeness=partial` и
`completeness_reason` с причиной; исключение наружу не выпускать.

Функция `run_config_show(settings: Settings) -> Envelope[dict[str, object]]` —
конверт вокруг `masked_dump(settings)`.

### 10. `src/google_keyword_ai/cli/__init__.py` и `cli/main.py`

Typer-приложение. Точка входа `main()` (её уже ждёт консольный скрипт).

Команды:
- `gkai doctor [--format json|table]`;
- `gkai config show [--format json|table]`.

Правила:
- при `--format json` (значение по умолчанию — `json`) в **stdout** печатается
  ровно `json.dumps(envelope.to_wire(), ensure_ascii=False)` и перевод строки,
  больше ничего;
- при `--format table` печатается человекочитаемая таблица; она тоже идёт в
  stdout, а логи — всегда в stderr;
- логирование настраивается вызовом `configure_logging` из `logging.py`
  до выполнения команды;
- никакой логики, кроме разбора аргументов, вызова use-case и печати. Любая
  проверка провайдеров, работа с БД и т. п. — в use-case слое.

Код возврата: `0` при `completeness=complete`, `1` при любом другом.

### 11. `src/google_keyword_ai/mcp/__init__.py` и `mcp/server.py`

MCP-сервер на `MCPServer` (см. «Проверенные факты» — API 2.x, не 1.x).

- `build_server(settings: Settings | None = None) -> MCPServer` — собирает
  сервер с именем `google-keyword-ai`, версией из `__version__` и одним
  инструментом;
- инструмент `doctor` без аргументов, аннотированный возвратом
  `Envelope[DoctorData]`, внутри вызывает тот же `run_doctor`;
- `main() -> None` — точка входа консольного скрипта: настраивает логирование
  и запускает `server.run("stdio")`.

Собственной сериализации в MCP быть не должно: инструмент возвращает модель,
`structured_content` SDK строит сам.

### 12. Тесты в `tests/`

Файлы ровно такие:

- `tests/conftest.py` — общие фикстуры: временный `data_dir`, `Settings` с ним,
  очистка переменных окружения `GKAI_*` между тестами.
- `tests/test_config.py` — порядок приоритетов и маскирование секретов.
- `tests/test_market.py` — маппинги `Market` и ошибки на неизвестных кодах.
- `tests/test_envelope.py` — `to_wire`, требование `completeness_reason`.
- `tests/test_storage.py` — PRAGMA, создание таблицы, идемпотентность
  миграций, отказ на БД из будущего.
- `tests/test_cli.py` — запускает CLI отдельным процессом через
  `subprocess.run([sys.executable, "-m", "google_keyword_ai.cli.main", ...])`
  либо через путь к скрипту `gkai`, и проверяет:
  - `test_doctor_json_envelope` — stdout разбирается как ОДИН JSON-объект, в
    нём есть `schema_version` (равен `1.0.0`), `data`, `warnings`, `errors`,
    `completeness`, а `data["providers"]` содержит ровно четыре записи;
  - `test_stdout_clean_with_debug_logs` — при `GKAI_LOG_LEVEL=debug` stdout
    по-прежнему разбирается как JSON, а stderr при этом НЕ пуст (лог пишется,
    но не в stdout);
  - `test_config_show_masks_secrets` — при заданном через окружение
    `GKAI_GOOGLE_ADS_DEVELOPER_TOKEN` его значение не встречается ни в stdout,
    ни в stderr;
  - коды возврата: `0` при `completeness=complete`.

  Имена тестов важны: по ним отбираются критерии приёмки
  (`-k doctor_json`, `-k stdout_clean`, `-k mask`).
- `tests/test_mcp_parity.py` — parity-тест CLI ↔ MCP на in-memory потоках.

Тесты асинхронного MCP писать через `anyio.run(...)` внутри обычной
синхронной тестовой функции — плагин `pytest-asyncio` в окружение НЕ
установлен, и добавлять его нельзя.

## Не трогать

- `pyproject.toml` и `uv.lock` — набор и версии зависимостей закреплены и
  проверены на совместимость. **Новых зависимостей не добавлять.** Если задача
  без новой библиотеки не решается — остановись и напиши об этом в отчёте, а не
  ставь молча.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — он переписывается в M8.
- `docs/superpowers/specs/2026-09-02-google-keyword-ai-mcp-design.md` — общий
  дизайн, его читают, но не правят.
- `src/google_keyword_ai/__init__.py` — строку версии не менять.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни дополнительной документации, ни CI-конфигов. Документация провайдеров и CI
  относятся к другим вехам.
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** только `src/google_keyword_ai/**` и `tests/**`,
и только те пути, что перечислены в «Что делать».

**Если разрешённого способа не остаётся** — остановись и доложи по разделу
«Контракт на невыполнимое». Обходить запрет, создавая файлы во временных
каталогах (`/tmp` и подобных), запрещено: доказательство обязано лежать в
дереве работы и быть на месте на момент приёмки.

## Критерии приёмки

Все команды запускать из корня репозитория. Интерпретатор и инструменты — из
`.venv/bin/`.

- **AC-001.** Линтер чист.
  Проверка: `.venv/bin/ruff check .`
- **AC-002.** Проверка типов в строгом режиме проходит.
  Проверка: `.venv/bin/mypy`
- **AC-003.** Весь тестовый корпус зелёный. **Критерий агрегирующий: при его
  провале остальные критерии всё равно выполнить и отчитаться по каждому.**
  Проверка: `.venv/bin/pytest -q`
- **AC-004.** `gkai doctor --format json` печатает в stdout ровно один
  JSON-объект, в котором есть ключи `schema_version`, `data`, `warnings`,
  `errors`, `completeness`, а `data` содержит `providers` из четырёх записей.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py -k doctor_json`
- **AC-005.** Логи не попадают в stdout даже на уровне debug: stdout остаётся
  разбираемым JSON, а сообщения лога видны в stderr.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py -k stdout_clean`
- **AC-006.** Секреты маскируются: значение токена, заданного через окружение,
  не встречается в выводе `gkai config show`.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py -k mask`
- **AC-007.** Порядок приоритетов конфигурации соблюдён: окружение перекрывает
  `.gkai.toml`, тот перекрывает пользовательский конфиг, тот — значения по
  умолчанию.
  Проверка: `.venv/bin/pytest -q tests/test_config.py -k precedence`
- **AC-008.** У открытой БД выставлены `journal_mode=wal`, `foreign_keys=1`,
  `busy_timeout=5000`, а `user_version` равен 1, и таблица `cache_entries`
  создана.
  Проверка: `.venv/bin/pytest -q tests/test_storage.py -k pragma`
- **AC-009.** Миграции идемпотентны, а БД с `user_version` больше текущей
  отвергается ошибкой `InvalidConfigurationError`.
  Проверка: `.venv/bin/pytest -q tests/test_storage.py -k "idempotent or future"`
- **AC-010.** `Market` для `ru/RU` даёт `hl=ru`, `gl=RU`, `trends_geo()=="RU"`,
  `gsc_country()=="rus"`; неизвестная страна даёт `InvalidConfigurationError`;
  `ads_criteria_id()` поднимает `ProviderUnavailableError`.
  Проверка: `.venv/bin/pytest -q tests/test_market.py`
- **AC-011.** Parity: `structured_content`, полученный от MCP-инструмента
  `doctor` через in-memory потоки, равен телу конверта, которое CLI печатает в
  stdout для той же операции.
  Проверка: `.venv/bin/pytest -q tests/test_mcp_parity.py`
- **AC-012.** Ни один файл вне перечисленных в «Что делать» не создан и не
  изменён, рабочее дерево чистое.
  Проверка: `git status --porcelain`

## Проверки принимающего

- **HC-001.** Прогон тестов и линтеров в Docker на чистом образе — убедиться,
  что результат не зависит от вендоренного окружения.
  Как: `docker run --rm -v <repo>:<repo> -w <repo> ghcr.io/astral-sh/uv:bookworm-slim bash -lc "uv run pytest -q"`
- **HC-002.** Матрица версий: тот же прогон на CPython 3.12, поскольку
  `requires-python = ">=3.12"` — это обещание, а окружение вехи собрано на 3.14.
- **HC-003.** Сборка wheel и проверка, что пакет ставится и консольные скрипты
  работают из установленного дистрибутива, а не только из дерева.
- **HC-004.** Мутационная проверка новых тестов: сломать по очереди
  маскирование секретов, каждую PRAGMA, проверку `completeness_reason`,
  отказ на БД из будущего и парити-сериализацию — убедиться, что падает именно
  соответствующий тест, а не соседний.
- **HC-005.** Реальный запуск MCP-сервера по stdio из Claude Code и вызов
  инструмента `doctor` — транспорт в песочнице не проверяется.

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

- Создавать и править файлы в: `src/google_keyword_ai/**`, `tests/**` — только
  по путям из раздела «Что делать».
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` (чтение статуса, добавление,
  коммит).
- Сеть: **не использовать**, её нет.
- Git: коммиты в текущей ветке `m1-scaffold` делать, не пушить, ветку не
  переключать, ребейз не делать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/` и `.toolchain/`, обращаться к внешним
  API, писать файлы во временные каталоги в качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
