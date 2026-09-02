import functools
from collections.abc import Callable
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from google_keyword_ai import __version__
from google_keyword_ai.clustering import KeywordCluster
from google_keyword_ai.config import Settings, load_settings
from google_keyword_ai.envelope import Envelope
from google_keyword_ai.errors import GkaiError
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.pipeline.models import DryRunPlan, ResearchData
from google_keyword_ai.scoring import KeywordScore
from google_keyword_ai.usecases.ads import (
    AdsData,
    run_ads_historical,
    run_competitor,
)
from google_keyword_ai.usecases.analysis import (
    KeywordProvenance,
    NicheData,
    ScoredResearchData,
    run_cluster,
    run_explain_score,
    run_keyword_inspect,
    run_niche_analyze,
    run_score,
)
from google_keyword_ai.usecases.doctor import DoctorData, run_doctor
from google_keyword_ai.usecases.expand import ExpandData, run_expand
from google_keyword_ai.usecases.gsc import OpportunitiesData, run_gsc_opportunities
from google_keyword_ai.usecases.research import run_research
from google_keyword_ai.usecases.suggest import SuggestData, run_suggest
from google_keyword_ai.usecases.trends import TrendsData, run_trends_compare


def build_server(settings: Settings | None = None) -> MCPServer:
    active_settings = load_settings() if settings is None else settings
    server = MCPServer(name="google-keyword-ai", version=__version__)

    def tool() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool that reports a refused request instead of crashing.

        A `GkaiError` is a failure this code saw coming and described -- an
        unusable date range, a limit that is not positive. Letting it escape
        makes the SDK treat the call as a crash and withhold the text, so the
        caller is told only "Error executing tool <name>" and cannot tell a
        rejected argument from a broken server. `ToolError` is the SDK's own
        name for an anticipated failure, and its message reaches the caller.
        """

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(function)
            def guarded(*args: object, **kwargs: object) -> object:
                try:
                    return function(*args, **kwargs)
                except GkaiError as exc:
                    raise ToolError(exc.message) from exc

            return cast(Callable[..., Any], server.tool()(guarded))

        return decorate

    # Synchronous on purpose: the SDK offloads sync tool functions to a worker
    # thread (anyio.to_thread.run_sync), while an async one would run blocking
    # database and network work directly on the event loop and stall stdio.
    @tool()
    def doctor() -> Envelope[DoctorData]:
        return run_doctor(active_settings)

    @tool()
    def suggest_keywords(
        query: str,
        language: str | None = None,
        country: str | None = None,
        limit: int | None = None,
    ) -> Envelope[SuggestData]:
        return run_suggest(
            active_settings,
            query,
            language=language,
            country=country,
            limit=limit,
        )

    @tool()
    def expand_keywords(
        seed: str,
        language: str | None = None,
        country: str | None = None,
        depth: int | None = None,
        max_queries: int | None = None,
        max_results: int | None = None,
        max_runtime_seconds: float | None = None,
        strategies: list[str] | None = None,
        limit: int | None = None,
    ) -> Envelope[ExpandData]:
        return run_expand(
            active_settings,
            seed,
            language=language,
            country=country,
            depth=depth,
            max_queries=max_queries,
            max_results=max_results,
            max_runtime_seconds=max_runtime_seconds,
            strategies=strategies,
            limit=limit,
        )

    @tool()
    def analyze_trends(
        keywords: list[str],
        language: str | None = None,
        country: str | None = None,
        timeframe: str = "today 12-m",
    ) -> Envelope[TrendsData]:
        return run_trends_compare(
            active_settings,
            keywords,
            language=language,
            country=country,
            timeframe=timeframe,
        )

    @tool()
    def get_keyword_metrics(
        keywords: list[str],
        language: str | None = None,
        country: str | None = None,
    ) -> Envelope[AdsData]:
        return run_ads_historical(
            active_settings,
            keywords,
            language=language,
            country=country,
        )

    @tool()
    def analyze_competitor(
        target: str,
        seed_keyword: str | None = None,
        language: str | None = None,
        country: str | None = None,
        limit: int | None = None,
    ) -> Envelope[AdsData]:
        return run_competitor(
            active_settings,
            target,
            seed_keyword=seed_keyword,
            language=language,
            country=country,
            limit=limit,
        )

    @tool()
    def find_gsc_opportunities(
        site_url: str,
        days: int = 28,
        country: str | None = None,
        limit: int | None = None,
    ) -> Envelope[OpportunitiesData]:
        return run_gsc_opportunities(
            active_settings,
            site_url,
            days=days,
            country=country,
            limit=limit,
        )

    @tool()
    def research_keywords(
        target: str,
        scenario: str = "auto",
        language: str | None = None,
        country: str | None = None,
        seed_keyword: str | None = None,
        limit: int | None = None,
    ) -> Envelope[ResearchData]:
        envelope = run_research(
            active_settings,
            target,
            scenario=scenario,
            language=language,
            country=country,
            seed_keyword=seed_keyword,
            dry_run=False,
            limit=limit,
        )
        return cast(Envelope[ResearchData], envelope)

    # Planning is a separate tool on purpose. A single tool returning either
    # shape would need a union return type, and the SDK then nests the payload
    # under a "result" key while the CLI prints the envelope itself — the two
    # interfaces would stop matching, which is exactly what the parity test
    # exists to prevent.
    @tool()
    def plan_research(
        target: str,
        scenario: str = "auto",
        language: str | None = None,
        country: str | None = None,
        seed_keyword: str | None = None,
    ) -> Envelope[DryRunPlan]:
        envelope = run_research(
            active_settings,
            target,
            scenario=scenario,
            language=language,
            country=country,
            seed_keyword=seed_keyword,
            dry_run=True,
        )
        return cast(Envelope[DryRunPlan], envelope)

    @tool()
    def score_run(run_id: str, limit: int | None = None) -> Envelope[ScoredResearchData | None]:
        return run_score(active_settings, run_id, limit=limit)

    @tool()
    def cluster_run(run_id: str) -> Envelope[list[KeywordCluster] | None]:
        return run_cluster(active_settings, run_id)

    @tool()
    def explain_score(run_id: str, keyword: str) -> Envelope[KeywordScore | None]:
        return run_explain_score(active_settings, run_id, keyword)

    @tool()
    def analyze_niche(run_id: str) -> Envelope[NicheData | None]:
        return run_niche_analyze(active_settings, run_id)

    @tool()
    def inspect_keyword(run_id: str, keyword: str) -> Envelope[KeywordProvenance | None]:
        return run_keyword_inspect(active_settings, run_id, keyword)

    return server


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
