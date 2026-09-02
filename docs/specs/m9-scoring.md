# Спека: оценка, кластеризация и отчёты (M9)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `9 из 10` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M8 приняты: каркас, четыре источника, три сценария исследования,
бюджет, dry-run и запуски с продолжением. 266 зелёных тестов. **Читай
существующий код и опирайся на его типы, не переписывая их.**

Сейчас `gkai research` отдаёт плоский список, отсортированный по спросу. Эта
веха добавляет то, ради чего инструмент и затевался: прозрачную оценку
возможности, группировку по темам и человекочитаемый отчёт.

**Чего в этой вехе НЕТ:** Claude Skill, README и общая документация проекта —
это веха M10. Новых источников данных нет.

## Проверенные факты

### Что уже есть

- `ResearchKeyword` в `pipeline/models.py` с полями `keyword`, `normalized`,
  `discovered_from`, `autocomplete_relevance`, `avg_monthly_searches`,
  `ads_competition`, `ads_competition_index`, `low_top_of_page_bid`,
  `high_top_of_page_bid`, `gsc_impressions`, `gsc_clicks`, `gsc_ctr`,
  `gsc_position`;
- `ResearchData` с `keywords`, `trends: TrendsResult | None`,
  `opportunities`, `stats`, `data_quality`;
- `DataQuality` с `sources`, `absolute_metrics`, `relative_metrics`,
  `derived_metrics`, `caveats`;
- `TrendsResult.timeline: list[TrendPoint]`, у точки `values: list[int]` и
  `timestamp`;
- `RunStore` и `RunRecord` в `pipeline/runs.py`; у записи есть
  `result: dict[str, object] | None` — сохранённый конверт исследования;
- `normalize_keyword` в `normalize.py`;
- `Envelope[T]`; при `completeness != complete` причина обязательна;
- инструменты MCP регистрируются **синхронными** функциями;
- **union-тип возврата у инструмента MCP запрещён**: SDK заворачивает такой
  результат в лишний ключ `result`, и вывод CLI перестаёт совпадать с
  `structured_content`. В `tests/test_mcp_parity.py` уже есть тест, который
  проверяет это для всех инструментов сразу.

### Три запрета, которые эта веха обязана удержать

Константы уже существуют в `pipeline/scenarios.py`: `TRENDS_CAVEAT`,
`ADS_CAVEAT`, `SITE_SEED_CAVEAT`. Переиспользуй их, не дублируй текст.

Дополнительно: **если данных о выдаче нет, сложность так и называется
неизвестной.** Подставлять вместо неё `ads_competition` запрещено.

### Границы вычислимого

Данные Google Ads доступны не всегда (кредов у владельца нет), Search Console —
тем более. Значит **каждая составляющая оценки обязана уметь отсутствовать**, а
итоговый балл — честно говорить, на скольких составляющих он посчитан. Оценка,
молча приравнивающая отсутствующее к нулю, врёт: ключ без данных Ads выглядел
бы хуже ключа с нулевым спросом.

## Что делать

### 1. Расширить `src/google_keyword_ai/config.py`

Веса оценки — в настройках, не в коде:

- `score_weight_demand: float = 0.35`;
- `score_weight_trend: float = 0.2`;
- `score_weight_commercial: float = 0.2`;
- `score_weight_opportunity: float = 0.25`;
- `score_demand_reference: int = 100000` — объём, дающий 100 баллов спроса;
- `score_bid_reference: float = 5.0` — ставка, дающая 100 баллов коммерческой
  ценности;
- `cluster_similarity_threshold: float = 0.34` — порог сходства;
- `cluster_min_size: int = 2`.

Валидация: все веса неотрицательны и **их сумма строго больше нуля**; ссылочные
значения положительны; порог сходства в интервале `(0, 1]`; `cluster_min_size >= 1`.
Иначе `InvalidConfigurationError`.

### 2. `src/google_keyword_ai/scoring.py`

