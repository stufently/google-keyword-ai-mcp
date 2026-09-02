from datetime import datetime

from pydantic import BaseModel, Field

from google_keyword_ai.opportunities import Opportunity
from google_keyword_ai.pipeline.budget import BudgetSpend
from google_keyword_ai.providers.expander import ExpansionStats
from google_keyword_ai.providers.trends.models import TrendsResult


class SourceUsage(BaseModel):
    name: str
    used: bool
    available: bool
    detail: str


class ResearchKeyword(BaseModel):
    keyword: str
    normalized: str
    discovered_from: list[str]
    autocomplete_relevance: int | None = None
    avg_monthly_searches: int | None = None
    ads_competition: str | None = None
    ads_competition_index: int | None = None
    low_top_of_page_bid: float | None = None
    high_top_of_page_bid: float | None = None
    gsc_impressions: int | None = None
    gsc_clicks: int | None = None
    gsc_ctr: float | None = None
    gsc_position: float | None = None


class ResearchStats(BaseModel):
    expansion: ExpansionStats | None = None
    spend: BudgetSpend
    stopped_by: str | None = None


class DataQuality(BaseModel):
    sources: list[SourceUsage]
    retrieved_at: datetime
    absolute_metrics: list[str]
    relative_metrics: list[str]
    derived_metrics: list[str]
    caveats: list[str]


class ResearchData(BaseModel):
    scenario: str
    input: str
    language: str
    country: str
    keywords: list[ResearchKeyword]
    trends: TrendsResult | None = None
    opportunities: list[Opportunity] = Field(default_factory=list)
    stats: ResearchStats
    data_quality: DataQuality


class DryRunPlan(BaseModel):
    scenario: str
    steps: list[str]
    estimated_autocomplete_queries: int
    estimated_ads_calls: int
    estimated_trends_calls: int
    sources: list[SourceUsage]
