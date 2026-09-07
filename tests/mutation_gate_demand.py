"""Thirty-two reversible demand mutations (M18-M20).

Run sequentially, never alongside edits/tests.

Each mutant must fail its own test at its designated assertion. Every target
first passes on the original source, including when running with --only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/google_keyword_ai/demand.py"
TEST_FILE = "tests/test_demand.py"
USECASE_SOURCE = ROOT / "src/google_keyword_ai/usecases/demand.py"
USECASE_TEST_FILE = "tests/test_demand_usecase.py"
CONFIG_SOURCE = ROOT / "src/google_keyword_ai/config.py"
RESEARCH_SOURCE = ROOT / "src/google_keyword_ai/usecases/research.py"
RESEARCH_TEST_FILE = "tests/test_research_demand.py"
RESEARCH_FACADE_TEST_FILE = "tests/test_research_demand_facades.py"
MCP_SOURCE = ROOT / "src/google_keyword_ai/mcp/server.py"


@dataclass(frozen=True)
class Mutation:
    id: str
    anchor: str
    replacement: str
    test: str
    assertion: str
    source: Path = SOURCE
    test_file: str = TEST_FILE

    @property
    def nodeid(self) -> str:
        return f"{self.test_file}::{self.test}"


MUTATIONS = (
    Mutation(
        "M1",
        "index = result.keywords.index(keyword)",
        "index = 0",
        "test_anchor_is_found_by_name_at_index_two",
        "assert participant.relative_demand == pytest.approx(28.17680, abs=0.00001)",
    ),
    Mutation(
        "M2",
        "relative = mean / anchor_mean * 100",
        "relative = mean / max(value for point in result.timeline for value in point.values) * 100",
        "test_batch_uses_anchor_mean_instead_of_batch_maximum",
        "assert rows[0].relative_demand == 200.0",
    ),
    Mutation(
        "M3",
        "            if mean is None:\n",
        "            if mean is None:\n                relative = 0.0\n",
        "test_unmeasured_participant_is_unknown_not_zero",
        "assert row.relative_demand is None",
    ),
    Mutation(
        "M4",
        "anchor_mean = measured_mean(result, anchor) if result is not None else None",
        "anchor_mean = (measured_mean(result, anchor) or 1.0) if result is not None else None",
        "test_anchor_collapse_invalidates_the_whole_batch",
        "assert all(row.relative_demand is None for row in rows)",
    ),
    Mutation(
        "M5",
        "        and point.has_data[index]\n",
        "",
        "test_mean_uses_only_measured_whole_weeks",
        'assert measured_mean(result, "small") == 20.0',
    ),
    Mutation(
        "M6",
        "MAX_DEMAND_KEYWORDS = 50",
        "MAX_DEMAND_KEYWORDS = 51",
        "test_fifty_one_keywords_are_refused",
        "assert reason is not None",
    ),
    Mutation(
        "M7",
        "(row.relative_demand is None, -(row.relative_demand or 0))",
        "(False, -(row.relative_demand or 0))",
        "test_nulls_sort_last_in_input_order_even_beside_measured_zero",
        'assert [row.keyword for row in rows] == ["anchor", "zero", "unknown1", "unknown2"]',
    ),
    Mutation(
        "M8",
        "elif anchor_row.relative_demand is None and row.relative_demand is not None:",
        "elif True:",
        "test_anchor_keeps_first_usable_batch_coverage",
        "assert (anchor.batch, anchor.weeks, anchor.measured_weeks) == (1, 2, 2)",
    ),
    Mutation(
        "M9",
        'RELATIVE_CAVEAT = "Values are relative to the anchor, not absolute search volumes."',
        'RELATIVE_CAVEAT = "Values are absolute search volumes."',
        "test_caveat_constants_keep_their_exact_meaning",
        "assert caveats == expected",
        source=USECASE_SOURCE,
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M10",
        'timeframe: str = "today 12-m",',
        'timeframe: str = "today 5-y",',
        "test_usecase_default_timeframe_reaches_data_and_http",
        'assert result.data.timeframe == "today 12-m"',
        source=USECASE_SOURCE,
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M11",
        "coverage < min_coverage",
        "coverage <= min_coverage",
        "test_coverage_exactly_at_the_threshold_is_emitted",
        "assert row.relative_demand == 200.0",
    ),
    Mutation(
        "M12",
        "relative is not None and not is_anchor and coverage < min_coverage",
        "relative is not None and coverage < min_coverage",
        "test_coverage_threshold_does_not_cut_the_anchor",
        "assert anchor.relative_demand == 100.0",
    ),
    Mutation(
        "M13",
        "relative is not None and not is_anchor and coverage < min_coverage",
        "not is_anchor and coverage < min_coverage",
        "test_coverage_threshold_does_not_overwrite_an_existing_null_reason",
        'assert "below the anchor\'s resolution" in row.reason',
    ),
    Mutation(
        "M14",
        "demand_min_coverage: float = 0.25",
        "demand_min_coverage: float = 0.5",
        "test_demand_min_coverage_defaults_to_a_quarter",
        "assert Settings().demand_min_coverage == 0.25",
        source=CONFIG_SOURCE,
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M15",
        "status = DemandStatus.LOW_COVERAGE",
        "status = DemandStatus.BELOW_RESOLUTION",
        "test_coverage_just_below_the_threshold_is_cut",
        "assert row.status == DemandStatus.LOW_COVERAGE",
    ),
    Mutation(
        "M16",
        "numeric = sum(row.relative_demand is not None for row in data.rows)",
        "numeric = sum(1 for row in data.rows if row.relative_demand)",
        "test_measured_zero_keeps_the_envelope_complete",
        "assert result.completeness is Completeness.COMPLETE",
        source=USECASE_SOURCE,
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M17",
        "relative_demand=relative,",
        "relative_demand=relative or None,",
        "test_measured_zero_survives_combine_usecase_cli_and_mcp",
        'assert zero["relative_demand"] == 0.0',
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M18",
        "if relative is not None and not is_anchor and coverage < min_coverage:",
        "if relative and not is_anchor and coverage < min_coverage:",
        "test_thin_measured_zero_is_cut_until_threshold_is_disabled",
        "assert row.relative_demand is None",
    ),
    Mutation(
        "M19",
        "rows=combine(batches, min_coverage=settings.demand_min_coverage),",
        "rows=combine(batches, min_coverage=0.25),",
        "test_usecase_zero_threshold_emits_a_single_week_of_fifty_three",
        "assert thin.relative_demand == 50.0",
        source=USECASE_SOURCE,
        test_file=USECASE_TEST_FILE,
    ),
    Mutation(
        "M20",
        "coverage < min_coverage",
        "round(coverage, 2) < min_coverage",
        "test_coverage_fraction_is_not_rounded_before_the_threshold",
        "assert row.relative_demand is None",
    ),
    Mutation(
        "M21",
        'MEASURED = "measured"',
        'MEASURED = "observed"',
        "test_demand_status_protocol_literals_are_fixed",
        'assert DemandStatus.MEASURED.value == "measured"',
    ),
    Mutation(
        "M22",
        "and key != normalized_seed",
        "and True",
        "test_selector_excludes_normalized_seed",
        'assert "seed" not in [anchor, *participants]',
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "M23",
        "selected_anchor = candidates[0] if anchor is None else anchor",
        "selected_anchor = candidates[-1] if anchor is None else anchor",
        "test_selector_uses_top_candidate_as_anchor",
        'assert anchor == "top"',
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "M24",
        "capacity = 1 + KEYWORDS_PER_BATCH * batches",
        "capacity = KEYWORDS_PER_BATCH * batches",
        "test_selector_reserves_room_for_anchor",
        "assert len([anchor, *participants]) == 9",
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "M25",
        '        guard.spend("trends")\n',
        "",
        "test_demand_batches_are_recorded_in_spend",
        "assert data.stats.spend.trends_calls == 3",
        source=ROOT / "src/google_keyword_ai/pipeline/scenarios.py",
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "M26",
        "if 1 + len(participants) < stats.requested:",
        "if False:",
        "test_budget_truncation_is_partial_and_named",
        "assert data.stats.demand.truncated_by_budget is True",
        source=ROOT / "src/google_keyword_ai/pipeline/scenarios.py",
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "M27",
        '    raise ValueError(f"Unknown demand status: {status!r}")',
        '    return "unavailable"',
        "test_renderer_refuses_unknown_demand_status",
        "assert reason is not None",
        source=ROOT / "src/google_keyword_ai/reports/markdown.py",
        test_file="tests/test_research_demand.py",
    ),
    Mutation(
        "A",
        '    demand: Annotated[bool, typer.Option("--demand")] = False,',
        '    demand: Annotated[bool, typer.Option("--demand")] = True,',
        "test_cli_omitted_demand_reaches_usecase_disabled",
        'assert run.call_args.kwargs["demand"] is False',
        source=ROOT / "src/google_keyword_ai/cli/main.py",
        test_file=RESEARCH_FACADE_TEST_FILE,
    ),
    Mutation(
        "B",
        "    def research_keywords(\n        target: str,\n        demand: bool = False,",
        "    def research_keywords(\n        target: str,\n        demand: bool = True,",
        "test_mcp_omitted_demand_reaches_usecase_disabled[research_keywords]",
        'assert run.call_args.kwargs["demand"] is False',
        source=MCP_SOURCE,
        test_file=RESEARCH_FACADE_TEST_FILE,
    ),
    Mutation(
        "C",
        "    def plan_research(\n        target: str,\n        demand: bool = False,",
        "    def plan_research(\n        target: str,\n        demand: bool = True,",
        "test_mcp_omitted_demand_reaches_usecase_disabled[plan_research]",
        'assert run.call_args.kwargs["demand"] is False',
        source=MCP_SOURCE,
        test_file=RESEARCH_FACADE_TEST_FILE,
    ),
    Mutation(
        "D",
        "    if data.stats.demand is not None and data.stats.demand.truncated_by_budget:\n"
        "        return Envelope(\n"
        "            data=data,\n"
        "            warnings=reported,\n"
        "            errors=errors,\n"
        "            completeness=Completeness.PARTIAL,\n"
        '            completeness_reason=f"Demand subset truncated by '
        '{data.stats.stopped_by} budget.",\n'
        "            run_id=run_id,\n"
        "        )\n",
        "",
        "test_budget_truncation_is_partial_and_named",
        'assert "max_trends_calls" in (envelope.completeness_reason or "")',
        source=RESEARCH_SOURCE,
        test_file=RESEARCH_TEST_FILE,
    ),
    Mutation(
        "E",
        "if keyword.demand_status is not None and keyword.demand_relative is None",
        "if keyword.demand_relative is None",
        "test_all_scenarios_share_optional_demand[False-niche]",
        "assert envelope.completeness is Completeness.COMPLETE",
        source=RESEARCH_SOURCE,
        test_file=RESEARCH_TEST_FILE,
    ),
)


def checked_original(mutation: Mutation) -> bytes:
    original = mutation.source.read_bytes()
    count = original.decode("utf-8").count(mutation.anchor)
    if count != 1:
        raise ValueError(f"{mutation.id}: replacement anchor occurs {count} times, expected 1")
    return original


def run_test(nodeid: str) -> subprocess.CompletedProcess[str]:
    # A fresh prefix prevents reading old bytecode, including same-size mutants
    # written within one filesystem timestamp tick. Never touch the ready venv.
    with tempfile.TemporaryDirectory(prefix="demand-mutation-pyc-") as pycache:
        return subprocess.run(
            [
                sys.executable,
                "-X",
                f"pycache_prefix={pycache}",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--tb=line",
                nodeid,
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PY_COLORS": "0"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )


def require_green(mutation: Mutation) -> None:
    result = run_test(mutation.nodeid)
    if result.returncode != 0 or re.search(r"\b1 passed\b", result.stdout) is None:
        print(result.stdout, flush=True)
        raise ValueError(f"{mutation.id}: original target must pass, pytest rc={result.returncode}")
    print(f"PASS original {mutation.nodeid}", flush=True)


def failure_location(result: subprocess.CompletedProcess[str], mutation: Mutation) -> str:
    failed_nodes = re.findall(r"^FAILED (\S+)", result.stdout, flags=re.MULTILINE)
    if result.returncode != 1 or failed_nodes != [mutation.nodeid]:
        raise ValueError(
            f"{mutation.id}: expected one failed target, pytest rc={result.returncode}"
        )
    locations = re.findall(r"^(.+\.py):(\d+): .+$", result.stdout, flags=re.MULTILINE)
    if len(locations) != 1:
        raise ValueError(f"{mutation.id}: expected one failure location, got {locations}")
    filename, number = locations[0]
    path = (ROOT / filename).resolve()
    if path != ROOT / mutation.test_file:
        raise ValueError(f"{mutation.id}: failed outside the target test: {filename}:{number}")
    lines = path.read_text(encoding="utf-8").splitlines()
    line_number = int(number)
    if not 1 <= line_number <= len(lines):
        raise ValueError(f"{mutation.id}: invalid failure line {number}")
    line = lines[line_number - 1].strip()
    if line != mutation.assertion:
        raise ValueError(
            f"{mutation.id}: wrong assertion: {line!r}, expected {mutation.assertion!r}"
        )
    return f"{mutation.test_file}:{number} -> {line}"


def run_mutation(mutation: Mutation) -> None:
    original = checked_original(mutation)
    before = hashlib.sha256(original).hexdigest()
    require_green(mutation)
    # The baseline test must not have changed the source being tested.
    if mutation.source.read_bytes() != original:
        raise ValueError(f"{mutation.id}: source changed during baseline; file not mutated")
    try:
        mutation.source.write_bytes(
            original.decode("utf-8")
            .replace(mutation.anchor, mutation.replacement, 1)
            .encode("utf-8")
        )
        result = run_test(mutation.nodeid)
        try:
            location = failure_location(result, mutation)
        except ValueError:
            print(result.stdout, flush=True)
            raise
    finally:
        mutation.source.write_bytes(original)
        restored = mutation.source.read_bytes()
        after = hashlib.sha256(restored).hexdigest()
        if restored != original or after != before:
            raise ValueError(f"RESTORATION FAILED: sha256 before={before}, after={after}")
        print(f"{mutation.id} RESTORED sha256={after}", flush=True)
    print(f"{mutation.id} KILLED at {location}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--only", choices=[mutation.id for mutation in MUTATIONS])
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if args.only is None or m.id == args.only]
    killed: list[str] = []
    try:
        for mutation in selected:
            checked_original(mutation)
        for mutation in selected:
            if args.self_check:
                require_green(mutation)
            else:
                run_mutation(mutation)
                killed.append(mutation.id)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"GATE ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if not args.self_check:
            for mutation in selected:
                print(
                    f"{mutation.id}: {'KILLED' if mutation.id in killed else 'NOT KILLED'}",
                    flush=True,
                )
    print(
        f"{'SELF-CHECK' if args.self_check else 'MUTATION GATE'} PASSED: {len(selected)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
