# Metrics and result fields

## Response envelope

Every JSON result contains:

- `schema_version`: wire-format version.
- `data`: command-specific payload.
- `warnings` and `errors`: degraded sources and failures that must stay visible.
- `completeness`: `complete`, `partial`, or `empty`.
- `completeness_reason`: required explanation for `partial` and `empty`.
- `run_id`: saved-run identifier when persistence was requested.

Never interpret a missing field as zero. For `partial`, identify the missing source
or stopped budget in the answer.

## Research data

- `scenario`, `input`, `language`, `country`: selected workflow and market.
- `keywords`: collected keyword rows; `keyword` preserves display text and
  `normalized` is the deduplication form.
- `discovered_from`: sources that produced each keyword.
- `autocomplete_relevance`: provider ordering signal, not volume.
- `avg_monthly_searches`: rounded Google Ads planning volume; absolute in kind.
- `ads_competition`, `ads_competition_index`: advertiser competition, not SEO
  difficulty.
- `low_top_of_page_bid`, `high_top_of_page_bid`: Google Ads bid estimates.
- `gsc_impressions`, `gsc_clicks`: observed Search Console counts for the property
  and date scope.
- `gsc_ctr`, `gsc_position`: observed ratio and average position in that scope.
- `trends.timeline`, regional interest and related queries: relative values within
  one `normalization_scope`; never compare 0–100 values from separate requests.
- Trends also records request `keywords`, `geo`, `timeframe`, `retrieved_at` and
  `source`. Timeline points carry timestamp, label, values and data flags; regional
  rows carry geography and values; related rows carry query, value and label.
- `opportunities`: derived candidates based on impressions, CTR and position. Each
  has query, optional page, clicks, impressions, CTR, position, `kind` and a
  human-readable `reason`.
- `stats.expansion`: executed query count, reached depth and expansion stop reason.
- `stats.spend`: consumed Autocomplete, Ads, Trends, keyword and runtime budget.
- `stats.stopped_by`: limit that ended collection without making it an error.

`data_quality.sources` records each source's `available`, `used` and `detail`.
`absolute_metrics`, `relative_metrics` and `derived_metrics` classify fields.
`retrieved_at` gives observation time; `caveats` must be repeated when relevant.

## Four kinds of numbers

1. Google Ads volume and bids are planning metrics that Google may round and group
   across close variants.
2. Trends values are relative interest, normalized to 0–100 within a response.
3. Search Console counts are real measurements for one connected property, date
   range, search type and filters; they do not describe the entire market.
4. Scores, opportunity values, trend growth and clusters are `gkai` calculations.

## Score components

The keyword score is a 0–100 weighted average of available components:

- `demand`: logarithmic normalization of Ads volume against the configured demand
  reference, capped at 100.
- `trend`: `50 + 50 × clamp(growth, -1, 1)`; growth compares the latest timeline
  quarter with the previous quarter.
- `commercial`: top-of-page bid divided by the bid reference, capped at 100; high
  bid is preferred and low bid is the fallback.
- `opportunity`: Search Console impressions scaled to 1,000 multiplied by the
  normalized position gap from positions 1 through 30.

Each component exposes `available`, `raw`, `normalized`, `weight`, `contribution`
and `explanation`. An unavailable component is excluded from both numerator and
denominator. It is not zero: lack of Ads or Search Console data is not evidence of
zero demand, value or opportunity.

`confidence` reports coverage, not statistical certainty: `high` is four available
components, `medium` three, `low` one or two, and `none` zero. With no components,
the displayed score is 0 only as an empty aggregate and confidence is `none`.

See [project scoring documentation](../../../../docs/scoring.md) for formulas,
default weights, clustering and niche factors.
