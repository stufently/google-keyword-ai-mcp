import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.usecases.doctor import run_doctor


def run_cli(
    tmp_path: Path, *arguments: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path / "cli-data")
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_doctor_json_envelope(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "doctor", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert set(payload) >= {
        "schema_version",
        "data",
        "warnings",
        "errors",
        "completeness",
    }
    assert payload["schema_version"] == "1.0.0"
    assert payload["completeness"] == "complete"
    assert len(payload["data"]["providers"]) == 4
    assert result.stdout.count("\n") == 1


def test_stdout_clean_with_debug_logs(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "doctor",
        "--format",
        "json",
        extra_env={"GKAI_LOG_LEVEL": "debug"},
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["data"]["database"] == "ok"
    assert result.stderr
    assert '"level": "debug"' in result.stderr


def test_config_show_masks_secrets(tmp_path: Path) -> None:
    token = "token-that-must-never-leak"
    result = run_cli(
        tmp_path,
        "config",
        "show",
        "--format",
        "json",
        extra_env={"GKAI_GOOGLE_ADS_DEVELOPER_TOKEN": token},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["google_ads_developer_token"] == "***"
    assert token not in result.stdout
    assert token not in result.stderr


def test_doctor_does_not_raise_when_database_cannot_open(tmp_path: Path) -> None:
    file_instead_of_directory = tmp_path / "not-a-directory"
    file_instead_of_directory.write_text("occupied", encoding="utf-8")

    envelope = run_doctor(Settings(data_dir=file_instead_of_directory))

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason
    assert envelope.data.database != "ok"
    assert envelope.data.schema_version == 0


def test_cli_returns_one_for_partial_result(tmp_path: Path) -> None:
    file_instead_of_directory = tmp_path / "not-a-directory"
    file_instead_of_directory.write_text("occupied", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "doctor",
        "--format",
        "json",
        extra_env={"GKAI_DATA_DIR": str(file_instead_of_directory)},
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["completeness"] == "partial"


def test_reconfigure_logging_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    configure_logging("debug")
    capsys.readouterr()

    structlog.get_logger("test").debug("one_event")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert json.loads(captured.err)["event"] == "one_event"
