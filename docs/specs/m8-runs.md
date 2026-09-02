# Спека: запуски и продолжение после сбоя (M8)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `8 из 10` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M7 приняты: каркас, четыре источника данных и три сценария
исследования с бюджетом и dry-run. 243 зелёных теста. **Читай существующий код
и опирайся на его типы, не переписывая их.**

Сейчас `gkai research` ничего не сохраняет: упал на середине — начинай заново и
трать лимиты повторно. Эта веха даёт запускам память.

**Чего в этой вехе НЕТ:** scoring и кластеризация (M9), Claude Skill, docs и
README (M10). Новых источников данных тоже нет.

## Проверенные факты

### Механизм миграций уже есть и его надо использовать

`src/google_keyword_ai/storage/migrations.py` содержит:

- `SCHEMA_VERSION: int` — сейчас `1`;
- `MIGRATIONS: list[Callable[[Connection], None]]` — индекс `i` переводит
  схему с версии `i` на `i+1`;
- `apply_migrations(engine) -> int` — читает `PRAGMA user_version`, применяет
  недостающие миграции, **каждую в своей транзакции**, идемпотентен, и
  поднимает `InvalidConfigurationError`, если база новее программы.

Существующая миграция №1 создала таблицу `cache_entries`. Эта веха добавляет
миграцию №2 и поднимает `SCHEMA_VERSION` до `2`. Существующую миграцию **не
трогать**: её уже применили у пользователей, изменение задним числом оставит
базы в несогласованном состоянии.

БД открывается через `storage/engine.py::open_database(settings)`; PRAGMA
`journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`,
`foreign_keys=ON` уже выставляются на каждое соединение.

### Почему кеша недостаточно для продолжения

Слой кеша (`cache.py`) хранит успешные ответы провайдеров. Он НЕ покрывает
падение между внешним вызовом и сохранением ответа: запрос ушёл, лимит и деньги
потрачены, а в кеше пусто. Поэтому у запуска нужна собственная машина
состояний со своими контрольными точками.

### Что уже есть в пайплайне

- `Budget`, `BudgetSpend`, `BudgetGuard` в `pipeline/budget.py`;
- `ResearchData`, `ResearchStats`, `DataQuality`, `DryRunPlan`,
  `ResearchKeyword`, `SourceUsage` в `pipeline/models.py`;
- `NewNicheResearch`, `CompetitorResearch`, `ExistingSiteResearch` и
  `ScenarioContext` в `pipeline/scenarios.py`; у каждого сценария есть
  `async run(context) -> ResearchData` и `plan(context) -> DryRunPlan`;
- `run_research(settings, target, *, scenario, language, country, seed_keyword, budget, dry_run, limit)`
  в `usecases/research.py`;
- `Envelope[T]` требует `completeness_reason`, когда `completeness` не
  `complete`.
- `google_keyword_ai.__version__` — версия приложения;
  `google_keyword_ai.cache.PARSER_VERSION` — версия разбора ответов.

### Ограничения окружения

- Инструменты MCP регистрируются **синхронными** функциями. В этой вехе новых
  инструментов MCP НЕ добавляется.
- Сети в песочнице нет: все тесты — на подделках сценариев и провайдеров.

## Что делать

### 1. Миграция схемы

В `src/google_keyword_ai/storage/migrations.py`: поднять `SCHEMA_VERSION` до
`2`, добавить в конец `MIGRATIONS` функцию `_migration_2`, создающую две
таблицы.

```
runs
  run_id          TEXT PRIMARY KEY      -- вида run_<26 символов>
  scenario        TEXT NOT NULL
  target          TEXT NOT NULL
  language        TEXT NOT NULL
  country         TEXT NOT NULL
  status          TEXT NOT NULL         -- pending|running|completed|failed
  app_version     TEXT NOT NULL
  parser_version  TEXT NOT NULL
  budget          TEXT NOT NULL         -- JSON снимка Budget
  config_snapshot TEXT NOT NULL         -- JSON настроек БЕЗ секретов
  result          TEXT                  -- JSON конверта, NULL пока нет
  error           TEXT
  created_at      TEXT NOT NULL         -- ISO-8601 UTC
  updated_at      TEXT NOT NULL

run_stages
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE
  name        TEXT NOT NULL
  position    INTEGER NOT NULL
  status      TEXT NOT NULL             -- pending|running|completed|failed
  fingerprint TEXT NOT NULL
  attempts    INTEGER NOT NULL DEFAULT 0
  checkpoint  TEXT                      -- JSON результата стадии, NULL пока нет
  error       TEXT
  started_at  TEXT
  finished_at TEXT
  PRIMARY KEY (run_id, name)
```

