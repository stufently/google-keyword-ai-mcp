"""Fifteen reversible demand mutations. Run sequentially, never alongside edits/tests.

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
