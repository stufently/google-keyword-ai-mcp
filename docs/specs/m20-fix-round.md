# M20 — круг правок по перекрёстному ревью

Веха M20 принята по коду. Перекрёстный мутационный прогон (Grok, 16 мутаций)
нашёл **дыры в тестах**, не дефекты боевого кода. Это единственный круг правок:
дальнейшие замечания уходят в бэклог.

## Где работать

Клон `/home/deploy/exec-clones/gkai-m20-research-demand`, ветка
`m20-research-demand`, HEAD `d4fc960`. База: `663 passed, 4 deselected`.
Окружение готово: `.venv` с Python 3.14, команды — напрямую из `.venv/bin/`.
Сети нет, Docker запрещён, ничего не устанавливать.

Сама эта спека (`docs/specs/m20-fix-round.md`) приезжает в клон untracked —
включи её в свой коммит, иначе дерево не станет чистым.

## Что именно не покрыто (проверено мной, не со слов ревьюера)

Каждая мутация ниже наложена мной вручную, прогнана и откачена со сверкой sha256.

| # | Правка боевого кода | Результат |
|---|---|---|
| A | `cli/main.py:409`: `demand … = False` → `True` | **весь набор зелёный, 663 passed** |
| B | `mcp/server.py:253`: `demand: bool = False` → `True` | **весь набор зелёный, 663 passed** |
| C | `mcp/server.py:301` (`plan_research`), то же | **весь набор зелёный, 663 passed** |
| D | `usecases/research.py`: ветка `truncated_by_budget` в конверте снята | новые тесты вехи ловят, но ассертом **паритета** (`test_research_demand_facades.py:152`), а `test_budget_truncation_is_partial_and_named` остаётся зелёным |
| E | `usecases/research.py`: из `missing_demand` убрано `keyword.demand_status is not None` | **все 35 новых тестов вехи зелёные**; падает только чужой `tests/test_runs_usecase.py` |

A, B, C — столбец спроса можно включить всем и навсегда, и набор этого не
заметит. Ровно то, что спека запрещала: «без флага прогон обязан остаться
прежним по стоимости и по форме».

E — по правилу «мутации гоняются по НОВЫМ тестам вехи отдельно» этот мутант
выживает: поведение ловит только соседний файл. Дыра адресности внутри вехи.

## Не трогать

- **Боевой код не менять вообще.** Все пять пунктов — дыры в тестах. Если
  какой-то тест невозможно написать без правки `src/`, это контракт на
  невыполнимое: остановись и доложи, не правь код «заодно».
- `tests/fixtures/**` — не трогать.
- Существующие зелёные тесты не ослаблять и не удалять; усиление
  `test_budget_truncation_is_partial_and_named` — только добавлением условий.
- `git pull`, смена ветки, ребейз, пуш — запрещены.

## Разрешено

`tests/test_research_demand.py`, `tests/test_research_demand_facades.py`,
`tests/test_facade_errors.py`, `tests/mutation_gate_demand.py`.

## Факты, на которые опираться (проверены)

- Без спроса `estimated_trends_calls = min(1, budget.max_trends_calls)` — при
  бюджете по умолчанию 3 это **1**. Со спросом `usecases/research.py:453`
  ставит его равным `active_budget.max_trends_calls` — **3**, и в `plan.steps`
  добавляется строка `Rank candidate demand after sorting, excluding the seed`.
  Это надёжный различитель dry-run с флагом и без.
- MCP публикует для обоих инструментов
  `input_schema["properties"]["demand"] == {"default": False, "title": "Demand",
  "type": "boolean"}` — проверено живым `list_tools`. Ассерт по `default`
  убивает мутации B и C сам по себе.
- `tests/test_facade_errors.py` уже умеет читать опубликованные схемы через
  хелпер `list_tools` (см. `test_the_error_guard_leaves_the_published_schemas_alone`).

## Критерии приёмки

Команды — из `.venv/bin/` в корне клона. Сети нет, Docker запрещён.

