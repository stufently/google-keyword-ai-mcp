# Спека: Google Ads Keyword Planner (M5)

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `5 из 9` · **Дата:** `2026-09-02` · **Исполнитель:** Codex

## Контекст

Вехи M1–M4 приняты: каркас, HTTP-слой, кеш, нормализация, Autocomplete, веерное
расширение, Google Trends. 138 зелёных тестов. **Читай существующий код и
опирайся на его типы, не переписывая их.**

Эта веха добавляет единственный источник **абсолютного** объёма поиска —
Google Ads Keyword Planner. У владельца пока НЕТ developer token, поэтому
провайдер обязан быть полностью опциональным: без кредов всё остальное
работает, а `doctor` честно говорит `missing credentials`.

**Чего в этой вехе НЕТ:** Search Console (M6), пайплайн `research` и runs (M7),
scoring и кластеризация (M8). Не реализовывай их.

## Проверенные факты

Снято 2026-09-02 с установленного в дереве клиента `google-ads` 31.4.0 и с
официальной документации Google. По памяти ничего не переписывать.

### API клиента

- Версии API в пакете: `v21`–`v25`; **по умолчанию `v25`** (`get_service`
  имеет `version: str = "v25"`).
- Создание клиента: `GoogleAdsClient.load_from_dict(config_dict, version=None)`.
- Сервис: `client.get_service("KeywordPlanIdeaService")`.
- Типы запросов: `client.get_type("GenerateKeywordIdeasRequest")` и
  `client.get_type("GenerateKeywordHistoricalMetricsRequest")`.
- Методы сервиса: `generate_keyword_ideas(request=...)` и
  `generate_keyword_historical_metrics(request=...)`.

### Поля `GenerateKeywordIdeasRequest`

```
customer_id, language, geo_target_constants, include_adult_keywords,
page_token, page_size, keyword_plan_network, keyword_annotation,
aggregate_metrics, historical_metrics_options,
keyword_and_url_seed | keyword_seed | url_seed | site_seed
```

🚨 Последние четыре — **один `oneof` с именем `seed`**: заполнять можно ровно
один. Попытка задать два — ошибка на стороне библиотеки, поэтому выбор режима
должен быть явным и взаимоисключающим в коде.

### Поля `GenerateKeywordHistoricalMetricsRequest`

```
customer_id, keywords, language, include_adult_keywords,
geo_target_constants, keyword_plan_network, aggregate_metrics,
historical_metrics_options
```

### Ответ

`GenerateKeywordIdeaResult`: `text`, `keyword_idea_metrics`,
`keyword_annotations`, `close_variants`.

`KeywordPlanHistoricalMetrics`:

```
avg_monthly_searches, monthly_search_volumes, competition,
competition_index, low_top_of_page_bid_micros,
high_top_of_page_bid_micros, average_cpc_micros
```

🚨 Ставки приходят в **micros**: значение делится на 1 000 000, чтобы получить
единицы валюты. Хранить и то, и другое не нужно — храним приведённое значение и
называем поле без суффикса `_micros`, но перевод обязателен и покрыт тестом.

### Гео и язык: числовые константы, а не ISO-коды

Google Ads не принимает `"RU"`. Нужны `geoTargetConstants/<criteria id>` и
`languageConstants/<id>`.

**Criteria ID стран** — из официального CSV Google
`geotargets-2026-08-12.csv` (скачан и разобран 2026-09-02, 273 666 строк,
отобраны строки с `Target Type == "Country"`):

```
AE 2784   BR 2076   BY 2112   CN 2156   DE 2276   ES 2724
FR 2250   GB 2826   IN 2356   IT 2380   JP 2392   KZ 2398
PL 2616   RU 2643   TH 2764   TR 2792   UA 2804   US 2840
```

**ID языков** — из официальной страницы `google-ads/api/data/codes-formats`
(разобрана 2026-09-02):

```
ar 1019   de 1001   en 1000   es 1003   fr 1002   hi 1023
it 1004   ja 1005   pl 1030   pt 1014   ru 1031   th 1044
tr 1037   uk 1036   zh_CN 1017 (и zh_TW 1018)
```

🚨 **Казахского (`kk`) в списке языков Google Ads НЕТ.** Он есть в
`SUPPORTED_LANGUAGES` нашего `Market`, поэтому `ads_language_id("kk")` обязан
поднимать `ProviderUnavailableError` с внятным текстом, а НЕ подставлять
похожий ID. Придумывать значение запрещено.

