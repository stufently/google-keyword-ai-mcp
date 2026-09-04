from collections.abc import Sequence
from datetime import datetime
from typing import cast

from pydantic import BaseModel

from google_keyword_ai.clustering import KeywordCluster, cluster_keywords, tokenize
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.normalize import normalize_keyword
from google_keyword_ai.pipeline.models import ResearchData, ResearchKeyword
from google_keyword_ai.pipeline.runs import RunStore
from google_keyword_ai.scoring import (
    KeywordScore,
    compute_trend_growth,
    score_keyword,
    trend_growth_gap,
    trend_series_keyword,
)
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases.limits import require_positive_limit


class ScoredResearchData(BaseModel):
    research: ResearchData
    scores: list[KeywordScore]
    clusters: list[KeywordCluster]


class NicheFactor(BaseModel):
    name: str
    value: float | None
    available: bool
    explanation: str


class NicheData(BaseModel):
    seed: str
    opportunity_score: float
    factors: list[NicheFactor]
    keywords_analyzed: int
    clusters: int
    caveats: list[str]


class MetricProvenance(BaseModel):
    metric: str
    value: int | float | str | None
    source: str
    retrieved_at: datetime | None
    language: str
    country: str
    is_derived: bool


class KeywordProvenance(BaseModel):
    keyword: str
    normalized: str
    discovered_from: list[str]
    metrics: list[MetricProvenance]


def _empty[T](reason: str, *, run_id: str | None = None) -> Envelope[T | None]:
    """Answer with an empty envelope, which is an answer and not a failure.

    A run that does not exist, or holds no saved result, is an ordinary outcome
    with a reason worth reading. The return types say `| None` so this can
    travel over MCP too: the SDK validates a tool's output against its declared
    type, and a type that cannot express `data: null` turns this answer into an
    opaque tool error.
    """
    return cast(
        Envelope[T | None],
        Envelope(
            data=None,
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
            run_id=run_id,
        ),
    )


def _load_research(settings: Settings, run_id: str) -> tuple[ResearchData | None, str | None]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return None, f"Run {run_id} was not found."
    if record.result is None:
        return None, f"Run {run_id} has no saved research result."
    payload = record.result.get("data")
    if payload is None:
        return None, f"Run {run_id} has no research data in its saved result."
    try:
        return ResearchData.model_validate(payload), None
    except ValueError:
        return None, f"Run {run_id} does not contain a valid research result."


def _scores(data: ResearchData, settings: Settings) -> list[KeywordScore]:
    growth = compute_trend_growth(data.trends)
    source = trend_series_keyword(data.trends)
    gap = trend_growth_gap(data.trends)
    return [
        score_keyword(keyword, settings, trend_growth=growth, trend_source=source, trend_gap=gap)
        for keyword in data.keywords
    ]


def run_score(
    settings: Settings,
    run_id: str,
    *,
    limit: int | None = None,
) -> Envelope[ScoredResearchData | None]:
    require_positive_limit(limit, "Score")
    research, reason = _load_research(settings, run_id)
    if research is None:
        return _empty(reason or "Saved research data is unavailable.", run_id=run_id)
    selected = (
        research
        if limit is None
        else research.model_copy(update={"keywords": research.keywords[:limit]})
    )
    scores = _scores(selected, settings)
    clusters = cluster_keywords([keyword.keyword for keyword in selected.keywords], settings)
    return Envelope(
        data=ScoredResearchData(research=selected, scores=scores, clusters=clusters),
        run_id=run_id,
    )


def _find_keyword(data: ResearchData, keyword: str) -> ResearchKeyword | None:
    normalized = normalize_keyword(keyword)
    return next((item for item in data.keywords if item.normalized == normalized), None)


def run_explain_score(
    settings: Settings, run_id: str, keyword: str
) -> Envelope[KeywordScore | None]:
    research, reason = _load_research(settings, run_id)
    if research is None:
        return _empty(reason or "Saved research data is unavailable.", run_id=run_id)
    found = _find_keyword(research, keyword)
    if found is None:
        return _empty(f"Keyword {keyword!r} was not found in run {run_id}.", run_id=run_id)
    return Envelope(
        data=score_keyword(
            found,
            settings,
            trend_growth=compute_trend_growth(research.trends),
            trend_source=trend_series_keyword(research.trends),
            trend_gap=trend_growth_gap(research.trends),
        ),
        run_id=run_id,
    )


def run_cluster(settings: Settings, run_id: str) -> Envelope[list[KeywordCluster] | None]:
    research, reason = _load_research(settings, run_id)
    if research is None:
        return _empty(reason or "Saved research data is unavailable.", run_id=run_id)
    return Envelope(
        data=cluster_keywords([keyword.keyword for keyword in research.keywords], settings),
        run_id=run_id,
    )


def _factor(name: str, value: float | None, explanation: str) -> NicheFactor:
    return NicheFactor(name=name, value=value, available=value is not None, explanation=explanation)


def _commercial_value(keywords: Sequence[ResearchKeyword], settings: Settings) -> float | None:
    bids = [
        keyword.high_top_of_page_bid
        if keyword.high_top_of_page_bid is not None
        else keyword.low_top_of_page_bid
        for keyword in keywords
    ]
    available = [bid for bid in bids if bid is not None]
    if not available:
        return None
    return sum(100.0 * min(bid / settings.score_bid_reference, 1.0) for bid in available) / len(
        available
    )


