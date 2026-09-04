import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    """One bucket of the interest timeline.

    `is_partial` marks a week that has not finished yet -- Google returns the
    current week with the days so far, and the golden capture shows exactly one
    such point at the end. Its value is a fragment, not a week, so it must not
    be averaged alongside whole ones; it is kept in the payload because Google
    really did return it.
    """

    timestamp: datetime
    formatted_time: str
    values: list[int]
    has_data: list[bool] = Field(default_factory=list)
    is_partial: bool = False


class GeoInterest(BaseModel):
    geo_code: str
    geo_name: str
    values: list[int]
    has_data: list[bool] = Field(default_factory=list)


class RelatedQuery(BaseModel):
    query: str
    value: int
    formatted_value: str
    has_data: bool | None = None


class RelatedQueries(BaseModel):
    top: list[RelatedQuery] = Field(default_factory=list)
    rising: list[RelatedQuery] = Field(default_factory=list)


class TrendsResult(BaseModel):
    keywords: list[str]
    geo: str
    timeframe: str
    normalization_scope: str
    timeline: list[TrendPoint] = Field(default_factory=list)
    geo_interest: list[GeoInterest] = Field(default_factory=list)
    related: RelatedQueries = Field(default_factory=RelatedQueries)
    retrieved_at: datetime
    source: str

    def carries_data(self) -> bool:
        """Say whether the reply holds any of the three things Trends returns.

        A result exists whatever happened: a request whose widgets all failed
        comes back as one of these, empty. Callers that tested the object for
        existence counted that outage as data, so the question belongs here,
        asked once, rather than being re-derived at each layer -- and the
        timeline alone cannot answer it either, because Google returns regions
        and related queries for keywords whose timeline is empty.
        """
        return bool(self.timeline or self.geo_interest or self.related.top or self.related.rising)


def build_normalization_scope(keywords: list[str], *, geo: str, timeframe: str, hl: str) -> str:
    canonical = json.dumps(
        {
            "keywords": keywords,
            "geo": geo,
            "timeframe": timeframe,
            "hl": hl,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
