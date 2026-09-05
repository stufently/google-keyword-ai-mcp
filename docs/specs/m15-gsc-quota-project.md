# Спека: Search Console — quota project и разбор 403

**Репозиторий:** `google-keyword-ai-mcp` · **Веха:** `M15` · **Дата:** `2026-09-05` · **Исполнитель:** Codex

## Контекст

`SearchConsoleProvider` (`src/google_keyword_ai/providers/search_console.py`)
реализован давно и до 05.09.2026 ни разу не проверялся вживую. Первая же живая
проверка показала, что путь с кредами типа `authorized_user` не работает
ВООБЩЕ: Google отвечает 403, потому что `searchconsole.googleapis.com` требует
quota project, а пользовательские креды его не несут и провайдер его нигде не
задаёт. Загрузчик при этом объявляет `authorized_user` поддерживаемым наравне с
`service_account` (`load_credentials`, строки 159–188), то есть половина
заявленной функциональности мертва.

Вторая, меньшая беда — тот же 403 переводится в `AuthenticationError`
«Search Console authentication or authorization failed (403)», и владелец
исправных кредов идёт чинить то, что не сломано.

Веха закрывает ровно эти две вещи. Разбор ответов, кэш, троттлинг, пагинация,
`dataState` и всё остальное поведение провайдера НЕ ТРОГАТЬ и не переписывать:
они покрыты `tests/test_search_console_provider.py` (913 строк) и работают.

## Проверенные факты

Всё ниже получено ЖИВЫМИ вызовами 05.09.2026 против настоящего Google, с
кредами владельца (`~/.config/gcloud/application_default_credentials.json`,
тип `authorized_user`), Python 3.14 в Docker.

1. **Без quota project — 403.** `sites().list()` с
   `Credentials.from_authorized_user_file(path, scopes=[webmasters.readonly])`
   вернул 403. Тело ответа дословно:

   ```json
   {"error": {"code": 403,
     "message": "Your application is authenticating by using local Application
       Default Credentials. The searchconsole.googleapis.com API requires a
       quota project, which is not set by default.",
     "errors": [{"reason": "accessNotConfigured", "domain": "usageLimits"}],
     "status": "PERMISSION_DENIED"}}
   ```

2. **С quota project — работает.** Тот же вызов после
   `creds.with_quota_project("graphic-ripsaw-271510")` вернул 95 свойств.
   Значит креды, скоуп `webmasters.readonly` и доступ к API исправны, а
   единственное недостающее звено — quota project.

3. **Настоящий отказ в доступе выглядит ИНАЧЕ.** `searchanalytics().query()` по
   чужому свойству `https://example.com/` с тем же quota project дал 403 с
   `"reason": "forbidden"`, `"domain": "global"` и сообщением «User does not
   have sufficient permission for site …». То есть два разных 403 различимы по
   полю `reason`, и это ПРОВЕРЕНО, а не предположено.

4. **`with_quota_project` есть у обоих типов кредов.** Метод объявлен на
   `google.auth.credentials.Credentials` и возвращает НОВЫЙ объект — результат
   обязателен к присваиванию, иначе правка не действует.

5. **Существующий тест 403 подаёт ПУСТОЕ тело.** `_http_error` в
   `tests/test_search_console_provider.py:403` строит
   `HttpError(SimpleNamespace(status=status, reason="test"), b"{}")`, и
   `test_errors_are_translated_by_http_status` ждёт для 403
   `AuthenticationError`. Поэтому 403 без распознаваемого `reason` ОБЯЗАН
   остаться `AuthenticationError` — это и безопасный дефолт, и условие того,
   что существующий тест не сломается.

6. **Новое поле настроек ничего не ломает.** Ни один тест не сверяет набор
   полей `Settings` на равенство (проверено командой из шаблона по `tests/`);
   переменная окружения выводится автоматически как `GKAI_<ИМЯ_ПОЛЯ>`
   (`_environment_values`, `config.py:259`), маскируется только `SecretStr`
   (`masked_dump`, `config.py:289`). Id проекта секретом не является, поэтому
   тип — `str | None`, а не `SecretStr`.

7. Не подтверждено и потому НЕ реализовывать: поведение при
   `service_account` + quota project вживую не проверялось (у сервисного
   аккаунта свой проект, и quota project ему обычно не нужен). Код обязан
   применять настройку одинаково к обоим типам, но живое доказательство есть
   только для `authorized_user`.

## Что делать

