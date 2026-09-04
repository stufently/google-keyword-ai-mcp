from datetime import UTC, datetime, timedelta

from google_keyword_ai.clustering import cluster_keywords
from google_keyword_ai.config import Settings
from google_keyword_ai.pipeline.budget import BudgetSpend
from google_keyword_ai.pipeline.models import (
    DataQuality,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.reports.markdown import render_markdown
from google_keyword_ai.scoring import score_keyword


def research(*, keywords: list[ResearchKeyword] | None = None) -> ResearchData:
    return ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[] if keywords is None else keywords,
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[
                SourceUsage(name="autocomplete", used=True, available=True, detail="used"),
                SourceUsage(name="google_ads", used=False, available=False, detail="no creds"),
            ],
            retrieved_at=datetime.now(UTC),
            absolute_metrics=["avg_monthly_searches"],
            relative_metrics=["trends_0_100"],
            derived_metrics=["opportunities"],
            caveats=["First caveat.", "Second caveat."],
        ),
    )


def test_report_has_all_eight_sections_in_order() -> None:
    rendered = render_markdown(research(), [], [])
    headings = [
        "# Keyword research",
        "## Summary",
        "## Top opportunities",
        "## Keyword clusters",
        "## Trends",
        "## Long-tail opportunities",
        "## Search Console opportunities",
        "## Data quality and limitations",
    ]
    positions = [rendered.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_empty_sections_explain_missing_data() -> None:
    rendered = render_markdown(research(), [], [])
    assert "No opportunities are available because" in rendered
    assert "No clusters are available because" in rendered
    assert "No Trends data is available because" in rendered


def test_data_quality_lists_sources_and_every_caveat() -> None:
    rendered = render_markdown(research(), [], [])
    assert "`autocomplete`: used" in rendered
    assert "`google_ads`: unavailable" in rendered
    assert "First caveat." in rendered
    assert "Second caveat." in rendered


def test_report_renders_scored_long_tail() -> None:
    item = ResearchKeyword(
        keyword="alpha keyword tool",
        normalized="alpha keyword tool",
        discovered_from=["autocomplete"],
        avg_monthly_searches=100,
    )
    data = research(keywords=[item])
    scores = [score_keyword(item, Settings())]
    clusters = cluster_keywords([item.keyword], Settings(cluster_min_size=1))
    assert "| alpha keyword tool |" in render_markdown(data, scores, clusters)


def test_source_line_does_not_repeat_the_state_word() -> None:
    """ "used — used" is noise, not information."""
    data = research()
    data.data_quality.sources = [
        SourceUsage(name="autocomplete", used=True, available=True, detail="used"),
        SourceUsage(name="trends", used=True, available=True, detail="53 timeline points"),
    ]

    report = render_markdown(data, [], [])

    assert "- Source `autocomplete`: used\n" in report
    assert "- Source `trends`: used — 53 timeline points" in report


def _trends(values: list[int], *, keyword: str = "alpha seed") -> TrendsResult:
    now = datetime.now(UTC)
    return TrendsResult(
        keywords=[keyword],
        geo="US",
        timeframe="today 12-m",
        normalization_scope="one-scope",
        timeline=[
            TrendPoint(
                timestamp=now + timedelta(days=index),
                formatted_time="",
                values=[value],
                has_data=[True],
            )
            for index, value in enumerate(values)
        ],
        retrieved_at=now,
        source="google_trends",
    )


def test_the_report_says_which_series_the_trend_figure_describes() -> None:
    """The report lists many keywords and one trend number, which invites the wrong reading.

    Trends is queried once per run, for a single series. Printing a bare
    percentage under a table of keywords reads as the trend of those keywords.
    """
    data = research().model_copy(update={"trends": _trends([10] * 4 + [20] * 4)})

    rendered = render_markdown(data, [], [])

    assert "alpha seed" in rendered
    assert "not the keywords listed above" in rendered


def test_an_unavailable_trend_does_not_blame_the_length_of_the_timeline() -> None:
    """Too few points is one reason among others, and naming only it misleads.

    A long timeline whose recent quarter Google could not measure also has no
    comparable trend, and telling the reader it was too short sends them
    looking for a longer window that would not help. There is a third way to
    have no figure -- a previous quarter that measured nothing, so there is no
    baseline to divide by -- and the message names whichever one applies.
    """
    blind = _trends([10] * 8)
    blind.timeline[-1].has_data = [False]
    data = research().model_copy(update={"trends": blind})

    rendered = render_markdown(data, [], [])

    assert "a week Google could not measure" in rendered
    assert "fewer than eight" not in rendered


def test_the_average_score_leaves_out_keywords_nothing_could_be_measured_for() -> None:
    """A keyword with no measurable component scores 0.0 and means nothing by it.

    Averaging those in turns "we could not measure this" into "this is worth
    nothing", which the scoring guide expressly refuses — and it makes the
    headline number fall as the run discovers more keywords, with nothing about
    the niche having changed.
    """
    measured = ResearchKeyword(
        keyword="alpha keyword tool",
        normalized="alpha keyword tool",
        discovered_from=["autocomplete"],
        avg_monthly_searches=1000,
    )
    unmeasured = ResearchKeyword(
        keyword="beta keyword tool",
        normalized="beta keyword tool",
        discovered_from=["autocomplete"],
    )
    settings = Settings()
    scores = [score_keyword(measured, settings), score_keyword(unmeasured, settings)]
    alone = score_keyword(measured, settings).score

    rendered = render_markdown(research(keywords=[measured, unmeasured]), scores, [])

    assert f"Average opportunity score: {alone:.2f}/100 across 1 keywords." in rendered
    assert "1 of 2 keywords had no measurable component" in rendered


def test_a_previous_quarter_of_zero_is_named_as_the_reason() -> None:
    """Eight measured points and two whole quarters, and still no growth figure.

    Dividing by a previous quarter that measured nothing has no answer, and
    reporting that as a short or unmeasured timeline sends the reader looking
    for data that is already there.
    """
    flat = _trends([0, 0, 0, 0, 0, 0, 5, 5])
    data = research().model_copy(update={"trends": flat})

    rendered = render_markdown(data, [], [])

    assert "no baseline" in rendered
    assert "could not measure" not in rendered


def test_metric_lists_nest_under_their_heading() -> None:
    """The placeholder was indented and the real entries were not.

    So a run that recorded metrics rendered them as siblings of `- Retrieved
    at:` and `- Source ...`, while a run that recorded none rendered the
    placeholder correctly as their child — the populated case looked broken and
    the empty one did not.
    """
    rendered = render_markdown(research(), [], [])

    assert "- Absolute metrics:\n  - avg_monthly_searches\n" in rendered
    assert "- Relative metrics:\n  - trends_0_100\n" in rendered
    assert "- Caveats:\n  - First caveat.\n" in rendered


def test_the_summary_counts_only_the_clusters_that_formed() -> None:
    """The leftovers bucket is a returned object, not a cluster.

    Counting it in the headline while the niche diversity factor excluded it put
    two different answers to the same question into one run's output.
    """
    keywords = [
        ResearchKeyword(keyword=text, normalized=text, discovered_from=["autocomplete"])
        for text in ("red shoe", "red shoes", "isolated")
    ]
    clusters = cluster_keywords(
        [keyword.keyword for keyword in keywords],
        Settings(cluster_similarity_threshold=0.3, cluster_min_size=2),
    )

    rendered = render_markdown(research(keywords=keywords), [], clusters)

    assert "Analyzed 3 keywords in 1 clusters." in rendered
    assert "1 keywords joined no cluster." in rendered
