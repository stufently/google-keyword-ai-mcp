import math

from pydantic import BaseModel

from google_keyword_ai.config import Settings
from google_keyword_ai.pipeline.models import ResearchKeyword
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult


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
    trend_source: str | None = None,
    trend_gap: str | None = None,
) -> KeywordScore:
    """Score one keyword, naming where each component came from.

    `trend_growth` is a property of the run, not of this keyword: Trends is
    queried once per run, for a single series, and that one growth figure
    scores every keyword. `trend_source` names the series so the explanation
    cannot be read as this keyword's own trend.
    """
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
    series = (
        "the run's Trends series" if trend_source is None else f"Trends series {trend_source!r}"
    )
    trend_explanation = (
        f"Trend unavailable because {trend_gap or 'no growth figure was supplied'}."
        if trend_growth is None
        else (
            f"Trend growth {trend_growth:+.2%} from {series} gives {trend:.2f}/100 "
            "within one scope; that one series scores every keyword in the run, "
            "so this is not a per-keyword trend."
        )
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


def trend_series_keyword(trends: TrendsResult | None) -> str | None:
    """Name the keyword whose series the growth figure describes."""
    if trends is None or not trends.keywords:
        return None
    return trends.keywords[0]


def _is_measured(point: TrendPoint) -> bool:
    """Say whether Google reported real interest for this point.

    A week Google marks as having no data still arrives with a value of zero.
    Reading that zero as real interest turns "we do not know" into "interest
    collapsed". An absent `hasData` predates the field and is trusted.
    """
    return bool(point.values) and (not point.has_data or bool(point.has_data[0]))


def _whole_weeks(trends: TrendsResult) -> list[TrendPoint]:
    """Drop the unfinished week from the end of the series.

    Google returns the current week with only the days that have happened, and
    marks it `isPartial`. Averaged in alongside whole weeks it is a fragment
    counted as a full one, and it sits inside the latest quarter, which is the
    half of the comparison the answer is about: a week measured over two days of
    seven can move the growth figure across zero on its own. It stays in the
    payload -- Google really did return it -- and only the comparison ignores it.
    """
    timeline = list(trends.timeline)
    while timeline and timeline[-1].is_partial:
        timeline.pop()
    return timeline


def trend_growth_gap(trends: TrendsResult | None) -> str | None:
    """Name the reason there is no growth figure, or None if there is one.

    `compute_trend_growth` has three ways to answer "no", and both messages
    written for it covered only two: a timeline that is too short and a window
    with an unmeasured week. The third is a previous quarter that averaged
    zero -- eight fully measured points, two whole quarters to compare, and no
    baseline to divide by. Reported as a missing or unmeasured timeline, that
    sends the reader looking for data which is right there.
    """
    if trends is None:
        return "no Trends data was collected"
    timeline = _whole_weeks(trends)
    if len(timeline) < 8:
        return "the timeline is shorter than the eight finished weeks a comparison needs"
    quarter = len(timeline) // 4
    latest = timeline[-quarter:]
    previous = timeline[-2 * quarter : -quarter]
    if not all(_is_measured(point) for point in (*latest, *previous)):
        return "one of the two quarters being compared has a week Google could not measure"
    if sum(point.values[0] for point in previous) == 0:
        return "the previous quarter measured no interest at all, so growth has no baseline"
    return None


def compute_trend_growth(trends: TrendsResult | None) -> float | None:
    """Compare the mean of the last quarter of a timeline with the one before it.

    The two windows are cut from the timeline by POSITION, before any point is
    discarded, because a position in this series is a week on the calendar.
    Dropping the unmeasured weeks first would slide both windows backwards and
    silently answer a question about an older period: a year whose last two
    months Google could not measure would report the growth of the two quarters
    that preceded them as the recent trend.

    A window therefore has to be measured in full to stand for its quarter.
    Anything less is reported as no trend at all, which is the honest answer
    when the recent period is the part that is missing.
    """
    if trends is None:
        return None
    timeline = _whole_weeks(trends)
    if len(timeline) < 8:
        return None
    quarter = len(timeline) // 4
    if quarter == 0:
        return None
    latest = timeline[-quarter:]
    previous = timeline[-2 * quarter : -quarter]
    if not all(_is_measured(point) for point in (*latest, *previous)):
        return None
    previous_average = sum(point.values[0] for point in previous) / len(previous)
    latest_average = sum(point.values[0] for point in latest) / len(latest)
    if previous_average == 0:
        return None
    return (latest_average - previous_average) / previous_average