Плюс индекс по `runs(created_at)`.

🚨 `config_snapshot` пишется через уже существующий
`config.masked_dump(settings)` — секреты в базу попадать не должны ни при
каких условиях.

### 2. `src/google_keyword_ai/pipeline/runs.py`

Модели и хранилище.

- `class StageStatus(StrEnum)`: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`;
- `class RunStatus(StrEnum)`: те же четыре значения;
- `class StageRecord` (pydantic): `name`, `position`, `status`,
  `fingerprint`, `attempts`, `checkpoint: dict[str, object] | None`,
  `error: str | None`, `started_at: datetime | None`,
  `finished_at: datetime | None`;
- `class RunRecord` (pydantic): `run_id`, `scenario`, `target`, `language`,
  `country`, `status`, `app_version`, `parser_version`, `budget: Budget`,
  `config_snapshot: dict[str, object]`, `result: dict[str, object] | None`,
  `error: str | None`, `created_at`, `updated_at`, `stages: list[StageRecord]`;
- `def new_run_id() -> str` — `"run_"` плюс 26 символов из
  `secrets.token_hex`/`uuid4().hex` (детерминированный формат, проверяемый
  регулярным выражением);
- `def stage_fingerprint(name: str, payload: Mapping[str, object]) -> str` —
  sha256 от канонического JSON (`sort_keys=True`, `ensure_ascii=False`,
  `separators=(",", ":")`), первые 32 hex-символа;
- `class RunStore` с конструктором от `Engine`:
  - `create(record) -> None`;
  - `get(run_id) -> RunRecord | None`;
  - `list(limit=20) -> list[RunRecord]` — новые первыми по `created_at`;
  - `save_stage(run_id, stage) -> None` — upsert по `(run_id, name)`;
  - `finish(run_id, *, status, result=None, error=None) -> None`;
  - `delete(run_id) -> bool`.

🚨 **Запись результата стадии атомарна:** `checkpoint`, `status`,
`finished_at` и `attempts` обновляются ОДНИМ оператором в одной транзакции.
Половинчатая стадия — со статусом `completed`, но без контрольной точки — не
должна быть представима.

### 3. `src/google_keyword_ai/pipeline/executor.py`

Машина состояний поверх сценариев.

- `class Stage` (pydantic, frozen): `name: str`, `position: int`,
  `fingerprint_payload: dict[str, object]`;
- `def scenario_stages(scenario_name: str, *, target: str, market, budget, seed_keyword=None) -> list[Stage]`
  — стадии по сценарию:
  - `niche`: `expand`, `ads_metrics`, `trends`;
  - `competitor`: `ads_ideas`, `expand`, `trends`;
  - `site`: `gsc_query`, `opportunities`, `ads_metrics`, `trends`.
  `fingerprint_payload` каждой стадии включает её имя, цель, язык, страну,
  снимок бюджета и `seed_keyword`;
- `class RunExecutor` с конструктором от `RunStore`, сценария и списка стадий:
  - `async def execute(self, record, context, *, resume: bool) -> ResearchData`.

Правила выполнения, буквально:

1. Стадии идут строго по `position`.
2. Стадия **пропускается** (её `checkpoint` берётся как есть), если
   одновременно: `resume is True`, её `status == COMPLETED`, её `fingerprint`
   совпадает с ожидаемым, и `app_version` с `parser_version` записи совпадают
   с текущими.
3. Иначе стадия выполняется: `status=RUNNING`, `attempts += 1`,
   `started_at` проставляется, затем — атомарная запись результата.
4. Отказ стадии: `status=FAILED`, `error` заполняется, запуск получает
   `status=FAILED`, и **исключение наружу НЕ выпускается** — выше по стеку
   формируется конверт.
5. **Смена версии приложения или парсера делает ВСЕ завершённые стадии
   устаревшими**: при `resume` такой запуск начинается заново, а в
   предупреждения попадает причина. Иначе продолжение склеило бы данные,
   разобранные разными версиями кода.

В этой вехе сценарии выполняются целиком одним вызовом `scenario.run(context)`,
а стадии отражают его ход: сценарий сам решает, к каким источникам идти.
**Не переписывай сценарии** ради дробления — задача этой вехи в памяти
запусков, а не в перекройке пайплайна. Стадия `expand` фиксирует результат
расширения, `ads_metrics` — обогащение, и так далее; если сценарий не выполнял
стадию (источник недоступен), она завершается со статусом `COMPLETED` и
контрольной точкой `{"skipped": true, "reason": "..."}`.

### 4. `src/google_keyword_ai/usecases/runs.py`

- `def run_show(settings, run_id) -> Envelope[RunRecord]` — нет такого
  запуска → `completeness=EMPTY` с причиной;
- `def run_list(settings, *, limit=20) -> Envelope[list[RunRecord]]`;
- `def run_export(settings, run_id) -> Envelope[dict[str, object]]` — полный
  снимок: запись, стадии, сохранённый конверт результата;
- `def run_resume(settings, run_id) -> Envelope[ResearchData]` — продолжает
  незавершённый запуск, пропуская годные стадии;
- `def run_rerun(settings, run_id) -> Envelope[ResearchData]` — выполняет то
  же исследование заново, **не** переиспользуя контрольные точки, и создаёт
  НОВЫЙ `run_id`, не затирая прежний.

### 5. Изменения в `usecases/research.py`

Добавить параметр `save_run: bool = False`. При `True`:

1. создаётся запись запуска со статусом `RUNNING` и стадиями в состоянии
   `PENDING` **до** обращения к источникам;
2. исследование выполняется через `RunExecutor`;
3. по завершении сохраняется конверт результата и статус `COMPLETED` либо
   `FAILED`;
4. `run_id` попадает в поле `run_id` конверта.

При `save_run=False` поведение не меняется: ничего не сохраняется, `run_id`
остаётся `None`. Сигнатура и поведение остальных параметров прежние.

### 6. Обёртки

- `cli/main.py`: флаг `--save-run` у `gkai research` и новая группа команд
  `gkai run list`, `gkai run show <id>`, `gkai run export <id>`,
  `gkai run resume <id>`, `gkai run rerun <id>` — у каждой `--format`.
  Существующие команды не менять.
- `mcp/server.py`: **не трогать**, новых инструментов в этой вехе нет.

### 7. `docs/runs.md`

Не длиннее 70 строк: зачем запускам память и почему кеша недостаточно; из чего
состоит запись; что такое отпечаток стадии и когда стадия считается годной для
пропуска; почему смена версии приложения или парсера обнуляет продолжение;
разница `resume` и `rerun`; что секреты в снимок конфигурации не попадают.

### 8. Тесты

- `tests/test_migrations_v2.py` — на свежей базе `user_version` равен `2` и обе
  таблицы созданы; **база версии 1 доводится до 2 без потери строк в
  `cache_entries`**; повторное применение ничего не делает; база версии 3
  отвергается.
- `tests/test_run_store.py` — создание, чтение, список в порядке «новые
  первыми», upsert стадии, удаление; **в `config_snapshot` нет значений
  секретов** (задать токен через окружение и убедиться, что его значения нет в
  сохранённом JSON); формат `run_id` соответствует регулярному выражению.
- `tests/test_executor.py` — стадии выполняются по порядку; при `resume`
  годная стадия пропускается и её контрольная точка переиспользуется; стадия с
  ДРУГИМ отпечатком выполняется заново; **смена `app_version` и смена
  `parser_version` по отдельности обнуляют продолжение** (два теста); отказ
  стадии даёт `FAILED` без выброса исключения; `attempts` растёт при повторе;
  стадия пропущенного источника получает `COMPLETED` с
  `{"skipped": true, ...}`.
- `tests/test_runs_usecase.py` — `show`/`export` для несуществующего запуска
  дают `empty` с причиной; `resume` не переигрывает годные стадии; `rerun`
  создаёт новый `run_id` и не трогает прежний; `research --save-run`
  проставляет `run_id` в конверте, а без флага он `None`.
- Дописать в `tests/test_cli.py` проверки `gkai run list` и
  `gkai research --save-run` на подделках.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** — ни
  alembic, ни чего-либо ещё. Механизм миграций уже есть.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/**` — читать, не изменять.
