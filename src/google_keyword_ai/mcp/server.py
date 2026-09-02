from mcp.server.mcpserver import MCPServer

from google_keyword_ai import __version__
from google_keyword_ai.config import Settings, load_settings
from google_keyword_ai.envelope import Envelope
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.usecases.doctor import DoctorData, run_doctor
from google_keyword_ai.usecases.suggest import SuggestData, run_suggest


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

    return server


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
