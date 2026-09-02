# Example: existing-site opportunities

## User request

> Найди запросы моего сайта, где много показов, но страницу можно улучшить.

## Commands

Confirm Search Console credentials and database availability:

```bash
gkai doctor --format json
```

Discover the exact property identifier when the user has not supplied it:

```bash
gkai gsc properties
```

Request derived opportunities for the desired date range:

```bash
gkai gsc opportunities "sc-domain:example.com" --days 28 --limit 50
```

Inspect underlying query observations if more context is needed:

```bash
gkai gsc queries "sc-domain:example.com" --days 28 --dimension query --limit 100
```

For an enriched, resumable site study, preview before execution:

```bash
gkai research "sc-domain:example.com" --scenario site --dry-run
gkai research "sc-domain:example.com" --scenario site --save-run
```

## Good answer

Prioritize queries with high property impressions and a weak average position or
low CTR. Report impressions, clicks, CTR, position, property and date window
together. Explain that the opportunity value is a `gkai` calculation, while the
underlying Search Console counts are real observations for this property.

State `completeness` and all relevant warnings. If credentials are absent, report
the `empty` reason and explain how a credential file must be configured; do not
replace first-party query observations with competitor site-seed ideas.

## Never say

- Do not generalize property impressions into total market search volume.
- Do not promise rankings from an opportunity score.
- Do not treat average position as a fixed rank for every impression.
- Do not hide missing Ads or Trends enrichment in a `partial` result.

