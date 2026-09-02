# Google Keyword Intelligence

Google Keyword Intelligence is an open-source CLI, MCP server and agent skill
for collecting, enriching and analyzing Google search-demand data from Google
Ads Keyword Planner, Autocomplete, Trends and Search Console.

It expands long-tail ideas, researches niches and competitor-derived themes,
finds first-party Search Console opportunities, budgets provider calls, saves and
resumes runs, scores keywords, creates lexical clusters and renders Markdown
reports. CLI and MCP return the same versioned response envelope.

## Quick start

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). CI checks both
the 3.12 compatibility floor and Python 3.14.

```bash
git clone https://github.com/stufently/google-keyword-ai-mcp.git
cd google-keyword-ai-mcp
uv sync
uv run gkai doctor --format json
uv run gkai research "running shoes" --language en --country US --dry-run
```

Review the dry-run sources and call estimates before making the real request:

```bash
uv run gkai research "running shoes" --language en --country US --save-run
```

Autocomplete and unofficial Trends work without account credentials. Results may
still be `partial` when optional providers are unavailable; always read
`completeness`, `completeness_reason`, `warnings` and `data_quality`.

## Installation

The core install includes CLI, MCP, Autocomplete and Trends:

```bash
uv sync
```

Install optional official-provider clients as needed:

```bash
uv sync --extra ads
uv sync --extra gsc
# or both
uv sync --all-extras
```

Configuration is loaded from the user config, project `.gkai.toml`, then
`GKAI_...` environment variables. Later sources override earlier ones. Inspect the
effective, secret-masked result with `uv run gkai config show`.

Google Ads requires your own developer token, customer ID and OAuth client values:

```bash
export GKAI_GOOGLE_ADS_DEVELOPER_TOKEN="..."
export GKAI_GOOGLE_ADS_CUSTOMER_ID="..."
export GKAI_GOOGLE_ADS_CLIENT_ID="..."
export GKAI_GOOGLE_ADS_CLIENT_SECRET="..."
export GKAI_GOOGLE_ADS_REFRESH_TOKEN="..."
```

Search Console requires your own service-account or authorized-user credential
file with access to the property:

```bash
export GKAI_SEARCH_CONSOLE_CREDENTIALS_PATH="/absolute/path/credentials.json"
```

See [Google Ads](docs/google-ads.md), [Search Console](docs/search-console.md) and
[privacy](docs/privacy.md) for provider and local-storage details.

## Check availability with `gkai doctor`

```bash
uv run gkai doctor --format json
```

`doctor` reports the database and exactly which of Autocomplete, Google Ads,
Trends and Search Console are ready. Use this output to choose a workflow; a
missing optional provider is explained in an `empty` or `partial` envelope rather
than silently replaced.

## Basic research

Preview a topic workflow cheaply, then save the approved run:

```bash
uv run gkai research "аренда квартиры в Паттайе" --language ru --country TH --dry-run
uv run gkai research "аренда квартиры в Паттайе" --language ru --country TH --save-run
```

Use the returned run ID for analysis without repeating collection:

```bash
uv run gkai score <run_id>
uv run gkai cluster <run_id>
uv run gkai niche analyze <run_id>
uv run gkai run export <run_id>
```

The pipeline, budgets and source order are described in
[docs/pipeline.md](docs/pipeline.md); saved-run behavior is in
[docs/runs.md](docs/runs.md).

## Competitor research

Automatic research recognizes a domain or URL. Preview first:

```bash
uv run gkai research competitor.com --language en --country US --dry-run
uv run gkai research competitor.com --language en --country US --save-run
```

For direct Google Ads site-seed ideas:

```bash
uv run gkai competitor competitor.com --language en --country US
```

A site seed returns ideas Google associates with that site. It does **not** reveal
queries for which the competitor ranks, organic positions or traffic.

## Search Console workflow

```bash
uv run gkai gsc properties
uv run gkai gsc opportunities "sc-domain:example.com" --days 28 --limit 50
uv run gkai gsc queries "sc-domain:example.com" --days 28 --dimension query
```

These figures are real observations for the connected property and selected date
range, not whole-market estimates. Opportunity values are derived by `gkai` from
impressions, CTR and average position.

## MCP server and Claude Code

Start the stdio server with:

```bash
uv run google-keyword-ai
```

Add a project `.mcp.json` for Claude Code:

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

Run Claude Code from the repository so `uv` resolves this project. Environment
variables and credential paths are inherited by the server. See the exact tool
list and parity contract in [docs/mcp.md](docs/mcp.md).

## Claude Skill

The project skill lives at
`.claude/skills/researching-google-keywords/SKILL.md`. Claude Code discovers the
project-local skill when opened in this repository. It always starts with
`gkai doctor`, selects niche, competitor or existing-site workflow from intent,
requires a dry run before expensive research, and preserves source caveats.

## Limitations

- Autocomplete and Trends use unofficial, undocumented Google endpoints with no
  availability or compatibility guarantee. An official Trends API exists but is in
  closed alpha with application-gated access, so this project does not use it;
  `OfficialTrendsAdapter` only reserves the seam.
- Google Ads and Search Console are optional and require your own credentials and
  access. There is no interactive OAuth flow; only credential files/values are
  consumed.
- Google rounds search volumes and combines close keyword variants. Google Ads
  competition measures advertiser demand, not SEO difficulty.
- Trends values are relative 0–100 interest within one response, not search volume.
- Search Console BigQuery export is not implemented.
- Clustering is deterministic and lexical; it does not use embeddings, SERPs or an
  LLM, and may group semantic synonyms poorly.

This project is not affiliated with or endorsed by Google.
Google and related product names are trademarks of their respective owners.
