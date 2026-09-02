import ast
import re
from pathlib import Path

import pytest

from google_keyword_ai.cli.main import app as cli_app
from google_keyword_ai.config import Settings
from google_keyword_ai.mcp.server import build_server
from google_keyword_ai.pipeline.scenarios import ADS_CAVEAT, SITE_SEED_CAVEAT, TRENDS_CAVEAT

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".claude" / "skills" / "researching-google-keywords"
SKILL_PATH = SKILL_DIR / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines and lines[0] == "---"
    closing = lines.index("---", 1)
    result: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, raw_value = line.partition(":")
        assert separator and key and key.strip() == key
        assert key not in result
        raw_value = raw_value.strip()
        assert key in {"name", "description"}
        if key == "name":
            assert re.fullmatch(r"[a-z0-9-]+", raw_value)
            value = raw_value
        else:
            assert raw_value.startswith(('"', "'"))
            value = ast.literal_eval(raw_value)
        assert isinstance(value, str)
        result[key] = value
    assert set(result) == {"name", "description"}
    return result


def test_skill_frontmatter_and_length() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)

    assert frontmatter["name"] == "researching-google-keywords"
    assert frontmatter["description"].strip()
    assert len(text.splitlines()) < 200


@pytest.mark.parametrize("value", ["[broken", "{broken"])
def test_frontmatter_parser_rejects_invalid_yaml(value: str) -> None:
    text = f"---\nname: researching-google-keywords\ndescription: {value}\n---\n"

    with pytest.raises((AssertionError, SyntaxError, ValueError)):
        _frontmatter(text)


def test_skill_referenced_files_exist() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    references = set(re.findall(r"\]\(((?:reference|examples)/[^)]+)\)", text))

    assert references
    assert all((SKILL_DIR / relative_path).is_file() for relative_path in references)


def test_cli_reference_covers_every_top_level_command() -> None:
    text = (SKILL_DIR / "reference" / "cli.md").read_text(encoding="utf-8")
    names = {item.name for item in cli_app.registered_commands if item.name is not None}
    names.update(item.name for item in cli_app.registered_groups if item.name is not None)

    assert names
    assert all(re.search(rf"\bgkai {re.escape(name)}\b", text) for name in names)


def test_mcp_document_covers_every_tool() -> None:
    text = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    names = {tool.name for tool in build_server(Settings())._tool_manager.list_tools()}

    assert names
    assert all(re.search(rf"`{re.escape(name)}`", text) for name in names)


def test_readme_contains_google_disclaimer() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "This project is not affiliated with or endorsed by Google." in text


def test_skill_contains_standing_caveats() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert TRENDS_CAVEAT in text
    assert ADS_CAVEAT in text
    assert SITE_SEED_CAVEAT in text
