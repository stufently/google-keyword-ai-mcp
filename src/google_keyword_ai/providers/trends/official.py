from collections.abc import Sequence

from google_keyword_ai.errors import ProviderUnavailableError
from google_keyword_ai.providers.trends.models import TrendsResult


class OfficialTrendsAdapter:
    unavailable_reason = "The official Google Trends API is in closed alpha and is not available."

    def is_available(self) -> bool:
        return False

    async def fetch(
        self,
        keywords: Sequence[str],
        *,
        geo: str,
        timeframe: str,
        hl: str,
    ) -> TrendsResult:
        del keywords, geo, timeframe, hl
        raise ProviderUnavailableError(self.unavailable_reason)
