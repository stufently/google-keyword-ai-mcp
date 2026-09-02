import os
import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from google_keyword_ai import __version__
from google_keyword_ai.errors import InvalidConfigurationError

_APP_DIR = "google-keyword-ai-mcp"
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})


def _default_data_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "share"
    return base / _APP_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GKAI_", extra="ignore")

    data_dir: Path = Field(default_factory=_default_data_dir)
    log_level: str = "info"
    default_language: str = "en"
    default_country: str = "US"
    http_timeout_seconds: float = 10.0
    http_max_attempts: int = 3
    http_backoff_base_seconds: float = 0.5
    http_user_agent: str = (
        f"google-keyword-ai/{__version__} (+https://github.com/stufently/google-keyword-ai-mcp)"
    )
    autocomplete_rate_limit_per_second: float = 5.0
    autocomplete_cache_ttl_seconds: int = 86400
    trends_enabled: bool = True
    trends_pacing_seconds: float = 0.8
    trends_cache_ttl_seconds: int = 21600
    trends_circuit_breaker_failures: int = 3
    trends_timezone_minutes: int = -180
    cache_enabled: bool = True
    google_ads_developer_token: SecretStr | None = None
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None
    google_ads_client_id: SecretStr | None = None
    google_ads_client_secret: SecretStr | None = None
    google_ads_refresh_token: SecretStr | None = None
    google_ads_api_version: str = "v25"
    google_ads_rate_limit_per_second: float = 1.0
    google_ads_ideas_cache_ttl_seconds: int = 604800
    google_ads_historical_cache_ttl_seconds: int = 2592000
    google_ads_page_size: int = 1000
    search_console_credentials_path: Path | None = None
    search_console_row_limit: int = 25000
    search_console_daily_row_cap: int = 50000
    search_console_cache_ttl_seconds: int = 21600
    search_console_rate_limit_per_second: float = 5.0
    gsc_opportunity_min_impressions: int = 100
    gsc_opportunity_min_position: float = 5.0
    gsc_opportunity_max_position: float = 30.0
    gsc_opportunity_max_ctr: float = 0.02

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in _LOG_LEVELS:
            raise InvalidConfigurationError(
                f"Unknown log level: {value}. Expected debug, info, warning, or error."
            )
        return normalized

    @field_validator("http_max_attempts")
    @classmethod
    def validate_http_max_attempts(cls, value: int) -> int:
        if value < 1:
            raise InvalidConfigurationError("http_max_attempts must be at least 1.")
        return value

    @field_validator(
        "http_timeout_seconds",
        "autocomplete_rate_limit_per_second",
        "trends_pacing_seconds",
        "google_ads_rate_limit_per_second",
        "search_console_rate_limit_per_second",
        "gsc_opportunity_min_position",
        "gsc_opportunity_max_position",
    )
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise InvalidConfigurationError("HTTP timing and rate-limit values must be positive.")
        return value

    @field_validator("autocomplete_cache_ttl_seconds")
    @classmethod
    def validate_positive_ttl(cls, value: int) -> int:
        if value <= 0:
            raise InvalidConfigurationError("autocomplete_cache_ttl_seconds must be positive.")
        return value

    @field_validator("trends_cache_ttl_seconds")
    @classmethod
    def validate_positive_trends_ttl(cls, value: int) -> int:
        if value <= 0:
            raise InvalidConfigurationError("trends_cache_ttl_seconds must be positive.")
        return value

    @field_validator(
        "google_ads_ideas_cache_ttl_seconds",
        "google_ads_historical_cache_ttl_seconds",
        "google_ads_page_size",
    )
    @classmethod
    def validate_positive_google_ads_integer(cls, value: int) -> int:
        if value <= 0:
            raise InvalidConfigurationError("Google Ads limits and cache TTLs must be positive.")
        return value

    @field_validator("trends_circuit_breaker_failures")
    @classmethod
    def validate_trends_circuit_breaker_failures(cls, value: int) -> int:
        if value < 1:
            raise InvalidConfigurationError("trends_circuit_breaker_failures must be at least 1.")
        return value

    @field_validator("search_console_row_limit")
    @classmethod
    def validate_search_console_row_limit(cls, value: int) -> int:
        if not 1 <= value <= 25000:
            raise InvalidConfigurationError("search_console_row_limit must be between 1 and 25000.")
        return value

    @field_validator(
        "search_console_daily_row_cap",
        "search_console_cache_ttl_seconds",
        "gsc_opportunity_min_impressions",
    )
    @classmethod
    def validate_positive_search_console_integer(cls, value: int) -> int:
        if value <= 0:
            raise InvalidConfigurationError(
                "Search Console limits and thresholds must be positive."
            )
        return value

    @field_validator("gsc_opportunity_max_ctr")
    @classmethod
    def validate_gsc_opportunity_max_ctr(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise InvalidConfigurationError(
                "gsc_opportunity_max_ctr must be above 0 and at most 1."
            )
        return value

    @model_validator(mode="after")
    def validate_gsc_opportunity_position_window(self) -> Self:
        if self.gsc_opportunity_min_position >= self.gsc_opportunity_max_position:
            raise InvalidConfigurationError(
                "gsc_opportunity_min_position must be below gsc_opportunity_max_position."
            )
        return self


def _read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as config_file:
            loaded = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InvalidConfigurationError(f"Unable to read configuration file {path}: {exc}") from exc
    return dict(loaded)


def _user_config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return base / _APP_DIR / "config.toml"


def _environment_values() -> dict[str, object]:
    values: dict[str, object] = {}
    for field_name in Settings.model_fields:
        environment_name = f"GKAI_{field_name.upper()}"
        if environment_name in os.environ:
            values[field_name] = os.environ[environment_name]
    return values


def load_settings(cwd: Path | None = None) -> Settings:
    working_directory = Path.cwd() if cwd is None else cwd
    values = _read_toml(_user_config_path())
    values.update(_read_toml(working_directory / ".gkai.toml"))
    values.update(_environment_values())
    return Settings.model_validate(values)


def masked_dump(settings: Settings) -> dict[str, object]:
    dumped: dict[str, Any] = settings.model_dump(mode="json")
    for field_name in settings.__class__.model_fields:
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            dumped[field_name] = "***" if value.get_secret_value() else None
    return dumped