Для `zh` берём `zh_CN` (1017) и пишем об этом в документации: у Google это два
разных языка, а у нас один код.

### Лимиты и кеш

Keyword Planning API ограничен примерно **1 запросом в секунду на customer ID**
(официальная страница квот). Обходить нельзя. Исторические метрики
обновляются примерно раз в месяц, поэтому TTL кеша длинный.

🚨 Троттлер обязан быть **межпроцессным**: CLI и MCP-сервер — это два разных
процесса, а лимит у них общий на customer ID. Внутрипроцессный семафор здесь
даёт ложное чувство защиты.

### Что уже есть в дереве

- `Settings` уже содержит поля кредов: `google_ads_developer_token`,
  `google_ads_customer_id`, `google_ads_login_customer_id`,
  `google_ads_client_id`, `google_ads_client_secret`,
  `google_ads_refresh_token` (все секретные — `SecretStr`).
- `Market.ads_criteria_id()` сейчас всегда поднимает
  `ProviderUnavailableError` с текстом про M4 — эта веха его реализует.
- `SqliteCache`, `build_cache_key`, `AsyncRateLimiter`, `Provider`,
  `ProviderInfo`, `Envelope`, таксономия ошибок.
- Инструменты MCP регистрируются **синхронными** функциями.
- Сети в песочнице нет, кредов Google Ads нет ни у кого. **Все тесты — на
  подделке сервиса**, реального обращения быть не может.

## Что делать

### 1. `src/google_keyword_ai/market.py`

Добавить таблицы и методы (существующее поведение не менять):

- `ADS_COUNTRY_CRITERIA: dict[str, int]` — 18 пар из «Проверенных фактов»;
- `ADS_LANGUAGE_CONSTANTS: dict[str, int]` — коды из «Проверенных фактов»,
  плюс `zh -> 1017`;
- `ads_criteria_id(self) -> int` — теперь возвращает число; если страны нет в
  таблице → `ProviderUnavailableError` с указанием, какой страны не хватает;
- `ads_language_id(self) -> int` — то же для языка; **для `kk` обязан
  поднимать `ProviderUnavailableError`**, потому что Google Ads такого языка
  не знает;
- `ads_geo_target_resource(self) -> str` → `f"geoTargetConstants/{id}"`;
- `ads_language_resource(self) -> str` → `f"languageConstants/{id}"`.

В комментарии у таблиц указать источник и дату: официальный CSV
`geotargets-2026-08-12` и страница codes-formats, снято 2026-09-02.

### 2. Расширить `src/google_keyword_ai/config.py`

- `google_ads_api_version: str = "v25"`;
- `google_ads_rate_limit_per_second: float = 1.0`;
- `google_ads_ideas_cache_ttl_seconds: int = 604800` (неделя);
- `google_ads_historical_cache_ttl_seconds: int = 2592000` (30 суток);
- `google_ads_page_size: int = 1000`.

Валидация: положительные значения, иначе `InvalidConfigurationError`.

### 3. `src/google_keyword_ai/ratelimit.py`

Добавить, не трогая существующий `AsyncRateLimiter`:

- `class InterProcessRateLimiter` с параметрами `rate_per_second: float` и
  `lock_path: Path`;
- `async def acquire(self) -> None` — берёт **файловую блокировку**
  (`fcntl.flock` на отдельном файле в каталоге данных), читает из него момент
  последней выдачи, при необходимости спит через `anyio.sleep`, записывает
  новый момент, отпускает блокировку. Блокирующие операции с файлом выполнять
  через `anyio.to_thread.run_sync`, чтобы не держать event loop;
- время хранить монотонно неубывающим: использовать `time.time()` (общее для
  процессов), а не `anyio.current_time()`, который у каждого процесса свой;
- `rate_per_second <= 0` → `InvalidConfigurationError`.

### 4. `src/google_keyword_ai/providers/google_ads.py`

- `class KeywordMetrics` (pydantic): `avg_monthly_searches: int | None`,
  `monthly_search_volumes: list[MonthlyVolume] = []`,
  `competition: str | None`, `competition_index: int | None`,
  `low_top_of_page_bid: float | None`, `high_top_of_page_bid: float | None`,
  `average_cpc: float | None`, `currency: str | None`;
- `class MonthlyVolume` (pydantic): `year: int`, `month: str`,
  `monthly_searches: int`;
