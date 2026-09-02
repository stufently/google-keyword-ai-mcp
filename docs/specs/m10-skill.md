# Спека: Claude Skill, README и документация (M10)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `10 из 10` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M9 приняты. Инструмент собран целиком: четыре источника данных, три
сценария исследования, бюджет, dry-run, запуски с продолжением, оценка,
кластеризация и markdown-отчёт. 299 зелёных тестов. **Читай существующий код и
опирайся на его типы, ничего в них не переписывая.**

Осталось последнее: сделать всё это пригодным к употреблению — Claude Skill,
README и документация проекта. **Код продукта в этой вехе не меняется вообще.**

## Проверенные факты

### Формат Agent Skills

Проверено 2026-09-02 по установленным на этой машине скилам. Frontmatter —
YAML между строками `---`, обязательные ключи `name` и `description`:

```yaml
---
name: researching-google-keywords
description: "..."
---
```

Встречающиеся дополнительные ключи: `allowed-tools`, `version`, `tags`,
`author`, `user_invocable`, `disable-model-invocation`. Обязательны только
первые два — остальные не выдумывать сверх нужного.

### Фактический состав CLI

`gkai --help` перечисляет ровно эти команды и группы:

```
doctor  suggest  expand  trends  competitor  research  score  cluster
explain-score  config  ads  gsc  run  niche  keyword
```

Внутри групп: `config show`; `ads ideas`, `ads historical`;
`gsc properties`, `gsc queries`, `gsc opportunities`;
`run list`, `run show`, `run export`, `run resume`, `run rerun`;
`niche analyze`; `keyword inspect`.

**Сверяйся с `gkai --help` и `gkai <группа> --help`, а не с памятью:**
описанная в README команда, которой нет, — это дефект документации.

### Фактический состав MCP

Инструментов 14. Проверяется командой (она же годится для сверки в тексте):

```
.venv/bin/python -c "from google_keyword_ai.config import Settings; from google_keyword_ai.mcp.server import build_server; print(sorted(t.name for t in build_server(Settings())._tool_manager.list_tools()))"
```

### Что уже задокументировано

В `docs/` уже лежат: `autocomplete.md`, `expansion.md`, `trends.md`,
`google-ads.md`, `search-console.md`, `pipeline.md`, `runs.md`, `scoring.md`.
Их **не переписывать**: новые документы на них ссылаются.

### Три запрета, которые скил обязан удерживать

Константы `TRENDS_CAVEAT`, `ADS_CAVEAT`, `SITE_SEED_CAVEAT` в
`pipeline/scenarios.py`. В SKILL.md они повторяются словами — это его главная
работа как оркестратора.

## Что делать

### 1. `.claude/skills/researching-google-keywords/SKILL.md`

Компактный, **заведомо меньше 200 строк**, с progressive disclosure: сам файл
описывает, когда применять скил и как выбрать workflow, а подробности выносит
в `reference/`.

Frontmatter:

```yaml
---
name: researching-google-keywords
description: "Researches Google keyword demand, long-tail queries, niche opportunity, competitor-derived keyword ideas, trends and Search Console opportunities using the gkai CLI. Use for SEO keyword research, niche analysis, Google search-demand analysis, keyword expansion, competitor keyword discovery, Google Ads Keyword Planner metrics, Google Trends analysis and Search Console query mining."
---
```

Содержание:

1. **Первый шаг всегда один:** `gkai doctor --format json` — узнать, какие
   провайдеры доступны. Не спрашивать пользователя, какой API использовать:
   выбор делается по намерению и по доступности.
2. **Выбор workflow по намерению:**
   - тема или фраза → `gkai research "<тема>" --language --country`;
   - домен или URL конкурента → `gkai research <домен>` (сценарий выбирается
     автоматически) либо `gkai competitor <домен>`;
   - свой сайт с подключённой Search Console → `gkai gsc opportunities <property>`.
3. **Дешёвое перед дорогим:** сначала `--dry-run`, чтобы показать план и
   стоимость, и только потом настоящий запуск; для больших исследований —
   `--save-run`, чтобы результат можно было разобрать без повторных запросов.
4. **Правила вывода** — четыре вида чисел и как о них говорить: абсолютные
   данные Google Ads, относительный интерес Trends, реальные показы Search
   Console, расчётные баллы `gkai`. Дословно три запрета.
5. **Как читать результат:** `completeness` (`complete` / `partial` / `empty`)
   и `warnings` — при `partial` обязательно сказать пользователю, чего не
   хватило; `data_quality` — источники и оговорки.
6. Ссылки на `reference/workflow.md`, `reference/metrics.md`,
   `reference/cli.md` и на примеры.

