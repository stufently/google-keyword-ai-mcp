# Спека: три сценария исследования (M7)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `7 из 10` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M6 приняты: каркас, HTTP-слой, кеш, нормализация, и все четыре
источника — Autocomplete, веерное расширение, Google Trends, Google Ads,
Search Console. 218 зелёных тестов. **Читай существующий код и опирайся на его
типы, не переписывая их.**

Эта веха соединяет источники в исследование: пользователь называет тему, домен
конкурента или свой сайт, а инструмент сам выбирает порядок обращений,
удерживается в бюджете и отдаёт один связный ответ.

**Чего в этой вехе НЕТ:**
- **никакой persistence запусков**: ни таблиц `runs`, ни `run show/export/resume`,
  ни машины состояний — это целиком веха M8;
- **никакого scoring и кластеризации** — это веха M9. Список ключей выходит
  плоским;
- Claude Skill и README — веха M10.

Не реализовывай их и не создавай под них файлы.

## Проверенные факты

Всё перечисленное уже есть в дереве и проверено прогоном тестов.

### Готовые кирпичи

- `AutocompleteProvider.suggest(query, market, *, limit=None) -> list[Suggestion]`;
- `KeywordExpander.expand(seed, market, *, strategies) -> tuple[list[KeywordCandidate], ExpansionStats]`
  с `ExpansionLimits(max_depth, max_queries, max_results, max_runtime_seconds)`
  и `ExpansionStats(queries_executed, depth_reached, stopped_by)`;
- `GoogleTrendsProvider.fetch(keywords, *, geo, timeframe, hl) -> TrendsResult`;
- `GoogleAdsProvider.keyword_ideas(seed, market, *, include_adult=False)` и
  `.historical_metrics(keywords, market)`, оба возвращают `list[KeywordIdea]`
  с `KeywordMetrics`; `AdsSeed(keywords, url, site)` и его `mode()`;
- `SearchConsoleProvider.query(...) -> SearchAnalyticsPage` и
  `.list_properties()`; `find_opportunities(rows, settings) -> list[Opportunity]`;
- у каждого провайдера есть `info -> ProviderInfo` и `is_available() -> bool`;
- `normalize_keyword`, `KeywordCandidate`, `deduplicate`;
- `Envelope[T]` с `completeness`, `completeness_reason`, `warnings`, `errors`;
  при `completeness != complete` причина обязательна;
- `Market.parse(language, country)`.

### Ограничения, которые нельзя нарушать

- **Cheap-first.** Порядок обращения к источникам фиксирован:
  кеш → бесплатное (Autocomplete) → дедупликация и отсев → Google Ads →
  Trends. В Ads уходит только то, что пережило отсев, а не все комбинации.
- **Google Ads и Search Console опциональны.** Кредов нет ни у кого, включая
  принимающего. Исследование обязано завершаться и без них, помечая это в
  `warnings`, а не падать.
- **Отказ одного провайдера не уничтожает исследование.** Частичный результат —
  это `completeness=partial` с причиной, а не исключение.
- **Три запрета в формулировках:** значения Trends 0–100 — не объём поиска;
  `ads_competition` — не SEO-сложность; site seed даёт «идеи ключей, которые
  Google связывает с сайтом», а не «запросы, по которым сайт ранжируется».
- Инструменты MCP регистрируются **синхронными** функциями.
- Сети в песочнице нет: все тесты — на подделках провайдеров.

## Что делать

### 1. `src/google_keyword_ai/pipeline/__init__.py`

Пустой.

### 2. `src/google_keyword_ai/pipeline/budget.py`

- `class Budget` (pydantic): `max_keywords: int = 2000`,
  `max_autocomplete_queries: int = 500`, `max_ads_calls: int = 20`,
  `max_trends_calls: int = 3`, `max_runtime_seconds: float = 300.0`.
  Валидация: все значения положительные, иначе `InvalidConfigurationError`.
- `class BudgetSpend` (pydantic): `autocomplete_queries: int = 0`,
  `ads_calls: int = 0`, `trends_calls: int = 0`, `keywords: int = 0`,
  `elapsed_seconds: float = 0.0`.
- `class BudgetGuard`: конструктор от `Budget`; методы
  `can_spend(kind: str, amount: int = 1) -> bool`, `spend(kind, amount=1)`,
  `exhausted_reason() -> str | None`, свойство `spend -> BudgetSpend`.
  `kind` — одно из `autocomplete`, `ads`, `trends`, `keywords`.
  Неизвестный `kind` → `InvalidConfigurationError`.
  Время считать через `anyio.current_time()`, а не системными часами.

