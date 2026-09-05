# Research pipeline

There are three research scenarios because their useful first source differs.
A new niche starts with broad Autocomplete expansion; a competitor starts with
Google Ads site or URL ideas; an existing site starts with its own Search Console
queries and opportunities. Forcing one linear pipeline onto all three would either
waste calls or erase the meaning of the input.

## Scenarios

- `niche`: expand a topic, deduplicate and filter, enrich selected candidates in
  Ads batches of at most 20, then request Trends for the original seed.
- `competitor`: request Ads ideas for a domain or URL, with optional keyword and
  URL seed; if Ads is
  unavailable *or its call budget is spent*, expand the optional seed with
  Autocomplete. A run already past `max_runtime_seconds` starts no fallback: the
  expander keeps its own clock and starts it fresh on every call, so a fallback
  launched past the ceiling would spend the whole runtime allowance a second
  time. Each of the three outcomes — no credentials, spent call budget, spent
  clock — is named in its own warning.
- `site`: read Search Console query/page rows, derive opportunities, enrich those
  queries in Ads, then request Trends for the highest-impression query.

`--scenario auto` selects among them. An explicit `niche`, `competitor`, or `site`
always wins.

## Cheap-first and budgets

Work uses cached provider responses first, then free Autocomplete, filtering and
deduplication, Google Ads, and finally Trends. Ads never receives discarded
candidates.

- `max_keywords`: maximum keyword rows retained during collection.
- `max_autocomplete_queries`: maximum expansion queries.
- `max_ads_calls`: maximum Keyword Planner operations. A historical-metrics
  batch of up to 20 keywords counts as one, and so does a keyword-ideas
  request. Google may split its answer across pages; the provider walks them
  under the shared one-request-per-second limit, but stops after
  `google_ads_max_pages` (default 20) and reports the answer as truncated
  rather than draining the pager. A wide seed can still cost more time than
  the call counter suggests, bounded by that page ceiling.
- `max_trends_calls`: maximum Trends requests.
- `max_runtime_seconds`: total elapsed runtime ceiling.

Reaching a budget is not an error, and neither is spending all of it. Collected
data is returned, and `stats.stopped_by` names a limit only when that limit
actually cost something: an operation was refused, or a list was trimmed. A run
that fits exactly inside its allowance is complete.

`--dry-run` returns the scenario steps, source availability, and arithmetic call
estimates without calling any provider. Use it to inspect likely cost and order.

## Reading data quality

`data_quality.sources` says which sources were available and actually used.
`absolute_metrics`, `relative_metrics`, and `derived_metrics` separate measurements
by meaning. `caveats` records interpretation limits and fallback sorting. Missing
optional credentials produce warnings and a partial result, not a crash.

The standing caveats are:

- Trends values are 0-100 relative interest, not search volume.
- `ads_competition` is advertiser competition, not SEO difficulty.
- A site seed yields keyword ideas Google associates with the site, not the
  queries the site actually ranks for.

Pass `--save-run` to persist a run and revisit it later; see [runs](runs.md).
Scoring and clustering are applied to a saved run rather than to these flat
keyword lists; see [scoring](scoring.md).
