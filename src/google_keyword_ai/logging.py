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
    structlog.get_logger(__name__).debug("logging_configured", level=normalized_level)