Исчерпание бюджета — **не ошибка**: сценарий останавливается, уже собранное
возвращается, причина попадает в `stopped_by`.

### 3. `src/google_keyword_ai/pipeline/models.py`

- `class SourceUsage` (pydantic): `name: str`, `used: bool`,
  `available: bool`, `detail: str`;
- `class ResearchKeyword` (pydantic): `keyword: str`, `normalized: str`,
  `discovered_from: list[str]`, `autocomplete_relevance: int | None = None`,
  `avg_monthly_searches: int | None = None`,
  `ads_competition: str | None = None`,
  `ads_competition_index: int | None = None`,
  `low_top_of_page_bid: float | None = None`,
  `high_top_of_page_bid: float | None = None`,
  `gsc_impressions: int | None = None`, `gsc_clicks: int | None = None`,
  `gsc_ctr: float | None = None`, `gsc_position: float | None = None`;
- `class ResearchStats` (pydantic): `expansion: ExpansionStats | None = None`,
  `spend: BudgetSpend`, `stopped_by: str | None = None`;
- `class DataQuality` (pydantic): `sources: list[SourceUsage]`,
  `retrieved_at: datetime`, `absolute_metrics: list[str]`,
  `relative_metrics: list[str]`, `derived_metrics: list[str]`,
  `caveats: list[str]`;
- `class ResearchData` (pydantic): `scenario: str`, `input: str`,
  `language: str`, `country: str`, `keywords: list[ResearchKeyword]`,
  `trends: TrendsResult | None = None`,
  `opportunities: list[Opportunity] = []`,
  `stats: ResearchStats`, `data_quality: DataQuality`;
- `class DryRunPlan` (pydantic): `scenario: str`, `steps: list[str]`,
  `estimated_autocomplete_queries: int`, `estimated_ads_calls: int`,
  `estimated_trends_calls: int`, `sources: list[SourceUsage]`.

🚨 `caveats` заполняется всегда и содержит три запрета из «Проверенных фактов»
дословно, когда соответствующий источник участвовал.

### 4. `src/google_keyword_ai/pipeline/scenarios.py`

Три сценария. У каждого — своя точка входа и свой порядок, общий цикл им не
навязывается.

`class ScenarioContext` — контейнер с `settings`, `market`, `budget_guard`,
провайдерами (любой может быть `None`, если недоступен) и `expander`.

**`NewNicheResearch(seed)`** — тема с нуля:

1. веерное расширение через `KeywordExpander` (лимиты выводятся из бюджета:
   `max_queries = budget.max_autocomplete_queries`,
   `max_results = budget.max_keywords`);
2. дедупликация и отсев мусора: убрать пустые, состоящие из одного символа и
   совпадающие с seed;
3. отобрать кандидатов для Ads: первые `N` по релевантности автокомплита,
   где `N` ограничено `max_ads_calls * 20` (Google принимает до 20 ключей за
   запрос исторических метрик) — и только их отправить в
   `historical_metrics`, батчами по 20;
4. Trends — **один** запрос по самому seed'у, если провайдер доступен и бюджет
   позволяет.

**`CompetitorResearch(target, seed_keyword=None)`** — сайт конкурента:

1. `GoogleAdsProvider.keyword_ideas` с `site_seed` для голого домена,
   `url_seed` для URL с путём, `keyword_and_url_seed` при заданном
   `seed_keyword`;
2. если Ads недоступен, сценарий **не падает**: он честно предупреждает, что
   без Ads идеи по сайту недоступны, и расширяет `seed_keyword`, если тот
   задан; если и его нет — результат пустой с внятной причиной;
3. Trends по самому заметному ключу, если бюджет позволяет.

**`ExistingSiteResearch(site_url)`** — свой сайт:

1. `SearchConsoleProvider.query` с `dimensions=["query","page"]` за окно по
   умолчанию;
2. `find_opportunities` по полученным строкам;
3. обогащение из Ads по запросам-возможностям, батчами по 20, в пределах
   бюджета;
4. Trends по самому частотному запросу, если бюджет позволяет.

У каждого сценария метод
`async def run(self, context) -> ResearchData` и
`def plan(self, context) -> DryRunPlan`. `plan` **не делает ни одного внешнего
вызова** и считает оценки арифметически.

