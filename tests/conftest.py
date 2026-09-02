from pathlib import Path

import pytest

from google_keyword_ai.config import Settings


@pytest.fixture(autouse=True)
def clear_gkai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(__import__("os").environ):
        if name.startswith("GKAI_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir)