### 2. `reference/` внутри каталога скила

- `reference/workflow.md` — три сценария подробно: с чего начинать, какие
  команды в каком порядке, что делать при недоступном провайдере, когда
  использовать `--save-run` и команды `run *`.
- `reference/metrics.md` — что означает каждое поле результата, какие числа
  абсолютные, какие относительные, какие расчётные; составляющие балла и
  `confidence`; почему отсутствующая составляющая не считается нулём.
- `reference/cli.md` — справочник команд с примерами. **Сверить с
  `gkai --help`**, ничего не выдумывать.

### 3. `examples/` внутри каталога скила

- `examples/niche-research.md` — «Исследуй поисковый спрос по теме "аренда
  квартиры в Паттайе" для русского языка и Таиланда»: ожидаемая
  последовательность команд и как выглядит хороший ответ.
- `examples/competitor-research.md` — «Посмотри, какие keyword themes Google
  связывает с competitor.com»: обязательно показать, что это НЕ запросы, по
  которым сайт ранжируется.
- `examples/existing-site.md` — «Найди запросы моего сайта, где много показов,
  но страницу можно улучшить».

Каждый пример — сценарий на 30–60 строк: запрос пользователя, команды,
разбор ответа, чего говорить нельзя.

### 4. `.claude/skills/researching-google-keywords/evals.md`

Три сценария приёмки скила из примеров выше, оформленные проверяемо: вход,
ожидаемые действия, критерии хорошего ответа, типичные ошибки. Это документ,
а не исполняемый тест.

### 5. `README.md` — переписать целиком

Разделы: что делает проект; быстрый старт; установка; `gkai doctor`; базовое
исследование; исследование конкурента; workflow Search Console; MCP-сервер и
как подключить его к Claude Code; Claude Skill; ограничения.

Позиционирование дословно:

> Google Keyword Intelligence is an open-source CLI, MCP server and agent skill
> for collecting, enriching and analyzing Google search-demand data from Google
> Ads Keyword Planner, Autocomplete, Trends and Search Console.

Обязательный дисклеймер:

```
This project is not affiliated with or endorsed by Google.
Google and related product names are trademarks of their respective owners.
```

Обязательно назвать честно: Autocomplete и Trends — неофициальные,
недокументированные источники без гарантий; Google Ads и Search Console
опциональны и требуют собственных кредов; объёмы Google округляет и
объединяет близкие варианты.

Раздел ограничений включает: интерактивного OAuth-флоу нет (только файлы
кредов), BigQuery-выгрузка Search Console не реализована, кластеризация
лексическая, официальный Trends API недоступен.

### 6. `docs/architecture.md`

Не длиннее 120 строк: три слоя (ядро, две тонкие обёртки, скил); почему ядро
асинхронное; единый конверт ответа и запрет union-типов у инструментов MCP;
провайдеры и их общий интерфейс; кеш, троттлинг и межпроцессный лимит;
хранилище и миграции; порядок вех и что в какой сделано. Ссылки на уже
существующие `docs/*.md`.

### 7. `docs/privacy.md`

Не длиннее 60 строк: какие данные инструмент хранит локально (кеш ответов,
запуски, снимок конфигурации без секретов); что секреты живут в переменных
окружения и файлах кредов и в БД не попадают; что сырьё ответов провайдеров,
требующих авторизации, по умолчанию не сохраняется; где лежит база и как её
удалить; что запросы уходят в Google и на что это влияет.

### 8. `docs/mcp.md`

Не длиннее 60 строк: как запустить сервер, точный список инструментов
(**сверить командой из «Проверенных фактов»**), пример конфигурации для
подключения к Claude Code, и что вывод инструмента совпадает с
`gkai ... --format json`.

### 9. `.github/workflows/ci.yml`

Прогон на CPython **3.12 и 3.14** (нижняя граница `requires-python` — это
обещание, и проверять его одной версией нельзя): `uv sync`, `ruff check`,
`ruff format --check`, `mypy`, `pytest -q`. Без публикации и без секретов.

### 10. Тест на согласованность документации

`tests/test_docs.py`:

- у SKILL.md корректный YAML-frontmatter с непустыми `name` и `description`,
  а `name` равен `researching-google-keywords`;
- SKILL.md короче 200 строк;
- все файлы `reference/` и `examples/`, упомянутые в SKILL.md, существуют;
- **каждая команда верхнего уровня из `gkai --help` упоминается в
  `reference/cli.md`** — документация не отстаёт от кода;
- **каждый инструмент MCP из `build_server` упомянут в `docs/mcp.md`**;
- README содержит дисклеймер о неаффилированности с Google.

