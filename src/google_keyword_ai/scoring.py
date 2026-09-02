import math

from pydantic import BaseModel

from google_keyword_ai.config import Settings
from google_keyword_ai.pipeline.models import ResearchKeyword
from google_keyword_ai.providers.trends.models import TrendsResult


class ScoreComponent(BaseModel):
    name: str
    available: bool
    raw: float | None
    normalized: float | None
    weight: float
    contribution: float
    explanation: str


class KeywordScore(BaseModel):
    keyword: str
    score: float
    components: list[ScoreComponent]
    components_available: int
    components_total: int
    confidence: str


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _component(
    name: str,
    raw: float | None,
    normalized: float | None,
    weight: float,
    explanation: str,
) -> ScoreComponent:
    available = normalized is not None
    return ScoreComponent(
        name=name,
        available=available,
        raw=raw,
        normalized=normalized,
        weight=weight,
        contribution=0.0 if normalized is None else normalized * weight,
        explanation=explanation,
    )


def score_keyword(
    keyword: ResearchKeyword,
    settings: Settings,
    *,
    trend_growth: float | None = None,
) -> KeywordScore:
    volume = keyword.avg_monthly_searches
    demand = (
        None
        if volume is None
        else min(
            100.0 * math.log10(volume + 1) / math.log10(settings.score_demand_reference + 1),
            100.0,
        )
    )
    demand_explanation = (
        "Demand unavailable because Google Ads search volume is missing."
        if volume is None
        else f"Search volume {volume:,} gives logarithmic demand {demand:.2f}/100."
    )

    trend = None if trend_growth is None else 50.0 + 50.0 * _clamp(trend_growth, -1.0, 1.0)
    trend_explanation = (
        "Trend unavailable because a comparable eight-point timeline is missing."
        if trend_growth is None
        else f"Trend growth {trend_growth:+.2%} gives {trend:.2f}/100 within one scope."
    )

    bid = (
        keyword.high_top_of_page_bid
        if keyword.high_top_of_page_bid is not None
        else keyword.low_top_of_page_bid
    )
    commercial = None if bid is None else 100.0 * min(bid / settings.score_bid_reference, 1.0)
    commercial_explanation = (
        "Commercial value unavailable because Google Ads bid data is missing."
        if bid is None
        else f"Top-of-page bid {bid:.2f} gives commercial value {commercial:.2f}/100."
    )

    impressions = keyword.gsc_impressions
    position = keyword.gsc_position
    opportunity = (
        None
        if impressions is None or position is None
        else 100.0 * min(impressions / 1000.0, 1.0) * _clamp((position - 1.0) / 29.0, 0.0, 1.0)
    )
    opportunity_explanation = (
        "Opportunity unavailable because Search Console impressions or position are missing."
        if opportunity is None
        else (
            f"{impressions:,} impressions at position {position:.2f} give "
            f"opportunity {opportunity:.2f}/100."
        )
    )

    components = [
        _component("demand", volume, demand, settings.score_weight_demand, demand_explanation),
        _component("trend", trend_growth, trend, settings.score_weight_trend, trend_explanation),
        _component(
            "commercial", bid, commercial, settings.score_weight_commercial, commercial_explanation
        ),
        _component(
            "opportunity",
            None if opportunity is None else float(impressions or 0),
            opportunity,
            settings.score_weight_opportunity,
            opportunity_explanation,
        ),
    ]
    available = [component for component in components if component.available]
    available_weight = sum(component.weight for component in available)
    score = (
        0.0
        if not available or available_weight == 0
        else sum(component.contribution for component in available) / available_weight
    )
    count = len(available)
    confidence = {0: "none", 1: "low", 2: "low", 3: "medium", 4: "high"}[count]
    return KeywordScore(
        keyword=keyword.keyword,
        score=score,
        components=components,
        components_available=count,
        components_total=len(components),
        confidence=confidence,
    )


def compute_trend_growth(trends: TrendsResult | None) -> float | None:
    if trends is None or len(trends.timeline) < 8:
        return None
    values = [point.values[0] for point in trends.timeline if point.values]
    if len(values) < 8:
        return None
    quarter = len(values) // 4
    if quarter == 0:
        return None
    latest = values[-quarter:]
    previous = values[-2 * quarter : -quarter]
    previous_average = sum(previous) / len(previous)
    latest_average = sum(latest) / len(latest)
    if previous_average == 0:
        return None
    return (latest_average - previous_average) / previous_average