def _niche_factors(
    research: ResearchData, settings: Settings, clusters: Sequence[KeywordCluster]
) -> list[NicheFactor]:
    keywords = research.keywords
    volumes = [
        keyword.avg_monthly_searches
        for keyword in keywords
        if keyword.avg_monthly_searches is not None
    ]
    total_demand = sum(volumes) if volumes else None
    demand_value = (
        None
        if total_demand is None
        else min(100.0 * total_demand / settings.score_demand_reference, 100.0)
    )
    significant = (
        None if not volumes else 100.0 * min(sum(volume >= 100 for volume in volumes) / 20.0, 1.0)
    )
    long_tail = (
        None
        if not keywords
        else 100.0
        * sum(len(tokenize(keyword.keyword)) >= 3 for keyword in keywords)
        / len(keywords)
    )
    growth = compute_trend_growth(research.trends)
    trend = None if growth is None else 50.0 + 50.0 * max(-1.0, min(growth, 1.0))
    commercial = _commercial_value(keywords, settings)
    positive_total = sum(volumes)
    concentration = (
        None
        if positive_total <= 0
        else 100.0 * sum(sorted(volumes, reverse=True)[:5]) / positive_total
    )
    regular_clusters = [cluster for cluster in clusters if cluster.label != "unclustered"]
    diversity = None if not keywords else 100.0 * min(len(regular_clusters) / 10.0, 1.0)
    gsc = [keyword for keyword in keywords if keyword.gsc_impressions is not None]
    site_coverage = (
        None
        if not gsc
        else 100.0 * sum((keyword.gsc_impressions or 0) > 0 for keyword in gsc) / len(gsc)
    )
    rendered_demand = total_demand if total_demand is not None else "unavailable"
    return [
        _factor(
            "total_demand",
            demand_value,
            f"Measured total demand: {rendered_demand}.",
        ),
        _factor(
            "significant_keywords",
            significant,
            "Share of a 20-keyword reference count with at least 100 monthly searches.",
        ),
        _factor(
            "long_tail_depth",
            long_tail,
            "Percentage of analyzed keywords containing at least three words.",
        ),
        _factor(
            "trend_direction",
            trend,
            "Recent versus previous quarter growth within one Trends normalization scope.",
        ),
        _factor(
            "commercial_value",
            commercial,
            "Average normalized top-of-page bid among keywords with bid data.",
        ),
        _factor(
            "query_concentration",
            concentration,
            "Share of measured demand held by the five largest keywords.",
        ),
        _factor(
            "cluster_diversity",
            diversity,
            "Distinct regular clusters relative to a ten-cluster reference.",
        ),
        _factor(
            "site_coverage",
            site_coverage,
            "Share of GSC-measured keywords already receiving impressions.",
        ),
    ]


def run_niche_analyze(settings: Settings, run_id: str) -> Envelope[NicheData | None]:
    research, reason = _load_research(settings, run_id)
    if research is None:
        return _empty(reason or "Saved research data is unavailable.", run_id=run_id)
    clusters = cluster_keywords([keyword.keyword for keyword in research.keywords], settings)
    factors = _niche_factors(research, settings, clusters)
    available = [
        factor.value for factor in factors if factor.available and factor.value is not None
    ]
    score = sum(available) / len(available) if available else 0.0
    return Envelope(
        data=NicheData(
            seed=research.input,
            opportunity_score=score,
            factors=factors,
            keywords_analyzed=len(research.keywords),
            clusters=len(clusters),
            caveats=research.data_quality.caveats,
        ),
        run_id=run_id,
    )


_METRIC_SOURCES = {
    "autocomplete_relevance": "autocomplete",
    "avg_monthly_searches": "google_ads",
    "ads_competition": "google_ads",
    "ads_competition_index": "google_ads",
    "low_top_of_page_bid": "google_ads",
    "high_top_of_page_bid": "google_ads",
    "gsc_impressions": "search_console",
    "gsc_clicks": "search_console",
    "gsc_ctr": "search_console",
    "gsc_position": "search_console",
}


def run_keyword_inspect(
    settings: Settings, run_id: str, keyword: str
) -> Envelope[KeywordProvenance | None]:
    research, reason = _load_research(settings, run_id)
    if research is None:
        return _empty(reason or "Saved research data is unavailable.", run_id=run_id)
    found = _find_keyword(research, keyword)
    if found is None:
        return _empty(f"Keyword {keyword!r} was not found in run {run_id}.", run_id=run_id)
    metrics = []
    for metric, source in _METRIC_SOURCES.items():
        value = getattr(found, metric)
        if value is not None:
            metrics.append(
                MetricProvenance(
                    metric=metric,
                    value=value,
                    source=source,
                    retrieved_at=research.data_quality.retrieved_at,
                    language=research.language,
                    country=research.country,
                    is_derived=metric in research.data_quality.derived_metrics,
                )
            )
    return Envelope(
        data=KeywordProvenance(
            keyword=found.keyword,
            normalized=found.normalized,
            discovered_from=found.discovered_from,
            metrics=metrics,
        ),
        run_id=run_id,
    )
