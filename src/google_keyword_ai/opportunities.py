from collections.abc import Sequence

from pydantic import BaseModel

from google_keyword_ai.config import Settings
from google_keyword_ai.providers.search_console import SearchAnalyticsRow


class Opportunity(BaseModel):
    query: str
    page: str | None
    clicks: int
    impressions: int
    ctr: float
    position: float
    kind: str
    reason: str


def find_opportunities(rows: Sequence[SearchAnalyticsRow], settings: Settings) -> list[Opportunity]:
    midpoint = (settings.gsc_opportunity_min_position + settings.gsc_opportunity_max_position) / 2
    opportunities: list[Opportunity] = []
    for row in rows:
        if row.impressions < settings.gsc_opportunity_min_impressions:
            continue
        if not (
            settings.gsc_opportunity_min_position
            <= row.position
            <= settings.gsc_opportunity_max_position
        ):
            continue

        quick_win = row.position <= midpoint and row.ctr <= settings.gsc_opportunity_max_ctr
        kind = "quick_win" if quick_win else "content_expansion"
        if quick_win:
            reason = (
                f"{row.impressions} impressions at position {row.position:.2f} with "
                f"CTR {row.ctr:.2%}, at or below the configured "
                f"{settings.gsc_opportunity_max_ctr:.2%} "
                "CTR ceiling in the first half of the position window."
            )
        else:
            reason = (
                f"{row.impressions} impressions at position {row.position:.2f} with "
                f"CTR {row.ctr:.2%} inside the configured position window "
                f"{settings.gsc_opportunity_min_position:.2f}-"
                f"{settings.gsc_opportunity_max_position:.2f}."
            )
        opportunities.append(
            Opportunity(
                query=row.keys.get("query", ""),
                page=row.keys.get("page"),
                clicks=row.clicks,
                impressions=row.impressions,
                ctr=row.ctr,
                position=row.position,
                kind=kind,
                reason=reason,
            )
        )
    return sorted(opportunities, key=lambda opportunity: opportunity.impressions, reverse=True)