Сортировка итогового списка: по `avg_monthly_searches` по убыванию, `None` в
конце; при полном отсутствии данных Ads — по `autocomplete_relevance`, и это
обязано быть отражено в `data_quality.caveats` отдельной строкой.

### 5. `src/google_keyword_ai/usecases/research.py`

- `def run_research(settings, target: str, *, scenario: str = "auto", language=None, country=None, seed_keyword=None, budget: Budget | None = None, dry_run: bool = False, limit=None) -> Envelope[ResearchData] | Envelope[DryRunPlan]`.

Выбор сценария при `scenario="auto"`:
- `target` начинается с `sc-domain:` или `https://` и совпадает с одной из
  property Search Console → `ExistingSiteResearch`;
- `target` похож на домен или URL → `CompetitorResearch`;
- иначе → `NewNicheResearch`.
Явно заданный `scenario` (`niche`, `competitor`, `site`) выбор перекрывает;
неизвестное значение → `InvalidConfigurationError`.

При `dry_run=True` возвращается конверт с `DryRunPlan` и **ни одного внешнего
вызова не делается**.

Как и остальные фасады: синхронный, через `anyio.run`; ошибки провайдеров не
выпускаются наружу, а превращаются в `warnings`/`errors` и
`completeness=partial`; полностью пустой результат → `empty` с причиной.

### 6. Обёртки

- `cli/main.py`: `gkai research <target>` с опциями `--scenario`,
  `--language`, `--country`, `--seed-keyword`, `--max-keywords`,
  `--max-autocomplete-queries`, `--max-ads-calls`, `--max-trends-calls`,
  `--max-runtime`, `--dry-run`, `--limit`, `--format`. Существующие команды не
  менять.
- `mcp/server.py`: **синхронный** инструмент
  `research_keywords(target, scenario="auto", language=None, country=None, seed_keyword=None, dry_run=False, limit=None)`.
  Существующие инструменты не менять.

### 7. `docs/pipeline.md`

Не длиннее 80 строк: три сценария и почему их три, а не один линейный порядок;
cheap-first; что означает каждый предел бюджета и что исчерпание — не ошибка;
зачем `--dry-run`; как читать `data_quality`; три запрета в формулировках;
явно — что запуски пока не сохраняются (это M8), а scoring и кластеризация
появятся в M9.

### 8. Тесты

Провайдеров подделывать; сеть не использовать.

- `tests/test_budget.py` — каждый предел срабатывает поимённо
  (`max_keywords`, `max_autocomplete_queries`, `max_ads_calls`,
  `max_trends_calls`, `max_runtime_seconds`); неизвестный `kind` → ошибка;
  неположительные значения → ошибка.
- `tests/test_scenarios.py` — для каждого из трёх сценариев: порядок
  обращений соответствует описанному (проверять по журналу вызовов подделок);
  Ads получает не все кандидаты, а только отобранных **после** дедупликации;
  ключи уходят в Ads батчами по 20; недоступный Ads не роняет ни один
  сценарий; `CompetitorResearch` без Ads и без `seed_keyword` даёт пустой
  результат с причиной; сортировка падает на релевантность автокомплита при
  отсутствии Ads и это попадает в `caveats`.
- `tests/test_dry_run.py` — `plan()` каждого сценария не делает ни одного
  вызова подделок и возвращает ненулевые оценки.
- `tests/test_research_usecase.py` — авто-выбор сценария для темы, домена, URL
  и property; явный `scenario` перекрывает; неизвестный → ошибка;
  `completeness=partial` при отказе одного провайдера, `empty` при полном
  отсутствии данных; `caveats` содержат три запрета, когда источники
  участвовали.
