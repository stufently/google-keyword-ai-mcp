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

## Optional relative demand

`gkai research … --demand` adds a common demand stage to all three scenarios,
after their final keyword sort. Without this option the provider calls, ordering,
and existing fields keep their previous behavior; the five new `demand_*` fields
and `stats.demand` are present as `null`. Demand does not change the research
sort or opportunity scores. With Ads the sort uses volume; otherwise it uses
Autocomplete relevance, breaking ties alphabetically.

```bash
gkai research "купить квартиру" --language ru --country RU --demand
gkai research "купить квартиру" --language ru --country RU \
  --demand-anchor "купить квартиру москва" --format markdown
```

The seed is excluded by its normalized spelling, both as an anchor and as a
participant. Here the seed means the query chosen for the scenario's existing
single-key Trends request: the original topic for `niche`, the most notable
keyword for `competitor`, and the highest-impression query for `site`.
The pure `select_research_candidates` helper preserves the sorted order,
normalizes and deduplicates the remaining candidates, and chooses the first
one as anchor. `--demand-anchor` overrides that choice and implies `--demand`.
The explicit anchor must be among these candidates, even if it would normally
fall beyond the budget slice. An absent anchor, including the excluded seed,
returns an `empty` refusal envelope. Candidate validation takes place after
collection; a dry run cannot check membership.

Seed exclusion follows the task author's live experiment of 6–7 September 2026,
using the same eight candidates from research of «купить квартиру»:

| Candidate | Seed anchor «купить квартиру» | Candidate anchor «купить квартиру москва» |
|---|---:|---:|
| купить квартиру спб | 4.510 | 75.653 |
| купить квартиру циан | 2.887 | 52.044 |
| купить квартиру екатеринбург | 1.469 | 25.587 |
| купить квартиру челябинск | 0.335 | 9.971 |
| купить квартиру уфа | 0.129 | 8.283 |
| купить квартиру щелково | 0.000 | 3.060 |

The broad seed compressed the tail into a measured zero. That zero must not be
read as absence of demand. Choosing the first candidate is still a heuristic:
an alphabetical tie can select a weak anchor. If its batch collapses, the rows
explicitly receive `anchor_collapsed`; there is no automatic anchor replacement.

### Budget and statistics

Available batches are `max_trends_calls - spend.trends_calls` at this stage.
One batch holds the common anchor and at most four participants, so the subset
contains at most **1 + 4 × available batches** unique keys. With the default
three calls, the single-seed stage spends one and demand gets two: **nine keys**.
Each attempted comparison, including a failed or cached one, increments
`spend.trends_calls`. Before every batch the guard checks both the call ceiling
and `max_runtime_seconds`; an already started request can finish, but a new one
does not start after the deadline. There is no extra fifty-key cap on research;
the standalone `gkai demand` command keeps its own 2–50-key contract.

`stats.demand` contains:

| Field | Meaning |
|---|---|
| `anchor` | Normalized anchor, or `null` if no batch was ranked |
| `requested` | All unique, nonempty candidates after excluding the seed, before budget slicing |
| `ranked` | Unique keys in batches actually attempted, including failed batches and the anchor once |
| `batches` | Comparison calls spent |
| `truncated_by_budget` | Candidate selection or batch execution was cut by the call/runtime budget |

Zero available batches means no demand requests and all row fields stay `null`;
the envelope explicitly names `max_trends_calls` and is `partial` when research
data exists. Budget truncation is also `partial`, with the limiting demand budget
in `completeness_reason`, even when an earlier source produced another warning.
Fewer than two eligible candidates or unavailable Trends skip demand with an
explanation. `--limit` still trims only the displayed keyword list after research;
statistics describe the full stage, including a ranked anchor outside that view.

### Row contract and reports

`demand_relative` uses the candidate anchor as 100, not the original Trends
0–100 normalization or absolute search volume. Values can exceed 100.
`demand_measured_weeks`, `demand_weeks`, and `demand_reason` carry the coverage
and explanation from the existing [demand calculation](demand.md).
The configured `demand_min_coverage` threshold applies unchanged: equality passes,
and the anchor is exempt. In the same live experiment, «йошкар ола» at 5/52 weeks
was `low_coverage`, while «щелково» at 13/52 met the default 0.25 threshold.

| `demand_status` | Meaning | `demand_relative` |
|---|---|---|
| `null` | Not in this run's ranked subset: demand was off, skipped, or the key was excluded/cut | `null` |
| `measured` | Measured with sufficient coverage | Number, including measured `0.0` |
| `low_coverage` | Insufficient measured whole weeks | `null` |
| `below_resolution` | No measured whole weeks beside this anchor | `null` |
| `anchor_collapsed` | Batch cannot be placed on the common scale | `null` |
| `batch_failed` | Provider failed to supply the batch | `null` |

`null` is **not a sixth `DemandStatus`**. The five statuses describe ranked
keywords; `None` describes the fact that a keyword was not ranked. Any ranked
row with a missing demand number makes research `partial`, even when every
demand batch failed: previously collected research keywords still exist.

Markdown adds a relative-demand table when requested or populated, explicitly
renders all five statuses and marks unranked rows separately. An unknown status
raises a `ValueError` naming that value. Optional `GEO_MAP`/`RELATED_QUERIES`
failures during comparison are retained in `data_quality.caveats`; they do not
invalidate a usable demand timeline or change completeness on resume.

Saved runs store demand options in the existing configuration snapshot and add
a `demand` stage to their fingerprints. Checkpoint reuse, replay and `run rerun`
preserve the request, including its explicit anchor. No database migration is
needed, and old runs without these options remain demand-disabled.

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
