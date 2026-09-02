# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии — [SemVer](https://semver.org/lang/ru/).

Записи старше ~60 дней выносятся в `docs/worklog-archive/YYYY-MM.md`.

## [Unreleased]

### Исправлено

- `gkai research`, `gkai run resume` и `gkai run rerun` считали неполным любой
  прогон, чьё веерное расширение остановилось по достижении заданной глубины:
  `stopped_by="max_depth"` протекал в конвейер и давал `partial` с причиной
  «stopped by max_depth», из-за чего успешный прогон завершался кодом 1.
  Теперь, как и было задокументировано в `docs/expansion.md`, срезанным
  считается только бюджетный стоп (`max_queries`, `max_results`,
  `max_runtime`); достижение запрошенной глубины — полный результат.

### Документация

- Продуктовые доки (`docs/`) приведены к одному языку — английскому:
  переведены `runs.md`, `google-ads.md`, `trends.md`, из `pipeline.md` убран
  русский список.
- `docs/pipeline.md` больше не утверждает, что запуски не сохраняются, а
  скоринг появится позже: обе вехи давно сданы.
- Исправлен нерабочий пример `gkai trends` в справочнике скила (без `compare`
  он завершался с кодом 2), устаревшее утверждение README об отсутствии
  официального Trends API и неверное описание сериализации MCP.

## [0.1.0] — 2026-09-02

Первый релиз. Проект собран за десять вех, каждая — отдельная спека в
`docs/specs/`, реализация через codex-build и ручная приёмка.

### Добавлено

- **Каркас** (M1): конфигурация через pydantic-settings, единый конверт ответа
  (`schema_version`, `data`, `warnings`, `errors`, `completeness`,
  `completeness_reason`, `run_id`) — общая модель `Envelope` для CLI и MCP,
  равенство wire-представлений закреплено parity-тестами, структурированные ошибки, JSON-логи в stderr, SQLite с forward-only
  миграциями по `PRAGMA user_version`, команда `gkai doctor`.
- **Google Autocomplete** (M2): HTTP-слой с ретраями и джиттером, кеш ответов,
  нормализация запросов, учёт рынка (`hl`/`gl`), команда `gkai suggest`.
- **Веерное расширение** (M3): алфавитные и модификаторные шаблоны, обход в
  глубину с бюджетом, дедупликация, команда `gkai expand`.
- **Google Trends** (M4): неофициальный API (`explore` + `widgetdata`),
  таймсерии, гео-разрез и related queries, kill switch и circuit breaker,
  golden-фикстуры боевых ответов, команда `gkai trends`.
- **Google Ads Keyword Planner** (M5): провайдер поверх `google-ads`,
  межпроцессный троттлер на `fcntl.flock` под лимит 1 rps на CID, идеи и
  историческая статистика, команды `gkai ads ideas` и `gkai ads historical`.
- **Search Console** (M6): выгрузка запросов с пагинацией и общей квотой строк
  на весь диапазон дат, поиск возможностей по позиции и CTR, команды
  `gkai gsc properties`, `gkai gsc queries`, `gkai gsc opportunities`.
- **Три сценария исследования** (M7): ниша, конкурент, существующий сайт;
  бюджетный сторож, dry-run с планом стоимости, деградация до частичного
  результата вместо отказа; команды `gkai research`, `gkai competitor`.
- **Персистентные запуски** (M8): машина состояний стадий, отпечатки входов,
  продолжение и перезапуск, экспорт; схема БД версии 2, команды
  `gkai run list|show|export|resume|rerun`.
- **Оценка и кластеризация** (M9): прозрачный балл возможности 0–100 с разбором
  по компонентам, лексическая кластеризация по Жаккару, markdown-отчёты;
  команды `gkai score`, `gkai cluster`, `gkai explain-score`,
  `gkai niche analyze`, `gkai keyword inspect`.
- **Скил и документация** (M10): Claude-скил `researching-google-keywords`,
  README, `docs/architecture.md`, `docs/privacy.md`, `docs/mcp.md`, CI на
  Python 3.12 и 3.14, тест на расхождение документации с кодом.
- **MCP-сервер** `google-keyword-ai`: 14 инструментов поверх того же ядра,
  stdio-транспорт, паритет с CLI закреплён тестами.

### Известные ограничения

- Google Ads и Search Console не проверены на живых кредах: покрытие только
  моками. Кластеризация лексическая, без эмбеддингов. Интерактивного OAuth нет
  — только файлы кредов. Выгрузка GSC в BigQuery не реализована.
- Autocomplete и Trends используют неофициальные эндпоинты: Google может
  сменить формат без предупреждения.
