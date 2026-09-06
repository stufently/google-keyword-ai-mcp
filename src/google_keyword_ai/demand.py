"""Pure anchor-based comparison of already parsed Google Trends batches."""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from pydantic import BaseModel

from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.normalize import normalize_keyword
from google_keyword_ai.providers.trends.models import TrendsResult

MAX_DEMAND_KEYWORDS = 50
KEYWORDS_PER_BATCH = 4


class DemandRow(BaseModel):
    keyword: str
    relative_demand: float | None
    is_anchor: bool
    batch: int
    measured_weeks: int
    weeks: int
    reason: str | None


@dataclass(frozen=True)
class DemandBatch:
    """Planned keys (anchor first), with either a result or a verbatim error.

    The result's keyword order can differ from the plan. All batches passed to
    combine must share an anchor, market and timeframe. Repeated participants
    are retained, allowing callers to inspect overlap between independent batches.
    """

    keywords: list[str]
    result: TrendsResult | None = None
    reason: str | None = None


def plan_batches(keywords: Sequence[str], anchor: str) -> list[list[str]]:
    unique = list(dict.fromkeys(key for raw in keywords if (key := normalize_keyword(raw))))
    if not 2 <= len(unique) <= MAX_DEMAND_KEYWORDS:
        raise InvalidConfigurationError(
            f"Demand requires between 2 and {MAX_DEMAND_KEYWORDS} unique nonempty keywords."
        )
    anchor = normalize_keyword(anchor)
    if anchor not in unique:
        raise InvalidConfigurationError("The explicit demand anchor must be present in the set.")
    participants = [keyword for keyword in unique if keyword != anchor]
    return [
        [anchor, *participants[start : start + KEYWORDS_PER_BATCH]]
        for start in range(0, len(participants), KEYWORDS_PER_BATCH)
    ]


def _measured_values(result: TrendsResult, keyword: str) -> list[int]:
    if keyword not in result.keywords:
        return []
    index = result.keywords.index(keyword)
    return [
        point.values[index]
        for point in result.timeline
        if not point.is_partial
        and index < len(point.values)
        and index < len(point.has_data)
        and point.has_data[index]
    ]


def measured_mean(result: TrendsResult, keyword: str) -> float | None:
    """Mean of measured complete weeks for this name, never of missing data."""
    values = _measured_values(result, keyword)
    return fmean(values) if values else None


def _batch_rows(batch: DemandBatch, number: int) -> list[DemandRow]:
    anchor = batch.keywords[0]
    result = batch.result
    anchor_mean = measured_mean(result, anchor) if result is not None else None
    weeks = sum(not point.is_partial for point in result.timeline) if result is not None else 0
    rows: list[DemandRow] = []
    for keyword in batch.keywords:
        relative: float | None = None
        reason: str | None = None
        measured = len(_measured_values(result, keyword)) if result is not None else 0
        if result is None:
            reason = batch.reason
            if not reason:
                raise ValueError("A missing demand batch requires the provider's failure reason.")
        elif anchor_mean is None or anchor_mean == 0:
            reason = (
                f"Anchor '{anchor}' collapsed: no measured whole weeks or zero mean; "
                "this batch cannot be placed on the common scale."
            )
        elif keyword == anchor:
            relative = 100.0
        else:
            mean = measured_mean(result, keyword)
            if mean is None:
                reason = (
                    f"Google returned no measured whole weeks for '{keyword}' beside anchor "
                    f"'{anchor}': below the anchor's resolution, not zero demand."
                )
            else:
                relative = mean / anchor_mean * 100
        rows.append(
            DemandRow(
                keyword=keyword,
                relative_demand=relative,
                is_anchor=keyword == anchor,
                batch=number,
                measured_weeks=measured,
                weeks=weeks,
                reason=reason,
            )
        )
    return rows


def combine(batches: Sequence[DemandBatch]) -> list[DemandRow]:
    """Scale each batch against its own anchor mean and stably rank its rows.

    Emit the anchor once, using the first usable batch's coverage. If every
    anchor collapsed or failed, keep the first batch's null anchor and reason.
    """
    rows: list[DemandRow] = []
    anchor_row: DemandRow | None = None
    for number, batch in enumerate(batches, start=1):
        for row in _batch_rows(batch, number):
            if row.is_anchor:
                if anchor_row is None:
                    anchor_row = row
                    rows.append(row)
                elif anchor_row.relative_demand is None and row.relative_demand is not None:
                    rows[0] = row
                    anchor_row = row
            else:
                rows.append(row)
    return sorted(rows, key=lambda row: (row.relative_demand is None, -(row.relative_demand or 0)))
