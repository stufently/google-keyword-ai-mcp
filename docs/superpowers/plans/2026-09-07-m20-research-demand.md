# M20 Research Demand Implementation Plan

**Goal:** Add optional relative demand to all research scenarios.

**Architecture:** A pure selector excludes the normalized seed and chooses an
anchor plus at most four participants per remaining Trends call. The shared
async result assembly enriches sorted rows using the existing demand combiner.
CLI, synchronous MCP, saved runs and Markdown consume the same result.

**Tech Stack:** Python 3.12–3.14, Pydantic, AnyIO, Typer, MCP, pytest.

**Spec:** `docs/specs/m20-research-demand.md` (approved task).

## Constraints

Work only in this prepared clone on `m20-research-demand`, based on `7423ea9`.
No network, Docker, push, pull, rebase or dependency installation. Run existing
`.venv/bin/` tools. Preserve providers, fixtures, dependencies and AGENTS.md.
Keep existing tests intact. Commit the supplied specification with the work.

## Execution

- [x] Add failing tests in `tests/test_research_demand.py`: normalized seed
  exclusion, first anchor, nine rows in two batches, exact spend, truncation,
  zero budget, all statuses, runtime refusal and explicit anchor validation.
  Run `.venv/bin/python -m pytest -q tests/test_research_demand.py`.
- [x] Implement `select_research_candidates(keywords, seed, batches, *, anchor)`
  in `demand.py`, returning `(anchor | None, participants)`. Add nullable row
  fields and `ResearchDemandStats` in `pipeline/models.py`. Make
  `_research_data` asynchronous; run the shared demand step after sorting and
  before copying spend/quality. Use `DemandBatch` and `combine`, preserving
  the standalone demand command's behavior.
- [x] Add facade tests and wire `demand=False`, `demand_anchor=None` through
  `usecases/research.py`, CLI and both research/planning MCP facades. An anchor
  implies demand. Persist options in the existing config snapshot, include
  them in requested-stage fingerprints, and restore on resume/rerun; test
  saved-run replay without changing the database schema.
- [x] Add renderer tests and a conditional demand table in
  `reports/markdown.py`: explicitly handle five statuses, separate unranked
  rows, and raise `ValueError` naming an unknown status.
- [x] Extend `tests/mutation_gate_demand.py` with M22–M27, each tied to a
  specific assertion in the new tests. Keep all 21 original mutations.
- [x] Update pipeline/demand/CLI/MCP docs, CHANGELOG and CLAUDE structure;
  document seed experiment, budgets, statuses and saved-run behavior.
- [x] Run AC-001 through AC-008, plus the original M17 mutation gate; review
  the diff, commit task files and supplied spec, and write eight evidence
  records to uncommitted `report.json`. Verify the allowed clean-tree check.

Validation: 663 tests passed (4 live integration tests deselected); ruff and
mypy clean; all 27 demand mutants and all 6 original M17 mutants killed.
Existing test functions were not modified. Independent review found one
ancillary-diagnostic persistence issue, corrected and covered by replay tests.
Acceptance evidence is recorded in the root `report.json`; delivery is local only.
