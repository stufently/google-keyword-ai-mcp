import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    google_ads_developer_token: SecretStr | None = None
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None
    google_ads_client_id: SecretStr | None = None
    google_ads_client_secret: SecretStr | None = None
    google_ads_refresh_token: SecretStr | None = None
    search_console_credentials_path: Path | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in _LOG_LEVELS:
            raise InvalidConfigurationError(
                f"Unknown log level: {value}. Expected debug, info, warning, or error."
            )
        return normalized


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
