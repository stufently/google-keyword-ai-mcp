import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    timestamp: datetime
    formatted_time: str
    values: list[int]
    has_data: list[bool] = Field(default_factory=list)


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