- `docs/superpowers/`, все `docs/specs/m1..m7*.md` и все существующие
  `docs/*.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- **Существующую миграцию №1 не изменять** ни при каких обстоятельствах:
  добавляется только новая, следующая по счёту.
- `providers/**`, `pipeline/scenarios.py`, `pipeline/budget.py`,
  `pipeline/models.py`, `opportunities.py`, `normalize.py`, `market.py`,
  `cache.py`, `http.py`, `ratelimit.py`, `mcp/server.py` — не менять.
  Правятся только `storage/migrations.py`, `usecases/research.py`,
  `cli/main.py` и создаются новые файлы из «Что делать».

  **Ровно одно исключение среди существующих тестов.** В
  `tests/test_storage.py` строка 33 утверждает
  `PRAGMA user_version == 1`. Эта веха поднимает схему до 2, поэтому число в
  ЭТОЙ строке надо обновить на актуальную `SCHEMA_VERSION` (лучше — сослаться
  на импортированную константу вместо литерала). Ничего другого в этом файле и
  ни одного другого существующего теста не менять: если приходится править
  что-то ещё — контракт принятой вехи сломан, остановись и доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (scoring, clustering, reports,
  skill).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/pipeline/runs.py`,
`src/google_keyword_ai/pipeline/executor.py`,
`src/google_keyword_ai/usecases/runs.py`, `tests/**` (кроме
`tests/fixtures/**`), `docs/runs.md`.

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
- **AC-004a.** Существующий тест PRAGMA обновлён под новую версию схемы и
  проходит.
  Проверка: `.venv/bin/pytest -q tests/test_storage.py -k pragma`
- **AC-004.** Схема поднимается до версии 2, обе таблицы создаются, а база
  версии 1 доводится до 2 без потери строк кеша.
  Проверка: `.venv/bin/pytest -q tests/test_migrations_v2.py`
- **AC-005.** В `config_snapshot` нет значений секретов, а `run_id` имеет
  объявленный формат.
  Проверка: `.venv/bin/pytest -q tests/test_run_store.py -k "secret or run_id"`
- **AC-006.** Запись результата стадии атомарна: статус `completed` без
  контрольной точки не сохраняется.
  Проверка: `.venv/bin/pytest -q tests/test_run_store.py -k atomic`
- **AC-007.** При `resume` годная стадия пропускается и её контрольная точка
  переиспользуется, а стадия с другим отпечатком выполняется заново.
  Проверка: `.venv/bin/pytest -q tests/test_executor.py -k "resume or fingerprint"`
- **AC-008.** Смена версии приложения и смена версии парсера **по
  отдельности** обнуляют продолжение. Проверяются оба случая поимённо.
  Проверка: `.venv/bin/pytest -q tests/test_executor.py -k "app_version or parser_version"`
- **AC-009.** Отказ стадии даёт `FAILED` без выброса исключения, `attempts`
  растёт при повторе.
  Проверка: `.venv/bin/pytest -q tests/test_executor.py -k "failed or attempts"`
- **AC-010.** `rerun` создаёт новый `run_id` и не изменяет прежний запуск.
  Проверка: `.venv/bin/pytest -q tests/test_runs_usecase.py -k rerun`
- **AC-011.** `research --save-run` проставляет `run_id` в конверте, а без
  флага он остаётся пустым и ничего не сохраняется.
  Проверка: `.venv/bin/pytest -q tests/test_runs_usecase.py -k save_run`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/specs/m3-expand.md docs/specs/m4-trends.md docs/specs/m5-google-ads.md docs/specs/m6-search-console.md docs/specs/m7-pipeline.md docs/autocomplete.md docs/expansion.md docs/trends.md docs/google-ads.md docs/search-console.md docs/pipeline.md tests/fixtures src/google_keyword_ai/providers src/google_keyword_ai/pipeline/scenarios.py src/google_keyword_ai/pipeline/budget.py src/google_keyword_ai/pipeline/models.py src/google_keyword_ai/opportunities.py src/google_keyword_ai/normalize.py src/google_keyword_ai/market.py src/google_keyword_ai/cache.py src/google_keyword_ai/http.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/mcp/server.py src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` зависнет на `tests/test_mcp_parity.py`, останови его,
>    отчитайся `not_attempted` с этой причиной и продолжай остальные критерии.
> 3. **Кредов Google Ads и Search Console нет ни у кого.** Все тесты — на
>    подделках.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Живой `gkai research --save-run` на реальной теме, затем
  `gkai run list`, `gkai run show`, `gkai run export` — запись читается, стадии
  на месте, секретов в снимке нет.
- **HC-003.** Обновление реальной базы версии 1 до версии 2 на живом файле,
  оставшемся от прошлых вех: строки кеша сохранились.
- **HC-004.** Мутационная проверка: сломать сравнение отпечатка, каждую из двух
  проверок версии, атомарность записи стадии, рост `attempts`, создание нового
  `run_id` в `rerun` и маскирование секретов в снимке.
- **HC-005.** Прервать живой запуск на середине и продолжить его: убедиться,
  что уже выполненные стадии не переигрываются.

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

- Создавать и править файлы в: `src/google_keyword_ai/pipeline/runs.py`,
  `src/google_keyword_ai/pipeline/executor.py`,
  `src/google_keyword_ai/usecases/runs.py`,
  `src/google_keyword_ai/usecases/research.py`,
  `src/google_keyword_ai/storage/migrations.py`,
  `src/google_keyword_ai/cli/main.py`, `tests/**` (кроме `tests/fixtures/**`),
  `docs/runs.md`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, изменять существующую миграцию №1, трогать `.venv/`,
  `.toolchain/`, `tests/fixtures/**` и код провайдеров, обращаться к настоящим
  API Google, писать файлы во временные каталоги в качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
