import anyio
import pytest

from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.ratelimit import AsyncRateLimiter


def test_acquire_waits_for_minimum_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)

    async def acquire_twice() -> None:
        limiter = AsyncRateLimiter(4.0)
        await limiter.acquire()
        await limiter.acquire()

    anyio.run(acquire_twice)

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.25, abs=0.01)


@pytest.mark.parametrize("rate", [0.0, -1.0])
def test_non_positive_rate_is_rejected(rate: float) -> None:
    with pytest.raises(InvalidConfigurationError, match="positive"):
        AsyncRateLimiter(rate)