- `class ScoreComponent` (pydantic): `name: str`, `available: bool`,
  `raw: float | None`, `normalized: float | None`, `weight: float`,
  `contribution: float`, `explanation: str`;
- `class KeywordScore` (pydantic): `keyword: str`, `score: float`,
  `components: list[ScoreComponent]`, `components_available: int`,
  `components_total: int`, `confidence: str`;
- `def score_keyword(keyword: ResearchKeyword, settings: Settings, *, trend_growth: float | None = None) -> KeywordScore`.

Четыре составляющие, каждая нормализуется в 0–100:

- **demand** — из `avg_monthly_searches`:
  `100 * log10(volume + 1) / log10(score_demand_reference + 1)`, обрезка
  сверху сотней. Логарифм, потому что разница между 100 и 1000 важнее, чем
  между 100 000 и 101 000. Нет объёма → составляющая недоступна;
- **trend** — из `trend_growth` (доля прироста, где `0.0` — без изменений):
  `50 + 50 * clamp(trend_growth, -1, 1)`. Нет данных → недоступна;
- **commercial** — из `high_top_of_page_bid`, при его отсутствии из
  `low_top_of_page_bid`: `100 * min(bid / score_bid_reference, 1)`. Нет
  ставок → недоступна;
- **opportunity** — из данных Search Console: чем больше показов и хуже
  позиция, тем выше возможность. `100 * min(impressions / 1000, 1) *
  clamp((position - 1) / 29, 0, 1)`. Нет данных GSC → недоступна.

🚨 **Итоговый балл считается только по доступным составляющим**: сумма
`normalized * weight` делится на сумму весов доступных составляющих. Отсутствие
данных НЕ приравнивается к нулю. Если доступна ни одна — `score = 0.0` и
`confidence = "none"`.

`confidence`: `"high"` при четырёх доступных, `"medium"` при трёх,
`"low"` при одной-двух, `"none"` при нуле.

У каждой составляющей `explanation` — человекочитаемая строка с фактическими
числами; у недоступной — почему её нет.

- `def compute_trend_growth(trends: TrendsResult | None) -> float | None` —
  сравнивает среднее значение последней четверти таймсерии со средним
  предыдущей четверти и возвращает относительный прирост. Меньше восьми точек
  или отсутствие данных → `None`. Значения берутся из `values[0]`.

🚨 `trend_growth` считается по значениям одного запроса Trends, то есть внутри
одного `normalization_scope`; сравнивать значения разных запросов запрещено, и
в документации это надо назвать.

### 3. `src/google_keyword_ai/clustering.py`

Детерминированная лексическая кластеризация, без LLM и без сети.

- `class KeywordCluster` (pydantic): `label: str`, `keywords: list[str]`,
  `size: int`, `shared_tokens: list[str]`;
- `def tokenize(text: str) -> list[str]` — нормализация через
  `normalize_keyword` и разбиение по пробелам;
- `def similarity(left: Sequence[str], right: Sequence[str]) -> float` —
  коэффициент Жаккара по множествам токенов; пустые множества дают `0.0`;
- `def cluster_keywords(keywords: Sequence[str], settings: Settings) -> list[KeywordCluster]`
  — простая агломерация: ключи обходятся в заданном порядке, каждый
  присоединяется к первому кластеру, со **всеми** членами которого сходство не
  ниже порога, иначе заводит свой; кластеры меньше `cluster_min_size`
  сливаются в кластер `"unclustered"`, который идёт последним.
  `label` — самые частые общие токены кластера через пробел, при их отсутствии
  первый ключ. Результат обязан быть **детерминированным**: одинаковый вход
  даёт одинаковый выход, независимо от порядка обхода словарей.

Архитектурно оставить место для других кластеризаторов (embedding, SERP,
LLM), но НЕ реализовывать их.

### 4. `src/google_keyword_ai/reports/__init__.py` и `reports/markdown.py`

