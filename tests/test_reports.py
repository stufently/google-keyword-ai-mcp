from datetime import UTC, datetime

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
