import ast
import re
from pathlib import Path
from typing import Any

import click
import pytest
from typer.main import get_command

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


def _cli_commands() -> tuple[set[str], set[tuple[str, str]]]:
    """Top-level command names and (group, subcommand) pairs, as Click resolves them.

    Ask the framework instead of re-deriving names: a command registered without
    an explicit name still gets one, so collecting ``item.name`` off Typer's
    ``registered_commands`` silently skips most of them.

    Groups are detected by ``list_commands``, not ``isinstance(click.Group)`` --
    ``TyperGroup`` does not subclass ``click.Group``, so an isinstance filter
    finds no subcommands at all. That is also why the root is typed ``Any``:
    the access here is structural, and claiming ``click.Group`` would be false.
    """
    root: Any = get_command(cli_app)
    context = click.Context(root)
    names = set(root.list_commands(context))

    pairs: set[tuple[str, str]] = set()
    for name in names:
        group = root.get_command(context, name)
        if group is None or not hasattr(group, "list_commands"):
            continue
        group_context = click.Context(group, parent=context)
        pairs.update((name, child) for child in group.list_commands(group_context))
    return names, pairs


def test_cli_reference_covers_every_top_level_command() -> None:
    text = (SKILL_DIR / "reference" / "cli.md").read_text(encoding="utf-8")
    names, _ = _cli_commands()

    assert names
    assert all(re.search(rf"\bgkai {re.escape(name)}\b", text) for name in names)


def test_cli_reference_covers_every_subcommand() -> None:
    text = (SKILL_DIR / "reference" / "cli.md").read_text(encoding="utf-8")
    _, pairs = _cli_commands()

    assert pairs
    assert all(
        re.search(rf"\bgkai {re.escape(group)} {re.escape(name)}\b", text) for group, name in pairs
    )


def _documented_tools() -> tuple[set[str], int]:
    """Tool names bulleted under '## Tools' in docs/mcp.md, plus the stated count."""
    text = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    section = re.search(r"^## Tools$(.*?)^## ", text, re.MULTILINE | re.DOTALL)
    assert section is not None
    stated = re.search(r"exposes exactly (\d+) tools", section.group(1))
    assert stated is not None
    return set(re.findall(r"^- `([a-z_]+)`$", section.group(1), re.MULTILINE)), int(stated.group(1))


def test_cli_reference_invents_no_commands() -> None:
    """The other direction: a renamed or deleted command left behind in the docs.

    Only the second token of a real *group* is checked. ``gkai trends compare``
    is legitimate even though ``compare`` is no Typer subcommand -- ``trends``
    takes it as a positional argument -- and ``trends`` is not a group, so the
    pair is never examined.
    """
    text = (SKILL_DIR / "reference" / "cli.md").read_text(encoding="utf-8")
    names, pairs = _cli_commands()
    groups = {group for group, _ in pairs}

    mentioned = re.findall(r"\bgkai ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", text)
    assert mentioned
    unknown = {f"gkai {first}" for first, _ in mentioned if first not in names} | {
        f"gkai {first} {second}"
        for first, second in mentioned
        if first in groups and second and (first, second) not in pairs
    }

    assert not unknown


def test_mcp_document_matches_registered_tools_exactly() -> None:
    """Both directions: an undocumented tool AND a tool documented but removed."""
    names = {tool.name for tool in build_server(Settings())._tool_manager.list_tools()}
    documented, stated_count = _documented_tools()

    assert names
    assert documented == names
    assert stated_count == len(names)


def test_readme_contains_google_disclaimer() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "This project is not affiliated with or endorsed by Google." in text


def test_skill_contains_standing_caveats() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert TRENDS_CAVEAT in text
    assert ADS_CAVEAT in text
    assert SITE_SEED_CAVEAT in text
