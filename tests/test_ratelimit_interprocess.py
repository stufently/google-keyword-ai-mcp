import fcntl
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.ratelimit import InterProcessRateLimiter


async def _working_thread_runner[T](function: Callable[..., T], *args: object) -> T:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(function, *args).result()


def test_two_instances_share_the_same_issue_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anyio.to_thread, "run_sync", _working_thread_runner)
    lock_path = tmp_path / "data" / "google-ads-customer.lock"
    first = InterProcessRateLimiter(10.0, lock_path)
    second = InterProcessRateLimiter(10.0, lock_path)

    async def acquire_twice() -> float:
        started = time.monotonic()
        await first.acquire()
        await second.acquire()
        return time.monotonic() - started

    elapsed = anyio.run(acquire_twice)

    assert elapsed >= 0.09
    assert lock_path.is_file()
    assert lock_path.parent == tmp_path / "data"


@pytest.mark.parametrize("rate", [0.0, -1.0])
def test_non_positive_rate_is_invalid(rate: float, tmp_path: Path) -> None:
    with pytest.raises(InvalidConfigurationError):
        InterProcessRateLimiter(rate, tmp_path / "rate.lock")


_WORKER = """
import sys, time, anyio
from pathlib import Path
from google_keyword_ai.ratelimit import InterProcessRateLimiter

limiter = InterProcessRateLimiter(float(sys.argv[2]), Path(sys.argv[1]))
anyio.run(limiter.acquire)
print(repr(time.time()))
"""


def test_acquire_waits_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    """Mutual exclusion is the whole point, and one process cannot prove it.

    Acquiring twice inside a single process passes even with no file lock at
    all: the second call just reads what the first one wrote. Launching two
    processes is not enough either, because interpreter start-up jitter usually
    separates them by more than the interval on its own. So hold the lock here
    and check that a real process genuinely blocks on it.
    """
    lock_path = tmp_path / "data" / "customer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # A very high rate: any waiting observed can only come from the lock.
        worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(lock_path), "1000"],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                worker.wait(timeout=2.0)

            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            stdout, _ = worker.communicate(timeout=60)
            assert worker.returncode == 0
            assert float(stdout.strip()) > 0
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=30)
    finally:
        holder.close()