- `class KeywordIdea` (pydantic): `text: str`, `metrics: KeywordMetrics`;
- `class AdsSeed` (pydantic, frozen): `keywords: list[str] = []`,
  `url: str | None = None`, `site: str | None = None`; метод
  `mode(self) -> str`, возвращающий одно из
  `keyword_seed | url_seed | keyword_and_url_seed | site_seed`, и
  поднимающий `InvalidConfigurationError`, если задано пусто или задан и
  `site`, и что-то ещё;
- `class GoogleAdsProvider(Provider)`:
  - конструктор принимает `settings`, `cache`, `rate_limiter` и
    **`service_factory: Callable[[], object] | None = None`**. Фабрика нужна,
    чтобы тесты подставляли подделку: обращения к сети в тестах быть не может;
  - `info` → `name="google_ads"`, `official=True`, `stability="stable"`;
  - `is_available()` → `True`, только если заданы developer token, customer id,
    client id, client secret и refresh token; иначе `False`;
  - `def build_service(self)` — собирает конфиг для
    `GoogleAdsClient.load_from_dict` из настроек (ключи
    `developer_token`, `client_id`, `client_secret`, `refresh_token`,
    `login_customer_id` при наличии, `use_proto_plus: True`) и возвращает
    `client.get_service("KeywordPlanIdeaService")`. Если задана фабрика —
    используется она;
  - `async def keyword_ideas(self, seed: AdsSeed, market: Market, *, include_adult: bool = False) -> list[KeywordIdea]`;
  - `async def historical_metrics(self, keywords: Sequence[str], market: Market) -> list[KeywordIdea]`.

Обе операции обязаны:

1. проверять `is_available()`, иначе `ProviderUnavailableError`;
2. читать кеш до любых сетевых действий (ключ обязан включать
   **`account_scope` = customer id**, иначе два аккаунта увидят чужие данные);
3. брать межпроцессный троттлер;
4. **вызывать блокирующий gRPC через `anyio.to_thread.run_sync`** — клиент
   `google-ads` синхронный, и прямой вызов заблокировал бы event loop;
5. переводить ставки из micros делением на 1 000 000;
6. складывать результат в кеш с соответствующим TTL.

Ошибки библиотеки (`GoogleAdsException` и любые её родители) переводить в наши:
исчерпание квоты (`RESOURCE_EXHAUSTED`) → `RateLimitError`, проблемы
авторизации → `AuthenticationError`, прочее → `ApiError`. Ловить именно
исключение библиотеки, а не `Exception`.

🚨 **Никогда не называть `competition` SEO-сложностью.** Поле называется
`ads_competition` во всех местах, где оно показывается человеку.

### 5. `src/google_keyword_ai/usecases/ads.py`

- `class AdsData` (pydantic): `provider: ProviderInfo`, `mode: str`,
  `language: str`, `country: str`, `ideas: list[KeywordIdea]`;
- `def run_ads_ideas(settings, keywords=None, *, url=None, site=None, language=None, country=None, include_adult=False, limit=None) -> Envelope[AdsData]`;
- `def run_ads_historical(settings, keywords, *, language=None, country=None) -> Envelope[AdsData]`;
- `def run_competitor(settings, target: str, *, seed_keyword=None, language=None, country=None, limit=None) -> Envelope[AdsData]`
  — если `target` выглядит как голый домен, используется `site_seed`; если это
  URL с путём — `url_seed`; при наличии `seed_keyword` — `keyword_and_url_seed`.

Все фасады синхронные, работают через `anyio.run`, и при
`ProviderUnavailableError`, `AuthenticationError`, `RateLimitError`,
`NetworkError`, `ApiError` возвращают конверт с пустым списком,
`completeness=EMPTY`, заполненными `errors` и `completeness_reason`, не
выпуская исключение. Пустой ответ без ошибки → `EMPTY` с причиной
`"no keyword ideas"`.

### 6. Обёртки

- `cli/main.py`: `gkai ads ideas`, `gkai ads historical`, `gkai competitor`.
  У `ads ideas` — позиционные ключевые слова плюс опции `--url`, `--site`,
  `--include-adult`, `--limit`, `--language`, `--country`, `--format`.
  Существующие команды не менять.
- `mcp/server.py`: **синхронные** инструменты
  `get_keyword_metrics(keywords, language=None, country=None)` (историческая
  метрика) и `analyze_competitor(target, seed_keyword=None, language=None, country=None, limit=None)`.
  Существующие инструменты не менять.
