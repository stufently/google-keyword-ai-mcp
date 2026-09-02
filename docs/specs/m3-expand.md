# Спека: веерное расширение семантики (M3)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `3 из 9` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1 и M2 приняты. В дереве уже есть каркас (`config.py`, `errors.py`,
`logging.py`, `market.py`, `envelope.py`, `storage/`, `usecases/doctor.py`,
`cli/main.py`, `mcp/server.py`) и инфраструктура с первым провайдером
(`http.py` с ретраями, `ratelimit.py`, `cache.py`, `normalize.py`,
`providers/base.py`, `providers/autocomplete.py`, `usecases/suggest.py`),
64 зелёных теста. **Читай этот код и опирайся на его типы, не переписывая их.**

Эта веха превращает одиночную подсказку в сбор семантики: из одного seed'а
делается много запросов к автокомплиту по нескольким стратегиям, результат
нормализуется, дедуплицируется и возвращается одним конвертом.

**Чего в этой вехе НЕТ:** Google Trends (M4), Google Ads (M5), Search Console
(M6), пайплайн `research` и runs (M7), scoring и кластеризация (M8). Не
реализовывай их и не создавай под них файлы.

## Проверенные факты

- `AutocompleteProvider.suggest(query, market, *, limit=None) -> list[Suggestion]`
  уже существует и сам делает «кеш → троттлер → HTTP → запасной endpoint».
  Расширение обязано вызывать именно его, а не ходить в сеть напрямую.
- `Suggestion` — pydantic-модель с полями `text`, `relevance: int | None`,
  `source`, `retrieved_at`.
- `normalize.py` уже даёт `normalize_keyword(text, *, collapse_punctuation=False)`,
  модель `KeywordCandidate(raw, normalized, discovered_from, relevance)` и
  `deduplicate(candidates)`, который объединяет `discovered_from` без потерь и
  берёт максимальную известную `relevance`.
- `Market.autocomplete_params()` возвращает `{"hl": язык, "gl": страна}`;
  поддерживаемые языки перечислены в `market.py` в `SUPPORTED_LANGUAGES`.
- `Envelope[T]` требует `completeness_reason`, когда `completeness` не
  `complete`.
- Инструменты MCP регистрируются **синхронными** функциями: SDK сам уводит их
  в поток. Асинхронная функция блокировала бы event loop.
- Сети в песочнице нет: все тесты — на `respx`.
- Русский алфавит для веера — 33 буквы `абвгдеёжзийклмнопрстуфхцчшщъыьэюя`.
  Английский — 26 букв `abcdefghijklmnopqrstuvwxyz`. Это данные, а не
  предположение: они фиксируются в файлах и покрываются тестом на длину.

### Арифметика веера, из которой берутся ограничения

Для русского seed'а один проход даёт: 33 суффиксных запроса + 33 префиксных +
10 цифровых + N модификаторов. При N≈30 это **около 106 запросов на один seed**.
При глубине 2 и 10 подсказках на запрос счёт уходит в десятки тысяч —
поэтому предохранители не пожелание, а обязательная часть вехи.

## Что делать

### 1. Данные: `src/google_keyword_ai/data/`

Создать пакет с данными ВНУТРИ пакета кода (из корня репозитория они не попали
бы в wheel).

- `src/google_keyword_ai/data/__init__.py` — пустой.
- `src/google_keyword_ai/data/alphabets/ru.txt` — 33 буквы русского алфавита,
  по одной в строке, строчные.
- `src/google_keyword_ai/data/alphabets/en.txt` — 26 букв латиницы.
- `src/google_keyword_ai/data/modifiers/ru.toml` и `.../en.toml` — модификаторы
  по категориям. Структура файла:

  ```toml
  [informational]
  prefix = ["как", "что", "почему", "зачем", "какой", "какая", "какие", "где"]
  suffix = ["это", "инструкция", "своими руками"]

  [commercial]
  prefix = ["купить", "заказать"]
  suffix = ["цена", "стоимость", "отзывы", "недорого", "дёшево", "рейтинг"]

  [comparison]
  suffix = ["или", "vs", "сравнение", "альтернатива", "аналог", "лучший"]

  [transactional]
  prefix = ["заказать"]
  suffix = ["рядом", "доставка", "онлайн", "срочно"]
  ```

  Для английского — осмысленный аналог. Категории те же четыре:
  `informational`, `commercial`, `comparison`, `transactional`. У категории
  может быть только `prefix`, только `suffix` или оба.

