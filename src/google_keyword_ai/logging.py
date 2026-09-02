import logging
import sys

import structlog

from google_keyword_ai.errors import InvalidConfigurationError

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(level: str) -> None:
    normalized_level = level.lower()
    if normalized_level not in _LEVELS:
        raise InvalidConfigurationError(
            f"Unknown log level: {level}. Expected debug, info, warning, or error."
        )

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[normalized_level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    # Third-party libraries log through the standard library, not structlog, and
    # something else configures a handler for them if we do not: the MCP stdio
    # server showed httpx INFO lines with full request URLs in a foreign format.
    # force=True drops any handler installed before us so everything lands on
    # stderr, keeping stdout free for protocol traffic and JSON output.
    logging.basicConfig(
        stream=sys.stderr,
        level=_LEVELS[normalized_level],
        format="%(message)s",
        force=True,
    )
    for noisy_logger in ("httpx", "httpcore"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    structlog.get_logger(__name__).debug("logging_configured", level=normalized_level)
