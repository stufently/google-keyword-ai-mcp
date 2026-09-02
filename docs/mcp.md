# MCP server

Start the stdio server from the repository:

```bash
uv run google-keyword-ai
```

Installing the wheel puts the same console script, `google-keyword-ai`, on `PATH`.

## Tools

The server exposes exactly 14 tools:

- `doctor`
- `suggest_keywords`
- `expand_keywords`
- `analyze_trends`
- `get_keyword_metrics`
- `analyze_competitor`
- `find_gsc_opportunities`
- `research_keywords`
- `plan_research`
- `score_run`
- `cluster_run`
- `explain_score`
- `analyze_niche`
- `inspect_keyword`

Verify the registered list without starting a transport:

```bash
uv run --all-extras python -c "from google_keyword_ai.config import Settings; from google_keyword_ai.mcp.server import build_server; print(sorted(t.name for t in build_server(Settings())._tool_manager.list_tools()))"
```

## Claude Code

Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "google-keyword-ai": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "google-keyword-ai"]
    }
  }
}
```

Start Claude Code in that directory. Pass `GKAI_...` settings through the parent
environment, and use absolute paths for credential files. Tool structured output
is the same versioned envelope printed by the corresponding
`gkai ... --format json` command. Planning is a separate tool so MCP never needs a
union return type and remains shape-compatible with CLI JSON.

An empty answer is still an answer. A run that does not exist, or holds no
saved result, comes back as a normal envelope with `data: null` and a reason in
`completeness_reason` — which is why the tools that read a saved run declare a
nullable payload. The SDK validates a tool's return value against its declared
type, so a type that cannot express `data: null` would turn that answer into an
opaque failure.

A request the server refuses — a limit that is not positive, an unusable date
range, an unknown scenario — is a tool error rather than an envelope, carrying
the same message the CLI puts in `completeness_reason`. That is the protocol's
way to say the arguments were wrong, and it keeps the reason visible instead of
reporting only which tool failed.