### 2. `src/google_keyword_ai/expansion.py`

Чистая, детерминированная генерация запросов — **без сети**, чтобы её можно
было полностью проверить тестами.

- `def load_alphabet(language: str) -> list[str]` — читает файл алфавита через
  `importlib.resources`. Для языка без своего файла возвращает английский
  алфавит (задокументировать это как осознанный запасной вариант, а не молча).
- `def load_modifiers(language: str) -> dict[str, dict[str, list[str]]]` —
  аналогично, с запасным английским набором.
- `class ExpansionStrategy(StrEnum)`: `SUFFIX_ALPHABET`, `PREFIX_ALPHABET`,
  `DIGITS`, `MODIFIERS`.
- `class ExpansionQuery` (pydantic, frozen): `text: str`,
  `strategy: ExpansionStrategy`, `seed: str`.
- `def build_queries(seed: str, language: str, strategies: Sequence[ExpansionStrategy]) -> list[ExpansionQuery]`:
  - `SUFFIX_ALPHABET` → `f"{seed} {буква}"` по всему алфавиту;
  - `PREFIX_ALPHABET` → `f"{буква} {seed}"`;
  - `DIGITS` → `f"{seed} {цифра}"` для `0`–`9`;
  - `MODIFIERS` → для каждой категории `prefix` даёт `f"{модификатор} {seed}"`,
    `suffix` даёт `f"{seed} {модификатор}"`;
  - результат дедуплицирован по `text` с сохранением порядка появления;
  - сам `seed` в список НЕ входит: его запрашивают отдельно.

### 3. `src/google_keyword_ai/providers/expander.py`

- `class ExpansionLimits` (pydantic): `max_depth: int = 1`,
  `max_queries: int = 500`, `max_results: int = 2000`,
  `max_runtime_seconds: float = 120.0`. Валидация: все значения
  положительные, иначе `InvalidConfigurationError`.
- `class ExpansionStats` (pydantic): `queries_executed: int`,
  `depth_reached: int`, `stopped_by: str | None` — что именно остановило веер
  (`"max_queries"`, `"max_results"`, `"max_runtime"`, `"max_depth"` или `None`,
  если веер исчерпался сам).
- `class KeywordExpander` с конструктором от `AutocompleteProvider` и
  `ExpansionLimits`:
  - `async def expand(self, seed: str, market: Market, *, strategies: Sequence[ExpansionStrategy]) -> tuple[list[KeywordCandidate], ExpansionStats]`.

Алгоритм, буквально:

1. Очередь seed'ов начинается с исходного seed'а на глубине 0.
2. Для каждого seed'а сначала запрашивается он сам, затем запросы из
   `build_queries`.
3. Каждый запрос идёт через `AutocompleteProvider.suggest`; счётчик
   `queries_executed` увеличивается на каждый ВЫПОЛНЕННЫЙ запрос.
4. Каждая подсказка превращается в `KeywordCandidate` с
   `raw=подсказка`, `normalized=normalize_keyword(подсказка)`,
   `discovered_from=[f"autocomplete:{стратегия}:{текст запроса}"]`,
   `relevance` из подсказки.
5. После обхода глубины результат дедуплицируется через `deduplicate`.
6. Если текущая глубина меньше `max_depth`, новые (ранее не встречавшиеся)
   нормализованные ключи становятся seed'ами следующей глубины.

Предохранители проверяются **перед** выполнением очередного запроса и
останавливают веер немедленно, с заполнением `stopped_by`:

- `queries_executed >= max_queries` → `"max_queries"`;
- число уникальных кандидатов `>= max_results` → `"max_results"`;
- прошло `>= max_runtime_seconds` (замерять `anyio.current_time()`, а не
  системными часами) → `"max_runtime"`;
- достигнута `max_depth` → `"max_depth"` (только если очередь следующей
  глубины была непуста, иначе веер исчерпался сам и `stopped_by is None`).

🚨 Остановка по предохранителю — это НЕ ошибка: уже собранные кандидаты
возвращаются, исключение не бросается.

Отказ отдельного запроса (любая `GkaiError`) не убивает весь веер: запрос
пропускается, работа продолжается. Но если **первый же** запрос по самому
seed'у упал, ошибка выпускается наружу — иначе пользователь получит пустой
результат вместо честной причины.

