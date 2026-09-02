import os
import platform

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai import __version__
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import GkaiError
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
    google_ads_detail = (
        "not implemented yet (M4)"
        if settings.google_ads_developer_token is not None
        else "missing credentials"
    )
    search_console_detail = (
        "not implemented yet (M5)"
        if settings.search_console_credentials_path is not None
        else "missing credentials"
    )
    return [
        ProviderStatus(name="autocomplete", available=True, detail="ready"),
        ProviderStatus(name="trends", available=True, detail="not implemented yet (M2/M3)"),
        ProviderStatus(name="google_ads", available=False, detail=google_ads_detail),
        ProviderStatus(name="search_console", available=False, detail=search_console_detail),
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