Этот тест — то, что не даст документации разойтись с кодом молча.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять.**
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `tests/fixtures/**`.
- `docs/superpowers/`, все `docs/specs/*.md`, и **все существующие
  `docs/*.md`** (`autocomplete.md`, `expansion.md`, `trends.md`,
  `google-ads.md`, `search-console.md`, `pipeline.md`, `runs.md`,
  `scoring.md`) — читать можно, править нельзя.
- 🚨 **Весь код продукта — `src/google_keyword_ai/**` — не менять ни одной
  строкой.** Эта веха документационная. Если документация расходится с кодом,
  правится документация. Единственное исключение — если тест на согласованность
  вскроет НАСТОЯЩИЙ дефект кода: тогда остановись и доложи, не правь код сам.
- Существующие 299 тестов должны остаться зелёными без правок.
- Не создавать файлов, которых нет в разделе «Что делать».
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:**
`.claude/skills/researching-google-keywords/**`, `README.md`,
`docs/architecture.md`, `docs/privacy.md`, `docs/mcp.md`,
`.github/workflows/ci.yml`, `tests/test_docs.py`.

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
- **AC-004.** У SKILL.md корректный frontmatter с нужным `name` и непустым
  `description`, и он короче 200 строк.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k frontmatter`
- **AC-005.** Все файлы `reference/` и `examples/`, упомянутые в SKILL.md,
  существуют.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k referenced`
- **AC-006.** Каждая команда верхнего уровня CLI упомянута в
  `reference/cli.md`.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k cli`
- **AC-007.** Каждый инструмент MCP упомянут в `docs/mcp.md`.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k mcp`
- **AC-008.** README содержит дисклеймер о неаффилированности с Google.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k disclaimer`
- **AC-009.** Файл CI существует и гоняет обе версии Python.
  Проверка: `.venv/bin/python -c "import pathlib,sys; text=pathlib.Path('.github/workflows/ci.yml').read_text(); sys.exit(0 if '3.12' in text and '3.14' in text else 1)"`
- **AC-010.** Каталог скила лежит по требуемому пути и содержит SKILL.md,
  `reference/` и `examples/`.
  Проверка: `.venv/bin/python -c "import pathlib,sys; base=pathlib.Path('.claude/skills/researching-google-keywords'); sys.exit(0 if (base/'SKILL.md').is_file() and (base/'reference').is_dir() and (base/'examples').is_dir() else 1)"`
- **AC-011.** Три запрета присутствуют в SKILL.md дословно по смыслу: про
  Trends, про Ads competition и про site seed.
  Проверка: `.venv/bin/pytest -q tests/test_docs.py -k caveats`
- **AC-012.** Код продукта не изменён ни одной строкой, как и все ранее
  принятые файлы.
  Проверка: `git diff --exit-code -- src pyproject.toml uv.lock AGENTS.md .gitignore .dockerignore docs/superpowers docs/specs docs/autocomplete.md docs/expansion.md docs/trends.md docs/google-ads.md docs/search-console.md docs/pipeline.md docs/runs.md docs/scoring.md tests/fixtures`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` зависнет на `tests/test_mcp_parity.py`, останови его,
>    отчитайся `not_attempted` с этой причиной и продолжай остальные критерии.
>    Для `tests/test_docs.py` транспорт не нужен: список инструментов берётся
>    из `build_server(...)._tool_manager.list_tools()` без запуска сессии.
> 3. **Кредов Google Ads и Search Console нет ни у кого.** Примеры в
>    документации пишутся так, чтобы читатель понимал: без кредов эти шаги
>    выдают `empty` с причиной, а не падают.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Прочитать SKILL.md и примеры глазами: команды существуют,
  формулировки не нарушают три запрета, объём разумный.
- **HC-003.** Пройти README как читатель: выполнить quick start с нуля в
  чистом контейнере и убедиться, что описанные команды работают.
- **HC-004.** Подключить MCP-сервер к Claude Code по инструкции из
  `docs/mcp.md` и вызвать инструмент.
- **HC-005.** Проверить, что тест согласованности реально ловит расхождение:
  временно убрать команду из `reference/cli.md` и убедиться, что тест краснеет.

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

- Создавать и править файлы в:
  `.claude/skills/researching-google-keywords/**`, `README.md`,
  `docs/architecture.md`, `docs/privacy.md`, `docs/mcp.md`,
  `.github/workflows/ci.yml`, `tests/test_docs.py`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `src/**`, `pyproject.toml`,
  `uv.lock`, существующие `docs/*.md`, трогать `.venv/`, `.toolchain/` и
  `tests/fixtures/**`, обращаться к настоящим API Google.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