### 4. `src/google_keyword_ai/usecases/expand.py`

- `class ExpandData` (pydantic): `seed`, `language`, `country`,
  `provider: ProviderInfo`, `strategies: list[str]`, `limits: ExpansionLimits`,
  `stats: ExpansionStats`, `keywords: list[KeywordCandidate]`;
- `def run_expand(settings, seed, *, language=None, country=None, depth=None, max_queries=None, max_results=None, max_runtime_seconds=None, strategies=None, limit=None) -> Envelope[ExpandData]`
  — синхронный фасад по образцу `run_suggest`: строит `Market`, открывает БД,
  собирает провайдера и запускает работу через `anyio.run`.
  - `strategies=None` означает все четыре;
  - `limit` обрезает итоговый список ключей (после дедупликации), не влияя на
    `stats`;
  - при `RateLimitError`, `NetworkError`, `ApiError`,
    `ProviderUnavailableError` — конверт с пустым списком,
    `completeness=EMPTY`, заполненными `errors` и `completeness_reason`;
    исключение наружу НЕ выпускать;
  - если веер остановлен предохранителем, но кандидаты есть —
    `completeness=PARTIAL` и `completeness_reason` вида
    `"stopped by max_queries"`;
  - пустой результат без ошибки — `EMPTY` с причиной `"no keywords"`.

### 5. Обёртки

- `cli/main.py`: команда
  `gkai expand <seed> [--language] [--country] [--depth] [--max-queries] [--max-results] [--max-runtime] [--strategy ... (повторяемый)] [--limit] [--format json|table]`.
  Существующие команды не менять.
- `mcp/server.py`: **синхронный** инструмент
  `expand_keywords(seed, language=None, country=None, depth=None, max_queries=None, max_results=None, max_runtime_seconds=None, strategies=None, limit=None) -> Envelope[ExpandData]`.
  Инструменты `doctor` и `suggest_keywords` не менять.

### 6. `docs/expansion.md`

Не длиннее 60 строк: какие стратегии есть, откуда берутся алфавиты и
модификаторы, как их дополнить своим языком, арифметика числа запросов,
что означают предохранители и почему остановка по ним — не ошибка.

### 7. Тесты

- `tests/test_expansion.py` — длина русского алфавита 33 и английского 26;
  `build_queries` для каждой стратегии даёт ожидаемые строки; дедупликация
  внутри `build_queries`; язык без своего файла берёт английский набор;
  seed в список запросов не попадает.
- `tests/test_expander.py` (на `respx`): веер обходит все стратегии; каждый
  предохранитель срабатывает и проставляет свой `stopped_by` (по одному тесту
  на `max_queries`, `max_results`, `max_runtime`, `max_depth`); отказ одного
  запроса не роняет веер; отказ на самом seed'е выпускает ошибку;
  `discovered_from` содержит стратегию и текст запроса; глубина 2 действительно
  использует найденные ключи как seed'ы.
- `tests/test_expand_usecase.py` — конверт при успехе; `PARTIAL` при остановке
  предохранителем с причиной; `EMPTY` при отказе провайдера; `limit` режет
  ключи, но не `stats`.
- Дописать в `tests/test_cli.py` проверку `gkai expand` на моках и в
  `tests/test_mcp_parity.py` — parity для `expand_keywords`
  (использовать существующую фикстуру `thread_offload`).

