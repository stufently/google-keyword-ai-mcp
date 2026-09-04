from collections.abc import Sequence

from google_keyword_ai.clustering import KeywordCluster, tokenize
from google_keyword_ai.pipeline.models import ResearchData
from google_keyword_ai.scoring import (
    KeywordScore,
    compute_trend_growth,
    trend_growth_gap,
    trend_series_keyword,
)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _list_or_none(values: Sequence[str], missing: str) -> list[str]:
    """Render a nested list under its heading bullet.

    The placeholder callers pass is already indented; the populated branch was
    not, so real metrics rendered as siblings of the headings above them while
    the empty case rendered as their child.
    """
    return [f"  - {_cell(value)}" for value in values] if values else [missing]


def _summary_line(data: ResearchData, clusters: Sequence[KeywordCluster]) -> str:
    """Count the clusters, and the remainder separately.

    The leftovers bucket is one of the objects `cluster_keywords` returns but it
    is not a cluster: the niche diversity factor already excludes it, so
    counting it here answered the same question two different ways in one run.
    """
    formed = [cluster for cluster in clusters if not cluster.is_remainder]
    line = f"Analyzed {len(data.keywords)} keywords in {len(formed)} clusters."
    leftover = sum(cluster.size for cluster in clusters if cluster.is_remainder)
    if leftover:
        line += f" {leftover} keywords joined no cluster."
    return line


def render_markdown(
    data: ResearchData,
    scores: Sequence[KeywordScore],
    clusters: Sequence[KeywordCluster],
) -> str:
    sources = data.data_quality.sources
    used = [source.name for source in sources if source.used]
    providers = ", ".join(used) if used else "none"
    lines = [
        "# Keyword research",
        "",
        f"- Seed/goal: {_cell(data.input)}",
        f"- Language: {_cell(data.language)}",
        f"- Country: {_cell(data.country)}",
        f"- Scenario: {_cell(data.scenario)}",
        f"- Providers: {providers}",
        "",
        "## Summary",
        "",
        _summary_line(data, clusters),
    ]

    # A keyword no component could be computed for scores 0.0 with confidence
    # "none". Averaging those in turns "we could not measure this" into "this is
    # worth nothing", which the scoring guide expressly refuses -- and it makes
    # the headline number FALL as the run discovers more keywords, with nothing
    # about the niche having changed.
    scored = [score for score in scores if score.components_available]
    if scored:
        average = sum(score.score for score in scored) / len(scored)
        lines.append(f"Average opportunity score: {average:.2f}/100 across {len(scored)} keywords.")
        unscored = len(scores) - len(scored)
        if unscored:
            lines.append(
                f"{unscored} of {len(scores)} keywords had no measurable component and are "
                "left out of that average rather than counted as zero."
            )
    elif scores:
        lines.append(
            f"None of the {len(scores)} keywords had a measurable component, so no average "
            "opportunity score can be given."
        )
    else:
        lines.append("No keyword scores are available because no keywords were returned.")

    lines.extend(["", "## Top opportunities", ""])
    ranked = sorted(scores, key=lambda score: (-score.score, score.keyword))[:10]
    if ranked:
        lines.extend(["| Keyword | Score | Confidence |", "|---|---:|---|"])
        lines.extend(
            f"| {_cell(score.keyword)} | {score.score:.2f} | {score.confidence} |"
            for score in ranked
        )
    else:
        lines.append("No opportunities are available because no keywords could be scored.")

    lines.extend(["", "## Keyword clusters", ""])
    if clusters:
        for cluster in clusters:
            members = ", ".join(_cell(keyword) for keyword in cluster.keywords)
            shared = ", ".join(cluster.shared_tokens) or "none"
            lines.append(
                f"- **{_cell(cluster.label)}** ({cluster.size}): {members}. "
                f"Shared tokens: {shared}."
            )
    else:
        lines.append("No clusters are available because no keywords were returned.")

    lines.extend(["", "## Trends", ""])
    growth = compute_trend_growth(data.trends)
    if data.trends is None:
        lines.append("No Trends data is available because the source was not used or unavailable.")
    elif growth is None:
        lines.append(f"Trend growth is unavailable: {trend_growth_gap(data.trends)}.")
    else:
        series = trend_series_keyword(data.trends)
        subject = "one series" if series is None else f"the series for `{_cell(series)}`"
        lines.append(
            f"Recent trend growth is {growth:+.2%} for {subject}, calculated within "
            f"normalization scope `{data.trends.normalization_scope}`. Trends is queried "
            "once per run, so this figure describes that series and not the keywords listed "
            "above."
        )

    lines.extend(["", "## Long-tail opportunities", ""])
    score_by_keyword = {score.keyword: score for score in scores}
    long_tail = [keyword for keyword in data.keywords if len(tokenize(keyword.keyword)) >= 3]
    if long_tail:
        lines.extend(["| Keyword | Score |", "|---|---:|"])
        for keyword in long_tail:
            score = score_by_keyword.get(keyword.keyword)
            rendered_score = "unavailable" if score is None else f"{score.score:.2f}"
            lines.append(f"| {_cell(keyword.keyword)} | {rendered_score} |")
    else:
        lines.append("No long-tail opportunities were found among keywords of three or more words.")

    lines.extend(["", "## Search Console opportunities", ""])
    if data.opportunities:
        for opportunity in data.opportunities:
            lines.append(
                f"- **{_cell(opportunity.query)}** — {opportunity.kind}: "
                f"{_cell(opportunity.reason)}"
            )
    else:
        lines.append(
            "No Search Console opportunities are available because none met the criteria "
            "or Search Console data was unavailable."
        )

    lines.extend(["", "## Data quality and limitations", ""])
    for source in sources:
        state = (
            "used"
            if source.used
            else ("available but not used" if source.available else "unavailable")
        )
        detail = _cell(source.detail)
        # Scenarios sometimes set the detail to the state word itself; repeating
        # it would render as "used — used".
        if detail.strip().casefold() == state.strip().casefold():
            lines.append(f"- Source `{source.name}`: {state}")
        else:
            lines.append(f"- Source `{source.name}`: {state} — {detail}")
    if not sources:
        lines.append("- No source availability metadata was recorded.")
    lines.append(f"- Retrieved at: {data.data_quality.retrieved_at.isoformat()}")
    lines.append("- Absolute metrics:")
    lines.extend(_list_or_none(data.data_quality.absolute_metrics, "  - None recorded."))
    lines.append("- Relative metrics:")
    lines.extend(_list_or_none(data.data_quality.relative_metrics, "  - None recorded."))
    lines.append("- Derived metrics:")
    lines.extend(_list_or_none(data.data_quality.derived_metrics, "  - None recorded."))
    lines.append("- Caveats:")
    lines.extend(_list_or_none(data.data_quality.caveats, "  - No caveats were recorded."))
    return "\n".join(lines) + "\n"