- Дописать в `tests/test_cli.py` проверку `gkai research --dry-run` и в
  `tests/test_mcp_parity.py` — parity для `research_keywords` (фикстура
  `thread_offload`).

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять.**
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/**` — читать, не изменять.
- `docs/superpowers/`, все `docs/specs/m1..m6*.md`, `docs/autocomplete.md`,
  `docs/expansion.md`, `docs/trends.md`, `docs/google-ads.md`,
  `docs/search-console.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- **Провайдеры менять НЕ нужно.** `providers/**`, `opportunities.py`,
  `normalize.py`, `market.py`, `cache.py`, `http.py`, `ratelimit.py`,
  `storage/**` остаются как есть. Правятся только `cli/main.py`,
  `mcp/server.py` и, при необходимости, `config.py` — и только так, как
  описано в «Что делать». Существующие 218 тестов должны остаться зелёными без
  правок: если приходится править существующий тест — контракт принятой вехи
  сломан, остановись и доложи.
- Не создавать таблиц в БД и не трогать миграции: сохранение запусков — веха
  M8.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (runs, scoring, clustering,
  reports, skill).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/pipeline/**`,
`src/google_keyword_ai/usecases/research.py`, `tests/**` (кроме
`tests/fixtures/**`), `docs/pipeline.md`.

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
- **AC-004.** Каждый из пяти пределов бюджета срабатывает и называет себя.
  **Все пять проверяются поимённо.**
  Проверка: `.venv/bin/pytest -q tests/test_budget.py`
- **AC-005.** Cheap-first соблюдён: в Google Ads уходят только кандидаты,
  пережившие дедупликацию и отсев, и не больше, чем позволяет бюджет.
  Проверка: `.venv/bin/pytest -q tests/test_scenarios.py -k cheap_first`
- **AC-006.** Ключи уходят в Ads батчами не больше 20 штук.
  Проверка: `.venv/bin/pytest -q tests/test_scenarios.py -k batch`
- **AC-007.** Каждый из трёх сценариев обращается к источникам в своём
  порядке. **Все три проверяются поимённо.**
  Проверка: `.venv/bin/pytest -q tests/test_scenarios.py -k "niche or competitor or existing_site"`
- **AC-008.** Недоступный Google Ads не роняет ни один сценарий, а
  `CompetitorResearch` без Ads и без seed-ключа даёт пустой результат с
  причиной.
  Проверка: `.venv/bin/pytest -q tests/test_scenarios.py -k unavailable`
- **AC-009.** При отсутствии данных Ads сортировка падает на релевантность
  автокомплита, и это записано в `caveats`.
  Проверка: `.venv/bin/pytest -q tests/test_scenarios.py -k sorting`
- **AC-010.** `--dry-run` не делает ни одного обращения к провайдерам и
  возвращает план с оценками.
  Проверка: `.venv/bin/pytest -q tests/test_dry_run.py`
- **AC-011.** Автовыбор сценария различает тему, домен, URL и property, явный
  выбор перекрывает автовыбор, неизвестный отвергается.
  Проверка: `.venv/bin/pytest -q tests/test_research_usecase.py -k scenario`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/specs/m3-expand.md docs/specs/m4-trends.md docs/specs/m5-google-ads.md docs/specs/m6-search-console.md docs/autocomplete.md docs/expansion.md docs/trends.md docs/google-ads.md docs/search-console.md tests/fixtures src/google_keyword_ai/providers src/google_keyword_ai/opportunities.py src/google_keyword_ai/normalize.py src/google_keyword_ai/market.py src/google_keyword_ai/cache.py src/google_keyword_ai/http.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/storage src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/expansion.py`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` или parity зависнут, останови их, отчитайся `not_attempted` с
>    этой причиной и продолжай остальные критерии. Делать инструмент
>    асинхронным ЗАПРЕЩЕНО.
> 3. **Кредов Google Ads и Search Console нет ни у кого.** Живой вызов
>    невозможен в принципе; все тесты — на подделках.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Живое исследование ниши с малым бюджетом: `gkai research` по
  реальной теме без кредов Ads и GSC — должен пройти на Autocomplete и Trends,
  вернуть `partial` с внятными предупреждениями и не упасть.
- **HC-003.** `gkai research --dry-run` вживую: план печатается, ни одного
  сетевого запроса не уходит (проверить по отсутствию задержек и по логам).
- **HC-004.** Мутационная проверка: сломать каждый предел бюджета, отбор
  кандидатов перед Ads, размер батча, порядок каждого сценария, запасную
  сортировку и признак dry-run.
- **HC-005.** Живой вызов `research_keywords` по настоящему stdio.

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

- Создавать и править файлы в: `src/google_keyword_ai/pipeline/**`,
  `src/google_keyword_ai/usecases/research.py`, `src/google_keyword_ai/cli/main.py`,
  `src/google_keyword_ai/mcp/server.py`, `src/google_keyword_ai/config.py`,
  `tests/**` (кроме `tests/fixtures/**`), `docs/pipeline.md`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/`, `.toolchain/`, `tests/fixtures/**`
  и код провайдеров, обращаться к настоящим API Google, писать файлы во
  временные каталоги в качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
