# Anchored demand implementation plan

**Goal:** Compare up to 50 unique keywords using a common Google Trends anchor.
**Spec:** `docs/specs/m18-anchored-demand.md` (approved task specification).
**Architecture:** Pure batch planning and arithmetic in `demand.py`; sequential
provider orchestration in `usecases/demand.py`; existing CLI/MCP envelope facades.
**Tech stack:** Prepared Python 3.14 environment, Pydantic, anyio, respx, pytest.

## Constraints

Work only in this clone on `m18-anchored-demand`, without push or rebase.
Run `.venv/bin/…` directly, with no installs, Docker, or live requests.
Preserve providers, existing tests, Trends golden fixtures, dependencies, and AGENTS.md.
Commit the supplied spec and demand fixtures. Keep `report.json` outside the commit.

## Steps

- [x] Add `tests/test_demand.py`, verify failure, implement pure `demand.py`.
  `plan_batches(keywords, anchor)` normalizes and deduplicates; validates 2–50
  unique nonempty keys and anchor membership. `DemandBatch` carries planned
  keywords (anchor first), a parsed result or verbatim failure reason.
  `measured_mean(result, keyword)` reads measured complete weeks by name.
  `combine(batches)` preserves participant order, emits one anchor (first
  usable batch, or first failed batch when none usable), and stably sorts nulls last.
  Fixtures verify 28.17680 and 28.12500 percent; synthetic tests exercise seven mutants.
  Run `.venv/bin/python -m pytest tests/test_demand.py -q`.
- [x] Add `tests/test_demand_usecase.py`, verify failure, implement
  `usecases/demand.py` with shared client/cache/provider and sequential fetches.
  Catch the four specified provider errors per batch. Keep irrelevant widget
  messages in `DemandData.notices`; derive completeness from rows and failures.
  Return nullable data on invalid input. Test real HTTP parsing through respx,
  failed first/middle/all batches, widget errors, normalization, and cache reuse.
  Run `.venv/bin/python -m pytest tests/test_demand_usecase.py -q`.
- [x] Add CLI/MCP parity cases, verify failure, register `gkai demand` and
  synchronous `rank_keyword_demand`. Use `_finish` and `_widen`. CLI notices
  follow the envelope on stderr. Verify nullable JSON refusals and table output.
  Run `.venv/bin/python -m pytest tests/test_mcp_parity.py tests/test_demand_usecase.py -q`.
- [x] Document method and caveats in `docs/demand.md`, add tool and count in
  `docs/mcp.md`, command in skill CLI reference, changelog and CLAUDE structure.
- [x] Build `tests/mutation_gate_demand.py` with seven independent target tests,
  green baseline before each mutation, unique source replacements, exact failing
  assertion verification, byte/sha256 restoration in finally, and no stale pyc.
  Run self-check, then all mutations sequentially, without concurrent edits/tests.
## Delivery verification

Independent review completed; its zero-keyword CLI finding was reproduced and
fixed with a passing regression test. The existing MCP schema test now expects
15 tools and also verifies the new tool's parameters and nullable output; its
previous schema assertions remain intact. Run AC-001–AC-009, commit task files,
verify the clean tree, and record exactly nine evidence entries in `report.json`.
No push. The report is the authoritative record of final acceptance commands.
