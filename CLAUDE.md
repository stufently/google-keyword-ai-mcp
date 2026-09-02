# CLAUDE.md — google-keyword-ai-mcp

CLI, MCP-сервер и Claude-скил для сбора и анализа поискового спроса Google.

Рабочие правила прогона (команды, запреты, Docker) — в `AGENTS.md`.
Здесь — структура и архитектурные решения.

## Точки входа

| Что | Как запускается |
|---|---|
| CLI | `gkai` → `google_keyword_ai.cli.main:main` |
| MCP-сервер | `google-keyword-ai` → `google_keyword_ai.mcp.server:main`, stdio |
| Скил | `.claude/skills/researching-google-keywords/` |

Оба интерфейса — равноправные фасады над одним ядром, а не обёртки друг друга.

## Структура

```
src/google_keyword_ai/
  config.py errors.py logging.py market.py envelope.py   # каркас
  http.py ratelimit.py cache.py normalize.py             # инфраструктура
  expansion.py scoring.py clustering.py opportunities.py # алгоритмы
  storage/     engine.py migrations.py                   # SQLite, user_version
  providers/   autocomplete.py expander.py google_ads.py search_console.py
               trends/{models,unofficial,official,provider}.py
  pipeline/    budget.py models.py scenarios.py runs.py executor.py
  usecases/    doctor suggest expand trends ads gsc research runs analysis
  reports/     markdown.py
  cli/main.py  mcp/server.py  data/{alphabets,modifiers}
```

Слои сверху вниз: `cli`/`mcp` → `usecases` → `pipeline` → `providers` →
`http`/`cache`/`ratelimit`. Обратных зависимостей нет: провайдер не знает про
сценарии, сценарий не знает про интерфейс.

## Ключевые решения

- **Async-first ядро, синхронные фасады.** Внутри всё на `anyio`; CLI
  оборачивает через `anyio.run`, а функции MCP-инструментов синхронные —
  SDK сам уносит их в рабочий поток.
- **Единый конверт ответа.** `schema_version`, `data`, `warnings`, `errors`,
  `completeness`, `completeness_reason`, `run_id`. Модель `Envelope` общая,
  сериализация — нет: CLI зовёт `to_wire()`, MCP отдаёт `Envelope` в SDK.
  Равенство wire-представлений держится на `tests/test_mcp_parity.py`.
- **Дешёвое раньше дорогого.** Autocomplete и расширение бесплатны и идут
  первыми; Ads и GSC — по явному запросу и под бюджетом.
- **Частичный результат вместо отказа.** Отвалившийся провайдер даёт
  `completeness=partial` с причиной, а не исключение наружу.
- **Три сценария вместо одной цепочки:** ниша, конкурент, существующий сайт.
- **Запуски персистентны:** стадии, отпечатки входов, продолжение и перезапуск.
- **Миграции forward-only** через `PRAGMA user_version`, без alembic;
  WAL + `busy_timeout` + `foreign_keys` выставляются явно.
- **Троттлинг межпроцессный** (`fcntl.flock`) там, где лимит на аккаунт, а не
  на процесс — Google Ads 1 rps/CID.
- **Скоринг прозрачный:** балл 0–100 раскладывается по компонентам,
  `gkai explain-score` показывает разбор для конкретного запроса.

## Провайдеры

| Провайдер | Статус | Креды |
|---|---|---|
| Autocomplete | неофициальный, работает | не нужны |
| Trends | неофициальный, kill switch + circuit breaker | не нужны |
| Google Ads Keyword Planner | реализован, вживую не проверен | developer token |
| Search Console | реализован, вживую не проверен | OAuth-файл |

Неофициальные эндпоинты могут сломаться при смене формата у Google — на этот
случай в `tests/fixtures/trends/` лежат боевые ответы как golden-фикстуры.

## Документация

`docs/architecture.md` — как всё устроено, `docs/mcp.md` — инструменты,
`docs/privacy.md` — что и где хранится, `docs/{autocomplete,expansion,trends,
google-ads,search-console,pipeline,runs,scoring}.md` — по подсистемам,
`docs/specs/` — спеки вех, `docs/superpowers/specs/` — общий дизайн.
Изменения — в `CHANGELOG.md`.