- `usecases/doctor.py`: у провайдера `google_ads` брать `available` из
  `GoogleAdsProvider.is_available()`; `detail` — `"ready"` при полных кредах,
  `"missing credentials"` иначе.

### 7. `docs/google-ads.md`

Не длиннее 80 строк: какие креды нужны и где их взять, что провайдер
опционален, четыре режима seed и что заполнять можно только один, перевод
micros, лимит 1 rps на customer ID и почему троттлер межпроцессный, длинные
TTL кеша и почему, откуда взяты таблицы criteria ID и языков (с датой), что
казахского в Ads нет, и **три запрета**: `ads_competition` — не SEO-сложность;
site seed даёт «идеи ключей, которые Google связывает с сайтом», а не
«запросы, по которым сайт ранжируется»; объёмы Google округляет и объединяет
близкие варианты, поэтому это не точный счётчик.

### 8. Тесты

Реального клиента не поднимать: везде подставлять подделку сервиса через
`service_factory`.

- `tests/test_market_ads.py` — `ads_criteria_id` и `ads_language_id` дают
  числа из таблиц; `geoTargetConstants/2643` и `languageConstants/1031` для
  `ru/RU`; **`kk` поднимает `ProviderUnavailableError`**; неизвестная страна
  тоже.
- `tests/test_google_ads_provider.py` — подделка сервиса возвращает
  фиксированный ответ: micros переводятся в единицы валюты; выбирается
  правильный режим seed для каждого из четырёх случаев; заданы одновременно
  `site` и `keywords` → `InvalidConfigurationError`; без кредов
  `is_available()` ложно и вызов даёт `ProviderUnavailableError`; ключ кеша
  различает customer id; второй вызов берётся из кеша без обращения к сервису;
  ошибка библиотеки переводится в нашу таксономию; блокирующий вызов уходит в
  поток (проверить, что вызов сервиса происходит не в потоке event loop —
  сравнить `threading.get_ident()` внутри подделки с идентификатором потока,
  в котором работает цикл).
- `tests/test_ratelimit_interprocess.py` — два экземпляра лимитера с общим
  файлом выдерживают интервал; неположительный rate → ошибка; файл блокировки
  создаётся в каталоге данных.
- `tests/test_ads_usecase.py` — конверт при успехе; `EMPTY` без кредов;
  `EMPTY` при ошибке провайдера без выброса исключения; `run_competitor`
  выбирает `site_seed` для домена и `url_seed` для URL с путём.
- Дописать в `tests/test_cli.py` проверки новых команд на подделке и в
  `tests/test_mcp_parity.py` — parity для `get_keyword_metrics` (использовать
  фикстуру `thread_offload`), а также что `doctor` показывает `google_ads`
  недоступным без кредов.

## Не трогать

- `pyproject.toml`, `uv.lock`. **Новых зависимостей не добавлять** —
  `google-ads` уже установлен как extra. Не хватает библиотеки — остановись и
  доложи.
- `.venv/`, `.toolchain/`, `.gitignore`, `.dockerignore`, `AGENTS.md`.
- `README.md` — переписывается в последней вехе.
- `tests/fixtures/**` — читать, не изменять.
- `docs/superpowers/`, `docs/specs/m1-scaffold.md`, `docs/specs/m2-autocomplete.md`,
  `docs/specs/m3-expand.md`, `docs/specs/m4-trends.md`, `docs/autocomplete.md`,
  `docs/expansion.md`, `docs/trends.md`.
- `src/google_keyword_ai/__init__.py` — версию не менять.
- Код вех M1–M4 менять НЕ нужно, кроме перечисленного в «Что делать»
  (`market.py`, `config.py`, `ratelimit.py`, `cli/main.py`, `mcp/server.py`,
  `usecases/doctor.py`). Существующие 138 тестов должны остаться зелёными без
  правок — кроме теста, который сейчас утверждает, что `ads_criteria_id`
  поднимает ошибку: его поведение эта веха меняет намеренно, и обновить его
  можно. Если приходится править ЛЮБОЙ другой существующий тест — остановись и
  доложи.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни CI-конфигов, ни файлов под будущие вехи (search_console, pipeline,
  scoring, clustering).
- Не читать и не выполнять скилы и плейбуки из `~/.codex/plugins`.

