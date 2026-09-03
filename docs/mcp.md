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

An empty answer is still an answer, and so is a refused one. A run that does
not exist, and a request the server refuses — a limit that is not positive, an
unusable date range, an unknown scenario — both come back as the ordinary
envelope with `data: null` and the reason in `completeness_reason`. It is the
same envelope, field for field, that `gkai ... --format json` prints for the
same input, so a caller parses one shape for every outcome.

Every tool therefore declares a nullable payload. The SDK validates a return
value against the declared type, so a tool whose payload cannot be `null` could
not report its own refusal: the answer would reach the caller as an opaque
`Error executing tool <name>` with the reason stripped out. Protocol errors are
left to the protocol — an unknown tool name, arguments that fail the input
schema — and never carry a result this project produced.

