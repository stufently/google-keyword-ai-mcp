"""Reversible, sequential mutation checks for the M17 Google Ads tests.

Run inside the project's Docker environment; never run two gates concurrently.
Each pytest process selects one node so its failure location is unambiguous.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "src/google_keyword_ai/providers/google_ads.py"
TEST_FILE = "tests/test_google_ads_provider.py"


@dataclass(frozen=True)
class Failure:
    nodeid: str
    source_path: str
    expected_line: str


@dataclass(frozen=True)
class Mutation:
    id: str
    anchor: str
    replacement: str
    must_fail: tuple[Failure, ...]
    must_pass: tuple[str, ...] = ()
    path: str = PROVIDER
    expected_sha256: str = "626a5e94c0ce427df0f635fe413a3c46776c410425420af3c0fb17c8f1c1abb0"


MUTATIONS = (
    Mutation(
        id="M1",
        anchor='            "max_pages": str(self._settings.google_ads_max_pages),\n',
        replacement="",
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_a_raised_cap_is_not_served_the_smaller_caps_answer",
                TEST_FILE,
                "assert len(service.calls) == 2",
            ),
            Failure(
                f"{TEST_FILE}::test_a_pre_cap_cache_entry_is_missed_not_read",
                PROVIDER,
                'raise ApiError("Google Ads cache entry is invalid.") from exc',
            ),
        ),
    ),
    Mutation(
        id="M2",
        anchor="                if pages_read >= max_pages:",
        replacement="                if pages_read >= max_pages - 1:",
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_a_raised_cap_reads_the_pages_the_smaller_cap_skipped",
                TEST_FILE,
                "assert [idea.text for idea in pages[1].ideas] == [",
            ),
        ),
        must_pass=(f"{TEST_FILE}::test_a_raised_cap_is_not_served_the_smaller_caps_answer",),
    ),
    Mutation(
        id="M3",
        anchor=(
            "        self._store_ideas_page(\n"
            "            cache_key,\n"
            "            IDEAS_ENDPOINT,\n"
            "            customer_id,\n"
            "            page,\n"
            "            self._settings.google_ads_ideas_cache_ttl_seconds,\n"
            "        )\n"
        ),
        replacement=(
            "        if not page.truncated:\n"
            "            self._store_ideas_page(\n"
            "                cache_key,\n"
            "                IDEAS_ENDPOINT,\n"
            "                customer_id,\n"
            "                page,\n"
            "                self._settings.google_ads_ideas_cache_ttl_seconds,\n"
            "            )\n"
        ),
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_truncated_keyword_ideas_are_cached_under_their_cap",
                TEST_FILE,
                "assert len(service.calls) == 1",
            ),
        ),
        must_pass=(
            f"{TEST_FILE}::test_a_cached_truncated_page_is_still_truncated",
            f"{TEST_FILE}::test_a_cached_truncated_page_keeps_its_reason",
            f"{TEST_FILE}::test_complete_keyword_ideas_remain_cached",
        ),
    ),
    Mutation(
        id="M4",
        anchor="            payload=_IDEAS_PAGE_ADAPTER.dump_json(page, by_alias=False),",
        replacement=(
            "            payload=_IDEAS_PAGE_ADAPTER.dump_json("
            'page.model_copy(update={"truncated": False}), by_alias=False),'
        ),
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_a_cached_truncated_page_is_still_truncated",
                TEST_FILE,
                "assert page.truncated is True",
            ),
        ),
        must_pass=(
            f"{TEST_FILE}::test_truncated_keyword_ideas_are_cached_under_their_cap",
            f"{TEST_FILE}::test_a_cached_truncated_page_keeps_its_reason",
        ),
    ),
    Mutation(
        id="M5",
        anchor="            payload=_IDEAS_PAGE_ADAPTER.dump_json(page, by_alias=False),",
        replacement=(
            "            payload=_IDEAS_PAGE_ADAPTER.dump_json("
            'page.model_copy(update={"truncation_reason": None}), by_alias=False),'
        ),
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_a_cached_truncated_page_keeps_its_reason",
                TEST_FILE,
                "assert page.truncation_reason is not None",
            ),
        ),
        must_pass=(f"{TEST_FILE}::test_a_cached_truncated_page_is_still_truncated",),
    ),
    Mutation(
        id="M6",
        anchor=(
            '            "country": market.country,\n'
            "        }\n"
            "        cache_key = self._cache_key(HISTORICAL_ENDPOINT, params, customer_id)"
        ),
        replacement=(
            '            "country": market.country,\n'
            '            "max_pages": str(self._settings.google_ads_max_pages),\n'
            "        }\n"
            "        cache_key = self._cache_key(HISTORICAL_ENDPOINT, params, customer_id)"
        ),
        must_fail=(
            Failure(
                f"{TEST_FILE}::test_historical_metrics_ignore_the_page_cap",
                TEST_FILE,
                "assert len(service.calls) == 1",
            ),
        ),
        must_pass=(f"{TEST_FILE}::test_a_pre_cap_cache_entry_is_missed_not_read",),
    ),
)


def checked_original(mutation: Mutation) -> bytes:
    original = (ROOT / mutation.path).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != mutation.expected_sha256:
        raise ValueError(
            f"{mutation.id}: SHA-256 mismatch for {mutation.path}: "
            f"expected {mutation.expected_sha256}, got {digest}; file not touched"
        )
    count = original.decode("utf-8").count(mutation.anchor)
    if count != 1:
        raise ValueError(
            f"{mutation.id}: anchor occurs {count} times in {mutation.path}, "
            "expected exactly 1; file not touched"
        )
    return original


def run_test(nodeid: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", nodeid],
        cwd=ROOT,
        # Do not leave compiled mutants behind for subsequent processes.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PY_COLORS": "0"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def passed(result: subprocess.CompletedProcess[str]) -> bool:
    # rc=0 alone could also mean that a test was skipped or xfailed.
    return result.returncode == 0 and re.search(r"\b1 passed\b", result.stdout) is not None


def failure_location(result: subprocess.CompletedProcess[str], failure: Failure) -> str:
    failed_nodes = re.findall(r"^FAILED (\S+)", result.stdout, flags=re.MULTILINE)
    if result.returncode != 1 or failed_nodes != [failure.nodeid]:
        raise ValueError(f"{failure.nodeid}: expected FAILED, pytest rc={result.returncode}")
    locations = re.findall(r"^(.+\.py):(\d+): .+$", result.stdout, flags=re.MULTILINE)
    if len(locations) != 1:
        raise ValueError(f"{failure.nodeid}: expected one --tb=line location, got {locations}")
    filename, number = locations[0]
    path = Path(filename)
    # pytest may print an absolute container path or a path relative to cwd.
    relative = (ROOT / path).resolve().relative_to(ROOT).as_posix()
    if relative != failure.source_path:
        raise ValueError(f"{failure.nodeid}: unexpected failure file {relative}")
    lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
    line_number = int(number)
    if not 1 <= line_number <= len(lines):
        raise ValueError(f"{failure.nodeid}: invalid source line {number}")
    line = lines[line_number - 1].strip()
    if failure.expected_line not in line:
        raise ValueError(
            f"{failure.nodeid}: unexpected source line {relative}:{number} -> {line}; "
            f"expected substring {failure.expected_line!r}"
        )
    return f"{relative}:{number} -> {line}"


def run_mutation(mutation: Mutation) -> bool:
    original = checked_original(mutation)
    path = ROOT / mutation.path
    before = hashlib.sha256(original).hexdigest()
    evidence: list[str] = []
    try:
        mutated = original.decode("utf-8").replace(mutation.anchor, mutation.replacement, 1)
        path.write_bytes(mutated.encode("utf-8"))
        for failure in mutation.must_fail:
            result = run_test(failure.nodeid)
            try:
                location = failure_location(result, failure)
            except ValueError:
                print(result.stdout, flush=True)
                raise
            evidence.append(f"{mutation.id} KILLED by {failure.nodeid} at {location}")
        for nodeid in mutation.must_pass:
            result = run_test(nodeid)
            if not passed(result):
                print(result.stdout, flush=True)
                raise ValueError(f"{nodeid}: must remain green, pytest rc={result.returncode}")
    except (OSError, ValueError) as exc:
        print(f"{mutation.id} SURVIVED ({exc})", flush=True)
        return False
    finally:
        # Preserve the exact pre-run bytes, including any newline conventions.
        path.write_bytes(original)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        if after != before:
            raise ValueError(
                f"RESTORATION FAILED for {mutation.path}: SHA-256 before={before}, after={after}"
            )
        print(f"{mutation.id} RESTORED sha256={after}", flush=True)
    for line in evidence:
        print(line, flush=True)
    return True


def self_check() -> int:
    nodes = dict.fromkeys(
        nodeid
        for mutation in MUTATIONS
        for nodeid in (*[failure.nodeid for failure in mutation.must_fail], *mutation.must_pass)
    )
    for nodeid in nodes:
        result = run_test(nodeid)
        if not passed(result):
            print(result.stdout, flush=True)
            print(f"SELF-CHECK FAILED: {nodeid}, pytest rc={result.returncode}", flush=True)
            return 1
        print(f"PASS {nodeid}", flush=True)
    print(f"SELF-CHECK PASSED: {len(nodes)} tests", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--only", choices=[mutation.id for mutation in MUTATIONS])
    mode.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    selected = tuple(m for m in MUTATIONS if args.only is None or m.id == args.only)
    verdicts: dict[str, str] = {}
    try:
        # Validate every selected anchor before the first write, including in self-check.
        for mutation in selected:
            checked_original(mutation)
        if args.self_check:
            return self_check()
        for mutation in selected:
            killed = run_mutation(mutation)
            verdicts[mutation.id] = "KILLED" if killed else "SURVIVED"
            if not killed:
                break
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"GATE ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if not args.self_check:
            print("\nID  RESULT", flush=True)
            for mutation in selected:
                print(f"{mutation.id}  {verdicts.get(mutation.id, 'NOT RUN')}", flush=True)
    return 0 if all(verdicts.get(m.id) == "KILLED" for m in selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