### 1. Настройка `search_console_quota_project_id`

В `src/google_keyword_ai/config.py`, в классе `Settings`, рядом с остальными
полями Search Console (после `search_console_credentials_path`) добавить:

```python
search_console_quota_project_id: str | None = None
```

Переменная окружения `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID` подхватывается
существующим механизмом, отдельного кода для неё писать НЕ надо.

Добавить валидатор в стиле соседних: значение из одних пробелов — это ошибка
конфигурации, а не «не задано». Пустая строка после `strip()` обязана поднимать
`InvalidConfigurationError` с внятным текстом. Непустое значение сохраняется
обрезанным по краям (`strip()`). Значение `None` остаётся `None` и валидатор
его не трогает.

Причина, по которой пустое значение НЕЛЬЗЯ молча считать «не задано»:
`GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID=` в окружении — это опечатка человека,
который думает, что настройку выставил; молчаливое игнорирование вернёт ему
ровно тот 403, ради которого веха и пишется.

### 2. Применение quota project к кредам

В `src/google_keyword_ai/providers/search_console.py`, в методе `_load_with`
(он общий для обоих типов кредов и уже оборачивает `ValueError`), после
успешного построения кредов применить quota project, если он задан:

- если `self._settings.search_console_quota_project_id` равен `None` —
  вернуть креды КАК ЕСТЬ, `with_quota_project` не звать вовсе;
- иначе вернуть `credentials.with_quota_project(<значение>)` — именно
  ВОЗВРАЩАЕМЫЙ объект, метод не изменяет исходный;
- если у объекта кредов нет метода `with_quota_project` (подделка в тестах,
  экзотический тип), поднять `InvalidConfigurationError` с текстом о том, что
  этот тип кредов quota project не поддерживает. Молча игнорировать нельзя:
  это вернёт пользователя к неотличимому 403.

`_load_with` — статический метод; чтобы прочитать настройки, сделать его
обычным методом (`self`) или передавать значение параметром. Оба варианта
допустимы, выбирай любой; вызывающие строки в `load_credentials` поправить
соответственно. Разбор `credential_type` и текст существующих ошибок не менять.

### 3. Разбор 403 по причине отказа

В том же файле, в `_translate_http_error`, для статуса 403 (и только для него;
401 остаётся как есть) прочитать тело ответа и посмотреть `reason` первого
элемента `error.errors`:

- `reason == "accessNotConfigured"` → `InvalidConfigurationError`, текст обязан
  называть quota project и настройку `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID`;
- любой другой `reason`, отсутствующее тело, нечитаемый JSON, тело не-объект,
  пустой список `errors` → прежний `AuthenticationError` с прежним текстом.

Тело берётся из атрибута `content` исключения (`bytes` либо `str`); разбор
обязан быть устойчив к мусору — ни одного нового исключения наружу.
`InvalidConfigurationError` уже импортирован в этом файле.

Порядок проверок важен: сначала распознать `accessNotConfigured`, и только
не распознав — вернуть `AuthenticationError`. Обратный порядок сделает первую
ветку недостижимой.

### 4. Тесты

Все новые тесты — в существующих файлах, новых файлов не создавать:

- `tests/test_config.py` — настройка и её валидатор;
- `tests/test_search_console_provider.py` — применение quota project и разбор
  403. Для «кредов» использовать подделку в стиле файла: объект с методом
  `with_quota_project`, возвращающим НОВЫЙ объект с записанным id, чтобы тест
  доказывал именно присваивание результата, а не сам факт вызова.

Подделка обязана вести себя как настоящий объект: `with_quota_project` НЕ
меняет исходный объект, а возвращает новый. Подделка, меняющая себя на месте,
пропустит ровно ту ошибку, ради которой пишется тест.

### 5. Документация

В `docs/search-console.md` добавить раздел про quota project: зачем нужен,
когда обязателен (креды `authorized_user`), имя настройки, и что при его
отсутствии Google отвечает 403 `accessNotConfigured`. Привести дословный текст
ошибки Google из «Проверенных фактов», пункт 1.

В `CHANGELOG.md` — запись в существующем стиле файла.

## Не трогать

- `src/google_keyword_ai/providers/google_ads.py` и вообще всё, что не Search
  Console: Ads-половина живой проверки не пройдена и вехой не закрывается.
