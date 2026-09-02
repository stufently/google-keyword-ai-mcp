import anyio

from google_keyword_ai.errors import InvalidConfigurationError


class AsyncRateLimiter:
    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise InvalidConfigurationError("rate_per_second must be positive.")
        self._minimum_interval = 1.0 / rate_per_second
        self._last_issued_at: float | None = None
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = anyio.current_time()
            if self._last_issued_at is not None:
                remaining = self._minimum_interval - (now - self._last_issued_at)
                if remaining > 0:
                    await anyio.sleep(remaining)
            self._last_issued_at = anyio.current_time()
