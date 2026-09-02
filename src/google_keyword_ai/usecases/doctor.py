import os
import platform

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai import __version__
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import GkaiError
from google_keyword_ai.providers.google_ads import GoogleAdsProvider
from google_keyword_ai.providers.search_console import SearchConsoleProvider
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.storage.migrations import SCHEMA_VERSION


class ProviderStatus(BaseModel):
    name: str
    available: bool
    detail: str


class DoctorData(BaseModel):
    version: str
    python_version: str
    data_dir: str
    database: str
    schema_version: int
    providers: list[ProviderStatus]


def _provider_statuses(settings: Settings) -> list[ProviderStatus]:
    google_ads_provider = GoogleAdsProvider(
        settings=settings,
        cache=None,
        rate_limiter=None,
    )
    google_ads_available = google_ads_provider.is_available()
    search_console_provider = SearchConsoleProvider(
        settings=settings,
        cache=None,
        rate_limiter=None,
    )
    search_console_available = search_console_provider.is_available()
    credentials_path = settings.search_console_credentials_path
    if search_console_available:
        search_console_detail = "ready"
    elif credentials_path is None:
        search_console_detail = "missing credentials"
    else:
        search_console_detail = f"credentials file not found: {credentials_path}"
    trends_available = GoogleTrendsProvider(settings=settings).is_available()
    return [
        ProviderStatus(name="autocomplete", available=True, detail="ready"),
        ProviderStatus(
            name="trends",
            available=trends_available,
            detail="ready (unofficial)" if trends_available else "disabled by configuration",
        ),
        ProviderStatus(
            name="google_ads",
            available=google_ads_available,
            detail="ready" if google_ads_available else "missing credentials",
        ),
        ProviderStatus(
            name="search_console",
            available=search_console_available,
            detail=search_console_detail,
        ),
    ]


def run_doctor(settings: Settings) -> Envelope[DoctorData]:
    database_status = "ok"
    schema_version = SCHEMA_VERSION
    failure_reason: str | None = None

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(settings.data_dir, os.W_OK):
            raise OSError(f"Data directory is not writable: {settings.data_dir}")
        engine = open_database(settings)
        engine.dispose()
    except (GkaiError, OSError, SQLAlchemyError) as exc:
        failure_reason = str(exc)
        database_status = failure_reason
        schema_version = 0

    data = DoctorData(
        version=__version__,
        python_version=platform.python_version(),
        data_dir=str(settings.data_dir),
        database=database_status,
        schema_version=schema_version,
        providers=_provider_statuses(settings),
    )
    if failure_reason is not None:
        return Envelope(
            data=data,
            errors=[failure_reason],
            completeness=Completeness.PARTIAL,
            completeness_reason=failure_reason,
        )
    return Envelope(data=data)


def run_config_show(settings: Settings) -> Envelope[dict[str, object]]:
    return Envelope(data=masked_dump(settings))
