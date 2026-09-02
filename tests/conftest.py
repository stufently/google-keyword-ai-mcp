from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from google_keyword_ai.config import Settings


@pytest.fixture(autouse=True)
def clear_gkai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(__import__("os").environ):
        if name.startswith("GKAI_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir)


@pytest.fixture
def thread_offload() -> None:
    """Skip when the event loop cannot be woken from a worker thread.

    MCP tool functions are deliberately synchronous, so the SDK runs them via
    ``anyio.to_thread.run_sync``. Waking the loop from that thread goes through
    asyncio's self-pipe, which some hardened sandboxes forbid alongside
    sockets; there the call never returns. Everywhere else this costs
    milliseconds and the test runs in full.
    """

    async def probe() -> None:
        with anyio.fail_after(5):
            await anyio.to_thread.run_sync(lambda: None)

    try:
        anyio.run(probe)
    except TimeoutError:  # pragma: no cover - only hit inside such a sandbox
        pytest.skip("event loop cannot be resumed from a worker thread in this environment")
    return None
