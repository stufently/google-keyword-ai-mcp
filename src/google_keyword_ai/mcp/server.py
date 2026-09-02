from mcp.server.mcpserver import MCPServer

from google_keyword_ai import __version__
from google_keyword_ai.config import Settings, load_settings
from google_keyword_ai.envelope import Envelope
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.usecases.ads import (
    AdsData,
    run_ads_historical,
    run_competitor,
)
from google_keyword_ai.usecases.doctor import DoctorData, run_doctor
from google_keyword_ai.usecases.expand import ExpandData, run_expand
from google_keyword_ai.usecases.gsc import OpportunitiesData, run_gsc_opportunities
from google_keyword_ai.usecases.suggest import SuggestData, run_suggest
from google_keyword_ai.usecases.trends import TrendsData, run_trends_compare


def build_server(settings: Settings | None = None) -> MCPServer:
    active_settings = load_settings() if settings is None else settings
    server = MCPServer(name="google-keyword-ai", version=__version__)

    # Synchronous on purpose: the SDK offloads sync tool functions to a worker
    # thread (anyio.to_thread.run_sync), while an async one would run blocking
    # database and network work directly on the event loop and stall stdio.
    @server.tool()
    def doctor() -> Envelope[DoctorData]:
        return run_doctor(active_settings)

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    return server


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