**Где МОЖНО создавать файлы:** `src/google_keyword_ai/**`, `tests/**` (кроме
`tests/fixtures/**`), `docs/google-ads.md`.

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
- **AC-004.** Таблицы гео и языков дают ровно значения из официальных
  источников, а казахский честно отвергается.
  Проверка: `.venv/bin/pytest -q tests/test_market_ads.py`
- **AC-005.** Ставки переводятся из micros в единицы валюты.
  Проверка: `.venv/bin/pytest -q tests/test_google_ads_provider.py -k micros`
- **AC-006.** Каждый из четырёх режимов seed выбирается корректно, а
  противоречивая комбинация отвергается. **Все четыре режима проверяются
  поимённо.**
  Проверка: `.venv/bin/pytest -q tests/test_google_ads_provider.py -k seed`
- **AC-007.** Без кредов провайдер недоступен и поднимает
  `ProviderUnavailableError`, не пытаясь никуда идти.
  Проверка: `.venv/bin/pytest -q tests/test_google_ads_provider.py -k credentials`
- **AC-008.** Ключ кеша различает customer id, а повторный вызов не трогает
  сервис.
  Проверка: `.venv/bin/pytest -q tests/test_google_ads_provider.py -k cache`
- **AC-009.** Блокирующий вызов клиента выполняется НЕ в потоке event loop.
  Проверка: `.venv/bin/pytest -q tests/test_google_ads_provider.py -k thread`
- **AC-010.** Межпроцессный троттлер выдерживает интервал между двумя разными
  экземплярами с общим файлом блокировки.
  Проверка: `.venv/bin/pytest -q tests/test_ratelimit_interprocess.py`
- **AC-011.** Use-case не выпускает исключения наружу и правильно выбирает
  режим для домена и для URL с путём.
  Проверка: `.venv/bin/pytest -q tests/test_ads_usecase.py`
- **AC-012.** Файлы, объявленные неприкосновенными, не изменены.
  Проверка: `git diff --exit-code -- pyproject.toml uv.lock AGENTS.md README.md .gitignore .dockerignore src/google_keyword_ai/__init__.py docs/superpowers docs/specs/m1-scaffold.md docs/specs/m2-autocomplete.md docs/specs/m3-expand.md docs/specs/m4-trends.md docs/autocomplete.md docs/expansion.md docs/trends.md tests/fixtures src/google_keyword_ai/envelope.py src/google_keyword_ai/errors.py src/google_keyword_ai/logging.py src/google_keyword_ai/storage src/google_keyword_ai/http.py src/google_keyword_ai/cache.py src/google_keyword_ai/normalize.py src/google_keyword_ai/expansion.py src/google_keyword_ai/providers/autocomplete.py src/google_keyword_ai/providers/expander.py src/google_keyword_ai/providers/trends`

> Примечания для исполнителя, из опыта прошлых прогонов:
> 1. **Коммит в этой песочнице невозможен** — `.git` смонтирован только для
>    чтения. Это ожидаемо, коммит делает принимающий.
> 2. **Тесты MCP через in-memory транспорт в этой песочнице зависают.** Если
>    `pytest -q` или parity зависнут, останови их, отчитайся `not_attempted` с
>    этой причиной и продолжай остальные критерии. Делать инструмент
>    асинхронным ЗАПРЕЩЕНО.
> 3. **Кредов Google Ads нет ни у кого, включая принимающего.** Живой вызов
>    невозможен в принципе — не пытайся и не считай это блокирующим.

## Проверки принимающего

- **HC-001.** Прогон в чистом Docker на CPython 3.14 и 3.12.
- **HC-002.** Мутационная проверка: сломать перевод micros, выбор каждого
  режима seed, участие customer id в ключе кеша, уход блокирующего вызова в
  поток, межпроцессный интервал, проверку кредов — каждый раз должен падать
  именно свой тест.
- **HC-003.** Сборка wheel и `gkai doctor` из установленного пакета: провайдер
  `google_ads` показан недоступным с причиной `missing credentials`.
- **HC-004.** Проверить, что отсутствие кредов не ломает остальные команды:
  `gkai suggest`, `gkai expand`, `gkai trends` продолжают работать вживую.
- **HC-005.** Живой вызов `get_keyword_metrics` по настоящему stdio — ожидается
  конверт с `empty` и причиной про отсутствие кредов, а не падение.

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
  `tests/fixtures/**`), `docs/google-ads.md`.
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
