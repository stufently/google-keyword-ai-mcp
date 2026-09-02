# Architecture

Google Keyword Intelligence has three layers around one set of domain models.

## Layers

1. **Core:** settings, market validation, envelopes, providers, cache, rate limits,
   storage, research scenarios, saved-run execution, scoring, clustering and
   reports under `src/google_keyword_ai/`.
2. **Thin wrappers:** Typer exposes the core as `gkai`; MCP exposes the same
   use cases and envelopes over stdio. Neither wrapper owns research logic.
3. **Agent skill:** `.claude/skills/researching-google-keywords/` selects workflows,
   orders cheap and expensive work, and preserves interpretation caveats.

Provider I/O is asynchronous so HTTP pacing, concurrent expansion and blocking
vendor clients can yield without freezing orchestration. Synchronous Google client
calls are moved to worker threads. Public use cases give both wrappers a simple
synchronous boundary; MCP tool functions are deliberately synchronous because the
SDK offloads them from its stdio event loop.

## Response contract and MCP

Every use case returns `Envelope[T]`: `schema_version`, typed `data`, `warnings`,
`errors`, `completeness`, optional reason and optional run ID. CLI JSON and MCP
structured output serialize this same envelope.

MCP tools must have one concrete result shape. Planning and execution are separate
`plan_research` and `research_keywords` tools because a union return type makes the
SDK wrap data under an extra `result` key and breaks CLI/MCP parity.

## Providers

All providers expose `Provider.info` (`name`, official status, stability) and
`is_available()`. Typed capabilities add provider-specific operations:

- Autocomplete: suggestions; unofficial and undocumented.
- Expander: bounded fan-out over Autocomplete strategies.
- Trends: normalized timelines, regions and related searches; unofficial fallback.
- Google Ads: official keyword ideas and historical planning metrics.
- Search Console: official properties and Search Analytics rows.

Failures cross the boundary as project errors and become honest `partial` or
`empty` envelopes. Details: [Autocomplete](autocomplete.md),
[expansion](expansion.md), [Trends](trends.md), [Google Ads](google-ads.md) and
[Search Console](search-console.md).

## Cache, throttling and storage

SQLite cache keys include provider, endpoint, canonical parameters, account scope
and parser version. Per-provider TTLs limit staleness. Async rate limiters pace
single-process calls; Google Ads also uses a file lock and timestamp for a shared
interprocess limit.

SQLite uses WAL, a busy timeout, foreign keys and numbered forward migrations.
Schema v1 added cached payloads; v2 added runs, stages, fingerprints and
checkpoints; v3 added the rest of the original request to a run — its seed
keyword and result limit — so `resume` and `rerun` repeat the question that was
actually asked. Saved configuration snapshots use masked values. See
[pipeline](pipeline.md) and [runs](runs.md).

## Milestones

- M1: package, configuration, errors, logging, market, envelope and SQLite.
- M2: HTTP, retry, cache, rate limiting, normalization and Autocomplete.
- M3: bounded keyword expansion and language data.
- M4: Google Trends provider and parsing.
- M5: Google Ads ideas, metrics and interprocess throttling.
- M6: Search Console queries and opportunities.
- M7: niche, competitor and existing-site pipelines with budgets and dry-run.
- M8: durable runs, checkpoints, resume, rerun and schema v2.
- M9: scoring, lexical clustering, reports and analysis commands.
- M10: CLI/MCP documentation, Claude skill and documentation consistency tests.

Scoring formulas and confidence are documented in [scoring.md](scoring.md).