- Разбор ответов, кэш, троттлинг, пагинация, `dataState`, `_as_date`,
  `_validate_data_state`, `is_available()` и `build_service()` в
  `search_console.py` — кроме строк, названных в «Что делать».
  `is_available()` в частности НЕ должен начать требовать quota project:
  сервисному аккаунту он не нужен, и такое требование сломает рабочий путь.
- Существующие тесты в `tests/test_search_console_provider.py`. **Исключение,
  разрешённое явно:** если для новой сигнатуры `_load_with` придётся поправить
  ВЫЗОВ в существующем тесте — правь только вызов, утверждения теста не
  трогай. Ожидание «403 → `AuthenticationError`» в
  `test_errors_are_translated_by_http_status` обязано остаться и проходить
  (см. «Проверенные факты», пункт 5).
- `pyproject.toml`, `uv.lock` — новых зависимостей не добавлять. Всё нужное
  (`google-auth`, `google-api-python-client`) уже стоит.
- `src/google_keyword_ai/__init__.py` (версия) — не бампать.
- Не создавать файлов, которых нет в разделе «Что делать»: ни планов, ни ADR,
  ни отдельных тестовых модулей.
- Не читать и не выполнять посторонние скилы и плейбуки.
- `go clean`, `rm -f`, `rm -rf` не использовать.

## Что исполнитель может проверить

- **Проверяется на хосте (идёт в `AC-*`):** ВСЁ, что есть в вехе. Проверено
  заранее: в клоне лежит прогретый `.venv` (`isolate.sh` греет `uv sync`),
  `google-auth` и `google-api-python-client` установлены, весь набор тестов
  проходит офлайн, сети не требует ни один тест. Команды писать через
  `.venv/bin/python`.
- **Проверяется только вживую (идёт в `HC-*`):** любой настоящий вызов Google.
  Причина: у исполнителя нет сети и нет кредов владельца.
- **Данных, которых нет в клоне, веха не требует.** Каталогов вне git у этого
  проекта нет; `tests/fixtures/trends/` лежит в git и вехой не используется.

## Критерии приёмки

AC-009 агрегирующий; при его провале остальные критерии всё равно выполнить и
отчитаться по каждому.

- **AC-001.** `Settings` принимает `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID` и
  кладёт значение в поле `search_console_quota_project_id`.
  Проверка: `.venv/bin/python -m pytest tests/test_config.py -q -k quota_project_id_is_read_from_the_environment; echo EXIT:$?`
- **AC-002.** Значение из одних пробелов отвергается как
  `InvalidConfigurationError`, а не считается незаданным.
  Проверка: `.venv/bin/python -m pytest tests/test_config.py -q -k blank_quota_project_is_refused; echo EXIT:$?`