- `def render_markdown(data: ResearchData, scores: Sequence[KeywordScore], clusters: Sequence[KeywordCluster]) -> str`.

Разделы строго в этом порядке, каждый — заголовок второго уровня:

```
# Keyword research
(шапка: seed/цель, язык, страна, сценарий, провайдеры)
## Summary
## Top opportunities
## Keyword clusters
## Trends
## Long-tail opportunities
## Search Console opportunities
## Data quality and limitations
```

Пустой раздел не выбрасывается: он остаётся с явной строкой о том, что данных
нет и почему. В `Data quality and limitations` перечисляются использованные и
недоступные источники, время получения данных, какие показатели абсолютные,
какие относительные, какие расчётные, и все `caveats`.

Long-tail — ключи из трёх и более слов.

### 5. `src/google_keyword_ai/usecases/analysis.py`

- `class ScoredResearchData` (pydantic): `research: ResearchData`,
  `scores: list[KeywordScore]`, `clusters: list[KeywordCluster]`;
- `class NicheFactor` (pydantic): `name`, `value: float | None`,
  `available: bool`, `explanation: str`;
- `class NicheData` (pydantic): `seed: str`, `opportunity_score: float`,
  `factors: list[NicheFactor]`, `keywords_analyzed: int`,
  `clusters: int`, `caveats: list[str]`;
- `class KeywordProvenance` (pydantic): `keyword`, `normalized`,
  `discovered_from`, `metrics: list[MetricProvenance]`;
- `class MetricProvenance` (pydantic): `metric`, `value`, `source`,
  `retrieved_at: datetime | None`, `language`, `country`, `is_derived: bool`;

Функции:

- `def run_score(settings, run_id, *, limit=None) -> Envelope[ScoredResearchData]`
  — берёт сохранённый запуск из `RunStore`, считает оценки и кластеры;
- `def run_explain_score(settings, run_id, keyword) -> Envelope[KeywordScore]`
  — разбор одного ключа; ключа нет в запуске → `EMPTY` с причиной;
- `def run_cluster(settings, run_id) -> Envelope[list[KeywordCluster]]`;
- `def run_niche_analyze(settings, run_id) -> Envelope[NicheData]`;
- `def run_keyword_inspect(settings, run_id, keyword) -> Envelope[KeywordProvenance]`.

**Ниша оценивается по независимым факторам**, каждый со своей доступностью:
суммарный измеримый спрос, число значимых ключей, глубина long-tail,
направление тренда, коммерческая ценность, концентрация запросов (доля спроса
у первой пятёрки), разнообразие кластеров, покрытие существующим сайтом при
наличии данных GSC. Общий `opportunity_score` — среднее доступных факторов,
и рядом ОБЯЗАТЕЛЬНО отдаётся разбивка. Один непрозрачный балл без разбивки —
дефект.

🚨 Все эти команды работают по **сохранённому запуску**, а не ходят в сеть:
входом служит `run_id`. Это делает их дешёвыми, воспроизводимыми и
проверяемыми без кредов.

### 6. Обёртки

- `cli/main.py`:
  - `gkai score <run_id>`, `gkai cluster <run_id>`,
    `gkai explain-score <run_id> <keyword>`,
    `gkai niche analyze <run_id>`, `gkai keyword inspect <run_id> <keyword>`;
  - у `gkai research` добавить `--format markdown`, печатающий отчёт из
    `reports/markdown.py` (для этого посчитать оценки и кластеры на месте).
    Остальные форматы не менять.
- `mcp/server.py`: **синхронные** инструменты `score_run`, `cluster_run`,
  `explain_score`, `analyze_niche`, `inspect_keyword`. У каждого конкретный
  тип возврата — никаких union.

### 7. `docs/scoring.md`

