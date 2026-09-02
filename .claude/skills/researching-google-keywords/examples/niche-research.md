# Example: niche research

## User request

> Исследуй поисковый спрос по теме "аренда квартиры в Паттайе" для русского
> языка и Таиланда.

## Commands

First inspect availability:

```bash
gkai doctor --format json
```

Preview the plan before spending provider calls:

```bash
gkai research "аренда квартиры в Паттайе" --language ru --country TH --dry-run
```

Summarize the selected niche scenario, sources and estimated Autocomplete, Ads and
Trends calls. After the user accepts the plan, preserve the substantial run:

```bash
gkai research "аренда квартиры в Паттайе" --language ru --country TH --save-run
```

If a `run_id` is returned, optional follow-up analysis is:

```bash
gkai score <run_id>
gkai cluster <run_id>
gkai niche analyze <run_id>
```

## Good answer

Lead with the strongest keyword groups and show their provenance. Label Ads
`avg_monthly_searches` and bids as rounded planning metrics. Describe Trends only
as relative 0–100 interest in this response. If Search Console was not part of a
niche run, do not present any property-specific claims.

State `completeness`. If it is `partial`, name the missing provider from
`completeness_reason` and `warnings`; for example, without Ads say that absolute
volume and bid data are absent. Include relevant `data_quality.caveats` and explain
that a low-confidence score has fewer available components.

## Never say

- Do not call Trends 0–100 values monthly search volume.
- Do not call Ads competition SEO difficulty.
- Do not treat missing provider fields as zero.
- Do not run the paid/full plan before showing the dry run.