- **AC-001.** Новый тест: CLI `research <target>` **без** `--demand` доезжает до
  `run_research` с `demand is False` и `demand_anchor is None`. Мокать фасад
  целиком, не глядя на kwargs, не считается — kwargs проверить явно.
  `.venv/bin/python -m pytest -q tests/test_research_demand_facades.py`
- **AC-002.** Новый тест dry-run: без флага `estimated_trends_calls == 1` и
  строки про спрос нет в `plan.steps`; с `--demand` — `== 3` и строка есть.
  Бюджет в обоих случаях дефолтный.
  `.venv/bin/python -m pytest -q tests/test_research_demand_facades.py`
- **AC-003.** Новый тест: `research_keywords` и `plan_research`, вызванные через
  `tool.fn` **без** аргумента `demand`, доезжают до юзкейса с `False`.
  `.venv/bin/python -m pytest -q tests/test_research_demand_facades.py`
- **AC-004.** В `tests/test_facade_errors.py` к разбору опубликованных схем
  добавлена сверка: у `research_keywords` и у `plan_research`
  `input_schema["properties"]["demand"]["default"] is False`.
  `.venv/bin/python -m pytest -q tests/test_facade_errors.py`
- **AC-005.** `test_budget_truncation_is_partial_and_named` усилен: в конверт
  заранее попадает warning, **не связанный** с бюджетом, и тест требует, чтобы
  `completeness_reason` всё равно называл `max_trends_calls`. Тест обязан падать
  при снятой ветке обрезки — то есть доказывать приоритет ветки, а не сам факт
  `partial`.
  `.venv/bin/python -m pytest -q tests/test_research_demand.py`
- **AC-006.** Тест на `enabled=False` внутри вехи требует, чтобы конверт был
  `Completeness.COMPLETE`: у всех ключей `demand_status is None`, и это НЕ
  «ranked без значения». Мутация E обязана убиваться **новыми тестами вехи**, а
  не соседним файлом.
  `.venv/bin/python -m pytest -q tests/test_research_demand.py`
- **AC-007.** Пять мутантов A–E добавлены в `tests/mutation_gate_demand.py` тем
  же обратимым способом, что и остальные (sha256 до и после, откат в `finally`,
  якорь ровно один раз, целевой тест зелёный на чистом дереве). Гейт проходит и
  печатает `MUTATION GATE PASSED: 26`.
  `.venv/bin/python tests/mutation_gate_demand.py`
- **AC-008.** Весь набор зелёный, счёт вырос относительно 663.
  `.venv/bin/python -m pytest -q`
- **AC-009.** Линтеры чисты.
  `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- **AC-010.** Работа закоммичена ОДНИМ коммитом в `m20-research-demand`
  (вместе с файлом спеки), дерево чистое кроме `report.json`. Сообщение
  ≤50 символов, императив, без трейлеров.
  `bash -c 'cd /home/deploy/exec-clones/gkai-m20-research-demand && test -z "$(git status --porcelain -- . ":(exclude)report.json" ":(exclude)report-blocked.md")"'`

## Контракт на невыполнимое

Если какой-то критерий требует правки боевого кода, или мутант из A–E не
убивается написанным тестом, или гейт не сходится — **остановись и доложи в
`report.json`** со статусом `blocked` и точной причиной. Обходить
несовместимость, глушить код возврата, помечать тест `skip`/`xfail`, ослаблять
ассерты — запрещено.

## Контракт отчёта

Положи в корень клона `report.json` строго такого вида:

```json
{"criteria": [
  {"id": "AC-001", "status": "pass", "command": "<команда-доказательство>",
   "rc": 0, "note": "коротко, что получилось"}
]}
```

Статусы: `pass` / `fail` / `blocked`. По одной записи на КАЖДЫЙ критерий
AC-001…AC-010, ни одного лишнего. `command` — та команда, которой критерий
реально доказан, целиком, без `|| true` и без подмены окружения: при приёмке
она перезапускается мной. Заявленный `pass` без выполненной команды снимает
всю работу.
