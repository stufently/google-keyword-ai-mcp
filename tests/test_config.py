from pathlib import Path

import pytest
from pydantic import SecretStr

from google_keyword_ai.config import Settings, load_settings, masked_dump
from google_keyword_ai.errors import InvalidConfigurationError


def test_precedence_environment_over_project_over_user_over_defaults(
    tmp_path: Path, monkeypatch: object
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".gkai.toml").write_text(
        'default_language = "de"\ndefault_country = "DE"\nlog_level = "warning"\n',
        encoding="utf-8",
    )
    config_home = tmp_path / "config"
    user_dir = config_home / "google-keyword-ai-mcp"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text(
        'default_language = "fr"\ndefault_country = "FR"\nlog_level = "error"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("GKAI_DEFAULT_LANGUAGE", "ru")

    settings = load_settings(project_dir)

    assert settings.default_language == "ru"
    assert settings.default_country == "DE"
    assert settings.log_level == "warning"


def test_user_config_overrides_defaults(tmp_path: Path, monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    config_home = tmp_path / "config"
    user_dir = config_home / "google-keyword-ai-mcp"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text('default_country = "TH"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    settings = load_settings(tmp_path)

    assert settings.default_language == "en"
    assert settings.default_country == "TH"


def test_default_data_dir_uses_xdg_data_home(tmp_path: Path, monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    settings = load_settings(tmp_path)

    assert settings.data_dir == tmp_path / "xdg-data" / "google-keyword-ai-mcp"


def test_masked_dump_never_reveals_secrets(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        google_ads_developer_token=SecretStr("developer-secret"),
        google_ads_client_id=SecretStr("client-secret"),
        google_ads_client_secret=SecretStr(""),
        google_ads_refresh_token=None,
    )

    result = masked_dump(settings)

    assert result["google_ads_developer_token"] == "***"
    assert result["google_ads_client_id"] == "***"
    assert result["google_ads_client_secret"] is None
    assert result["google_ads_refresh_token"] is None
    assert "developer-secret" not in repr(result)
    assert "client-secret" not in repr(result)


def test_trends_settings_defaults() -> None:
    settings = Settings()

    assert settings.trends_enabled is True
    assert settings.trends_pacing_seconds == 0.8
    assert settings.trends_cache_ttl_seconds == 21600
    assert settings.trends_circuit_breaker_failures == 3
    assert settings.trends_timezone_minutes == -180


def test_cache_sweep_default() -> None:
    assert Settings().cache_sweep_enabled is True


def test_cache_size_limit_default() -> None:
    assert Settings().cache_max_bytes == 536870912


def test_cache_size_limit_rejects_negative_values() -> None:
    with pytest.raises(InvalidConfigurationError, match="cache_max_bytes"):
        Settings(cache_max_bytes=-1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trends_pacing_seconds", 0),
        ("trends_cache_ttl_seconds", 0),
        ("trends_circuit_breaker_failures", 0),
    ],
)
def test_trends_settings_reject_invalid_limits(field: str, value: int) -> None:
    with pytest.raises(InvalidConfigurationError):
        Settings.model_validate({field: value})


def test_google_ads_settings_defaults() -> None:
    settings = Settings()

    assert settings.google_ads_api_version == "v25"
    assert settings.google_ads_rate_limit_per_second == 1.0
    assert settings.google_ads_ideas_cache_ttl_seconds == 604800
    assert settings.google_ads_historical_cache_ttl_seconds == 2592000
    assert settings.google_ads_page_size == 1000
    assert settings.google_ads_max_pages == 20


def test_google_ads_max_pages_defaults_to_twenty() -> None:
    assert Settings().google_ads_max_pages == 20


def test_google_ads_max_pages_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GKAI_GOOGLE_ADS_MAX_PAGES", "3")
    assert Settings().google_ads_max_pages == 3


@pytest.mark.parametrize("value", [0, -1])
def test_google_ads_max_pages_rejects_zero_and_negative(value: int) -> None:
    with pytest.raises(InvalidConfigurationError):
        Settings(google_ads_max_pages=value)


@pytest.mark.parametrize(
    "field",
    [
        "google_ads_rate_limit_per_second",
        "google_ads_ideas_cache_ttl_seconds",
        "google_ads_historical_cache_ttl_seconds",
        "google_ads_page_size",
        "google_ads_max_pages",
    ],
)
def test_google_ads_settings_reject_non_positive_values(field: str) -> None:
    with pytest.raises(InvalidConfigurationError):
        Settings.model_validate({field: 0})


def test_quota_project_id_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID", "  test-quota-project \t")

    assert Settings().search_console_quota_project_id == "test-quota-project"
    settings = load_settings(tmp_path)
    assert settings.search_console_quota_project_id == "test-quota-project"
    assert masked_dump(settings)["search_console_quota_project_id"] == "test-quota-project"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_quota_project_is_refused(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvalidConfigurationError, match="quota_project_id must not be blank"):
        Settings(search_console_quota_project_id=value)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID", value)
    with pytest.raises(InvalidConfigurationError, match="quota_project_id must not be blank"):
        load_settings(tmp_path)


def test_quota_project_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID", raising=False)
    assert Settings().search_console_quota_project_id is None
    assert Settings(search_console_quota_project_id=None).search_console_quota_project_id is None