- **AC-003.** При заданном quota project креды типа `authorized_user`
  возвращаются ИМЕННО те, что вернул `with_quota_project` (проверяется по
  записанному id в возвращённом объекте, а не по факту вызова).
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k authorized_user_credentials_get_the_quota_project; echo EXIT:$?`
- **AC-004.** То же самое для кредов типа `service_account`.
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k service_account_credentials_get_the_quota_project; echo EXIT:$?`
- **AC-005.** При НЕзаданном quota project `with_quota_project` не зовётся
  вовсе, и креды возвращаются нетронутыми.
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k credentials_are_untouched_without_a_quota_project; echo EXIT:$?`
- **AC-006.** 403 с `reason: accessNotConfigured` переводится в
  `InvalidConfigurationError`, и текст называет
  `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID`.
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k access_not_configured_is_a_configuration_error; echo EXIT:$?`
- **AC-007.** 403 с `reason: forbidden` остаётся `AuthenticationError`.
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k forbidden_stays_an_authentication_error; echo EXIT:$?`
- **AC-008.** 403 с нечитаемым или пустым телом остаётся `AuthenticationError`
  и не поднимает никакого нового исключения.
  Проверка: `.venv/bin/python -m pytest tests/test_search_console_provider.py -q -k an_unreadable_403_body_stays_an_authentication_error; echo EXIT:$?`
- **AC-009.** Весь набор тестов зелёный (агрегирующий критерий).
  Проверка: `.venv/bin/python -m pytest -q; echo EXIT:$?`
- **AC-010.** Линтер чист.
  Проверка: `.venv/bin/python -m ruff check .; echo EXIT:$?`
- **AC-011.** Форматирование чисто.
  Проверка: `.venv/bin/python -m ruff format --check .; echo EXIT:$?`
- **AC-012.** Типы чисты.
  Проверка: `.venv/bin/python -m mypy; echo EXIT:$?`

## Проверки принимающего

- **HC-001.** Живой `gkai gsc properties` с настоящими кредами владельца и
  заданным quota project возвращает непустой список свойств, а не 403.
  Как: прогон в Docker с `GKAI_SEARCH_CONSOLE_CREDENTIALS_PATH` и
  `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID`, сверка с 95 свойствами из шага
  ресерча.
- **HC-002.** Живой `gkai gsc properties` БЕЗ quota project даёт конверт с
  внятной причиной про quota project, а не «authentication failed».
  Как: тот же прогон без `GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID`.
- **HC-003.** Полный набор тестов в Docker на 3.12 и 3.14.
- **HC-004.** Мутационная проверка новых тестов: снять применение quota
  project, снять присваивание результата `with_quota_project`, перевернуть
  порядок веток разбора 403 — каждая мутация обязана убить СВОЙ тест.

Исполнителю: `HC-*` не выполняй и в `unverified` не перечисляй; если
упоминаешь — начинай пункт с его id.

## Контракт на невыполнимое

> Если два требования противоречат друг другу, два пина несовместимы или
> требование невыполнимо в этом окружении — **ОСТАНОВИСЬ и доложи** в поле
> `blocked_reason` финального отчёта, приложив доказательство. Обходить
> несовместимость через `--no-deps`, `--force-reinstall`, вендоринг, подмену
> тулчейна, отключение проверок или игнор constraints запрещено. Остановка с
> внятной причиной — правильный исход.
>
> Если одна и та же команда упала дважды с той же ошибкой — не запускай её в
> третий раз без смены подхода. Если другого подхода нет — остановись и доложи.

## Разрешения

> Подтверждений не запрашивай: режим неинтерактивный, отвечать некому. СПЕКА И
> ЕСТЬ утверждённый дизайн. Шаг согласования, брейншторма или ревью с человеком
> из любых загруженных скилов пропусти и переходи к реализации.

- Создавать и править файлы в: `src/google_keyword_ai/config.py`,
  `src/google_keyword_ai/providers/search_console.py`, `tests/test_config.py`,
  `tests/test_search_console_provider.py`, `docs/search-console.md`,
  `CHANGELOG.md`.
- Запускать: `.venv/bin/python -m pytest`, `.venv/bin/python -m ruff`,
  `.venv/bin/python -m mypy`, `git status`, `git diff`, `git add`,
  `git commit`.
- Сеть: **не использовать вообще.** Никаких обращений наружу: ни к реестрам
  пакетов, ни к API, ни `git push`. Всё нужное уже лежит в дереве. Тестов,
  ходящих в настоящий Google, не писать ни одного — живую проверку делает
  человек на приёмке.
- Docker: **не использовать вообще, ни одной командой** — ни `docker`, ни
  `compose`, ни обращений к сокету. Тесты, которым нужен внешний сервис, ты НЕ
  поднимаешь и НЕ прогоняешь; прогон против живого стенда делает человек на
  приёмке. Попытка поднять сервис самому считается провалом задачи, а не
  находчивостью.
- Порты: слушателей не открывать вовсе — вехе они не нужны.
- Git: коммиты в клоне допустимы; `push` запрещён.
- **Нельзя ни при каких условиях:** писать в боевые эндпоинты, трогать чужие
  контейнеры, выполнять push, печатать или сохранять секреты. Настоящих кредов
  в клоне нет и быть не должно — если найдёшь файл, похожий на креды,
  остановись и доложи.

## Формат отчёта

Финальный ответ — **ТОЛЬКО JSON-объект**: без прозы до и после, без
```-ограждений.

```json
{
  "summary": "одно-два предложения: что сделано",
  "criteria": [
    {
      "id": "AC-001",
      "status": "pass",
      "evidence_command": ".venv/bin/python -m pytest tests/test_config.py -q -k quota_project_id_is_read_from_the_environment; echo EXIT:$?",
      "evidence_output": "1 passed in 0.30s\nEXIT:0"
    }
  ],
  "deviations": [{"what": "…", "why": "…"}],
  "unverified": [{"what": "…", "why": "…"}],
  "blocked": false,
  "blocked_reason": "",
  "files_touched": ["src/google_keyword_ai/config.py"]
}
```

Имена полей ровно такие: `evidence_command` и `evidence_output`, не `command`
и не `output`. Статус — из набора `pass` / `fail` / `not_attempted`.
