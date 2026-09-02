# Scoring, clustering, and niche analysis

Keyword scores are transparent weighted averages on a 0–100 scale. The four
components are independent and may be unavailable.

## Keyword formulas

- **Demand:** `100 × log10(volume + 1) / log10(demand_reference + 1)`, capped
  at 100. Search demand spans orders of magnitude, so a logarithm preserves the
  meaningful difference between 100 and 1,000 without letting very large terms
  swamp everything else.
- **Trend:** `50 + 50 × clamp(growth, -1, 1)`. Flat interest is 50, a doubling
  reaches 100, and a complete decline reaches 0.
- **Commercial:** `100 × min(top_of_page_bid / bid_reference, 1)`. The high bid
  is preferred; the low bid is the fallback.
- **Opportunity:** `100 × min(impressions / 1,000, 1) × clamp((position - 1) /
  29, 0, 1)`. Many impressions combined with a weak position expose more room
  to improve.

The weights and references live in `Settings` and can be changed through
`.gkai.toml` or `GKAI_...` environment variables. Defaults are demand 0.35,
trend 0.20, commercial 0.20, opportunity 0.25, demand reference 100,000, and
bid reference 5.0.

An unavailable component is excluded from both the weighted numerator and
denominator. It is not treated as zero: missing Ads or Search Console access is
not evidence of zero demand or zero value. With no available components the
score is 0 and confidence is `none`.

Confidence reports coverage: `high` means four components, `medium` three,
`low` one or two, and `none` zero. Every component also reports its raw value,
normalized value, weight, contribution, availability, and explanation.

Trend growth compares the mean of the last quarter of a timeline with the
previous quarter. It requires at least eight points and uses `values[0]` from
one Trends response. Values are comparable only inside that response's single
`normalization_scope`; values from different requests must never be compared.
Points Google marks as having no data are dropped rather than read as zero
interest: a week that could not be measured is not a week of no demand, and
averaging those zeros in would report a collapse that never happened.

The trend component belongs to the run, not to the keyword next to it. Trends
is queried once per run, for a single series, and that one growth figure scores
every keyword — so each explanation names the series it came from.

## Niche factors

Niche analysis separately reports total measurable demand, significant-keyword
count, long-tail depth, trend direction, commercial value, the share of demand
held by the top five keywords, cluster diversity, and existing-site coverage. Its score
is the arithmetic mean of available factors. The factor breakdown is always
shown because a lone aggregate would hide which sources were missing and which
market characteristic drove the result.

Clustering is deterministic and lexical. It uses normalized-token Jaccard
similarity and keeps the implementation boundary open for future embedding,
SERP, or LLM clusterers without pretending those exist today.

## Limitations that must remain visible

- Google Trends values are relative interest on a 0-100 scale, not search volume.
- Google Ads competition describes advertiser demand, not SEO difficulty.
- A site seed returns keyword ideas Google associates with the site, not the
  queries the site actually ranks for.

When search-result-page data is absent, SEO difficulty remains unknown.
Advertising competition is never substituted for it.