Не длиннее 90 строк: формула каждой составляющей и почему именно такая
(в частности, почему спрос логарифмический); где лежат веса и как их менять;
**почему отсутствующая составляющая исключается из среднего, а не считается
нулём**; что означает `confidence`; факторы ниши и почему их показывают
разбивкой; что `trend_growth` считается внутри одного `normalization_scope`;
что при отсутствии данных о выдаче сложность остаётся неизвестной; три
стандартных запрета.

### 8. Тесты

- `tests/test_scoring.py` — каждая из четырёх составляющих считается по
  формуле на заданных числах (**четыре теста поимённо**); отсутствующая
  составляющая исключается из среднего, а не обнуляет балл (ключ только со
  спросом получает тот же балл, что и его составляющая demand); `confidence`
  меняется с числом доступных составляющих; ноль доступных даёт `0.0` и
  `"none"`; `compute_trend_growth` возвращает `None` на короткой таймсерии и
  считает прирост на длинной.
- `tests/test_clustering.py` — похожие ключи попадают в один кластер, разные —
  в разные; порог из настроек влияет на результат; кластеры меньше
  `cluster_min_size` уходят в `unclustered`, который идёт последним; результат
  детерминирован (два прогона на одном входе совпадают).
- `tests/test_reports.py` — все восемь разделов присутствуют в заданном
  порядке; пустой раздел содержит объяснение, а не исчезает; в разделе о
  качестве данных перечислены источники и все `caveats`.
- `tests/test_analysis_usecase.py` — все пять функций работают по
  сохранённому запуску; несуществующий запуск и отсутствующий ключ дают
  `empty` с причиной; `NicheData` всегда несёт разбивку по факторам;
  недоступный фактор помечен и не участвует в среднем.