Тесты не должны ходить в сеть ни при каких условиях. Замедления не допускать:
`anyio.sleep` в тестах подменять, как это уже сделано в тестах M2.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** — всё нужное
  установлено. Не хватает библиотеки — остановись и доложи.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `docs/superpowers/`, `docs/specs/m1-scaffold.md`, `docs/specs/m2-autocomplete.md`,
  `docs/autocomplete.md` — читать можно, править нельзя.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- Код вех M1 и M2 менять НЕ нужно. `cli/main.py` и `mcp/server.py` правятся
  только добавлением новой команды и нового инструмента. Существующие 64 теста
  должны остаться зелёными без правок: если приходится править существующий
  тест, значит сломан контракт принятой вехи — остановись и доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (trends, google_ads,
  search_console, pipeline, scoring, clustering).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/**`, `tests/**` и
`docs/expansion.md` — только перечисленные пути.

**Если разрешённого способа не остаётся** — остановись и доложи по разделу
«Контракт на невыполнимое». Создавать доказательства во временных каталогах
(`/tmp` и подобных) запрещено.

## Критерии приёмки

- **AC-001.** Линтер чист.
  Проверка: `.venv/bin/ruff check .`
- **AC-002.** Проверка типов в строгом режиме проходит.
  Проверка: `.venv/bin/mypy`
- **AC-003.** Весь тестовый корпус зелёный, включая 64 теста вех M1 и M2.
  **Критерий агрегирующий: при его провале остальные критерии всё равно
  выполнить и отчитаться по каждому.**
  Проверка: `.venv/bin/pytest -q`
- **AC-004.** Алфавиты загружаются из данных пакета: русский — 33 буквы,
  английский — 26, неизвестный язык берёт английский.
  Проверка: `.venv/bin/pytest -q tests/test_expansion.py -k alphabet`
- **AC-005.** `build_queries` порождает ожидаемые строки для всех четырёх
  стратегий и не включает сам seed.
  Проверка: `.venv/bin/pytest -q tests/test_expansion.py -k queries`
- **AC-006.** Каждый из четырёх предохранителей останавливает веер и
  проставляет соответствующий `stopped_by`. **Все четыре проверяются
  поимённо**, одного общего теста недостаточно.
  Проверка: `.venv/bin/pytest -q tests/test_expander.py -k "max_queries or max_results or max_runtime or max_depth"`
- **AC-007.** Отказ отдельного запроса не роняет веер, а отказ на самом seed'е
  выпускает ошибку наружу.
  Проверка: `.venv/bin/pytest -q tests/test_expander.py -k failure`
- **AC-008.** `discovered_from` у найденного ключа содержит и стратегию, и
  текст запроса, которым он найден; при повторной находке источники
  объединяются.
  Проверка: `.venv/bin/pytest -q tests/test_expander.py -k discovered`
- **AC-009.** Глубина 2 использует найденные на первой глубине ключи как
  seed'ы.
  Проверка: `.venv/bin/pytest -q tests/test_expander.py -k depth`
- **AC-010.** Остановка предохранителем даёт `completeness=partial` с
  причиной, отказ провайдера — `empty` без выброса исключения.
  Проверка: `.venv/bin/pytest -q tests/test_expand_usecase.py`
- **AC-011.** `gkai expand` печатает конверт, MCP-инструмент `expand_keywords`
  отдаёт тот же payload, а `doctor` и `suggest` продолжают работать.
  Проверка: `.venv/bin/pytest -q tests/test_cli.py tests/test_mcp_parity.py`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/autocomplete.md src/google_keyword_ai/market.py src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/storage src/google_keyword_ai/http.py src/google_keyword_ai/cache.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/normalize.py src/google_keyword_ai/providers/autocomplete.py`

> Примечание для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения, `git add` падает с `index.lock: Read-only file system`. Это
>    ожидаемо, коммит делает принимающий; блокирующим обстоятельством не
>    считать.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.**
>    Инструменты синхронные, SDK уводит их в поток, а пробуждение event loop из
>    чужого потока песочница не пропускает — ровно как и сокеты. В
>    `tests/conftest.py` для этого уже есть фикстура `thread_offload`, которая
>    пропускает такой тест: используй её в новых MCP-тестах. Если AC-011 или
>    AC-003 всё же зависнут — останови их, отчитайся `not_attempted` с этой
>    причиной и продолжай. Делать инструмент асинхронным ЗАПРЕЩЕНО: это
>    заблокирует event loop в бою.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Живой веер по настоящему seed'у с малыми лимитами — проверить,
  что запросов уходит столько, сколько обещано, и что троттлер их разносит.
- **HC-003.** Мутационная проверка: по очереди отключить каждый предохранитель,
  дедупликацию, объединение `discovered_from`, переход на следующую глубину и
  пропуск упавшего запроса — каждый раз должен падать именно свой тест.
- **HC-004.** Сборка wheel и проверка, что файлы алфавитов и модификаторов
  ПОПАЛИ в дистрибутив и читаются из установленного пакета.
- **HC-005.** Живой вызов `expand_keywords` по настоящему stdio.

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
  `docs/expansion.md` — только по путям из раздела «Что делать».
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет. Все тесты — на моках `respx`.
- Git: коммит невозможен (read-only `.git`); ветку не переключать, ребейз не
  делать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/` и `.toolchain/`, обращаться к
  настоящим эндпоинтам Google, писать файлы во временные каталоги в качестве
  доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