- Дописать в `tests/test_cli.py` проверки новых команд и
  `gkai research --format markdown`; в `tests/test_mcp_parity.py` — parity
  хотя бы для `analyze_niche` и `explain_score` (фикстура `thread_offload`).
  Существующий тест, запрещающий union-тип у инструментов, обязан остаться
  зелёным.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** — ни
  numpy, ни scikit-learn, ни библиотек эмбеддингов. Кластеризация
  детерминированная и пишется на стандартной библиотеке.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/**` — читать, не изменять.
- `docs/superpowers/`, все `docs/specs/m1..m8*.md` и все существующие
  `docs/*.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- `providers/**`, `pipeline/**`, `storage/**`, `opportunities.py`,
  `normalize.py`, `market.py`, `cache.py`, `http.py`, `ratelimit.py` — не
  менять. Правятся только `config.py`, `cli/main.py`, `mcp/server.py` и
  создаются новые файлы из «Что делать». Существующие 266 тестов должны
  остаться зелёными без правок: если приходится править существующий тест —
  контракт принятой вехи сломан, остановись и доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под веху M10 (skill, README).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/scoring.py`,
`src/google_keyword_ai/clustering.py`, `src/google_keyword_ai/reports/**`,
`src/google_keyword_ai/usecases/analysis.py`, `tests/**` (кроме
`tests/fixtures/**`), `docs/scoring.md`.

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
- **AC-004.** Каждая из четырёх составляющих оценки считается по объявленной
  формуле. **Все четыре проверяются поимённо.**
  Проверка: `.venv/bin/pytest -q tests/test_scoring.py -k "demand or trend or commercial or opportunity"`
- **AC-005.** Недоступная составляющая исключается из среднего, а не считается
  нулём; при нуле доступных балл равен нулю с `confidence="none"`.
  Проверка: `.venv/bin/pytest -q tests/test_scoring.py -k "missing or none"`
- **AC-006.** `confidence` отражает число доступных составляющих.
  Проверка: `.venv/bin/pytest -q tests/test_scoring.py -k confidence`
- **AC-007.** `compute_trend_growth` даёт `None` на короткой таймсерии и
  считает прирост на длинной.
  Проверка: `.venv/bin/pytest -q tests/test_scoring.py -k growth`
- **AC-008.** Кластеризация детерминирована, порог из настроек влияет на
  результат, мелкие кластеры уходят в `unclustered` последним.
  Проверка: `.venv/bin/pytest -q tests/test_clustering.py`
- **AC-009.** Markdown-отчёт содержит все восемь разделов в объявленном
  порядке, а пустой раздел объясняет своё отсутствие данных.
  Проверка: `.venv/bin/pytest -q tests/test_reports.py`
- **AC-010.** Оценка ниши всегда сопровождается разбивкой по факторам, а
  недоступный фактор помечен и не участвует в среднем.
  Проверка: `.venv/bin/pytest -q tests/test_analysis_usecase.py -k niche`
- **AC-011.** Все команды анализа работают по сохранённому запуску;
  несуществующий запуск и отсутствующий ключ дают `empty` с причиной, а
  инструменты MCP по-прежнему не заворачивают payload в лишний ключ.
  Проверка: `.venv/bin/pytest -q tests/test_analysis_usecase.py tests/test_mcp_parity.py -k "missing or empty or result_key or nests"`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs docs/autocomplete.md docs/expansion.md docs/trends.md docs/google-ads.md docs/search-console.md docs/pipeline.md docs/runs.md tests/fixtures src/google_keyword_ai/providers src/google_keyword_ai/pipeline src/google_keyword_ai/storage src/google_keyword_ai/opportunities.py src/google_keyword_ai/normalize.py src/google_keyword_ai/market.py src/google_keyword_ai/cache.py src/google_keyword_ai/http.py src/google_keyword_ai/ratelimit.py src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/expansion.py`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` зависнет на `tests/test_mcp_parity.py`, останови его,
>    отчитайся `not_attempted` с этой причиной и продолжай остальные критерии.
> 3. **Кредов Google Ads и Search Console нет ни у кого.** Все тесты — на
>    подделках и на сохранённых запусках.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Живой `gkai research --format markdown` по реальной теме: отчёт
  читается, все разделы на месте, пустые честно объяснены.
- **HC-003.** Живая цепочка по сохранённому запуску: `score`, `cluster`,
  `explain-score`, `niche analyze`, `keyword inspect` — каждая отдаёт связный
  ответ, кластеры осмысленные, разбивка балла сходится с итогом.
- **HC-004.** Мутационная проверка: сломать каждую формулу составляющей,
  исключение недоступной составляющей из среднего, порог кластеризации,
  порядок разделов отчёта и подсчёт факторов ниши.
- **HC-005.** Живой вызов `analyze_niche` и `explain_score` по настоящему
  stdio.

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

- Создавать и править файлы в: `src/google_keyword_ai/scoring.py`,
  `src/google_keyword_ai/clustering.py`, `src/google_keyword_ai/reports/**`,
  `src/google_keyword_ai/usecases/analysis.py`,
  `src/google_keyword_ai/config.py`, `src/google_keyword_ai/cli/main.py`,
  `src/google_keyword_ai/mcp/server.py`, `tests/**` (кроме
  `tests/fixtures/**`), `docs/scoring.md`.
- Запускать: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`,
  `.venv/bin/mypy`, `.venv/bin/gkai`, `git` для чтения состояния.
- Сеть: **не использовать**, её нет.
- Git: коммит невозможен (read-only `.git`); ветку не переключать.
- Чего нельзя ни при каких условиях: менять `pyproject.toml` и `uv.lock`,
  добавлять зависимости, трогать `.venv/`, `.toolchain/`, `tests/fixtures/**`,
  код провайдеров и пайплайна, обращаться к настоящим API Google, писать файлы
  во временные каталоги в качестве доказательств.

## Формат отчёта

Финальный ответ обязан соответствовать схеме, поданной флагом
`--output-schema`. По **каждому** AC-id — статус, точная команда-доказательство
и её дословный вывод. Статус `pass` ставится только если команда реально
выполнялась в этом прогоне и вернула ноль; иначе `fail` или `not_attempted`.
