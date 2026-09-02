---
name: researching-google-keywords
description: "Researches Google keyword demand, long-tail queries, niche opportunity, competitor-derived keyword ideas, trends and Search Console opportunities using the gkai CLI. Use for SEO keyword research, niche analysis, Google search-demand analysis, keyword expansion, competitor keyword discovery, Google Ads Keyword Planner metrics, Google Trends analysis and Search Console query mining."
---

# Researching Google keywords

Use this skill to choose and orchestrate a `gkai` workflow. Keep every claim tied to
the source that produced it.

## Always start here

Run this before choosing commands:

```bash
gkai doctor --format json
```

Use its provider availability to adapt the workflow. Do not ask the user which API
to use: choose from their intent and the providers reported as available. Missing
Google Ads or Search Console credentials should produce an `empty` or `partial`
result with a reason, not an invented substitute.

## Choose the workflow by intent

- Topic or phrase: begin with
  `gkai research "<topic>" --language <language> --country <country> --dry-run`.
- Competitor domain or URL: begin with
  `gkai research <domain-or-url> --language <language> --country <country> --dry-run`;
  automatic scenario selection handles the target. Use
  `gkai competitor <domain-or-url>` for direct site-seed ideas.
- An owned site with Search Console connected: use
  `gkai gsc opportunities <property>`.

Read [the workflow guide](reference/workflow.md) before running a full scenario.
Use [the CLI reference](reference/cli.md) when composing flags or follow-up commands.

## Spend cheaply before expensively

For `research`, run `--dry-run` first and show the planned sources, steps and call
estimates. Run the real request only after the user accepts that plan. Add
`--save-run` to substantial research so it can be inspected, scored, clustered,
exported or resumed without repeating successful calls.

## Report numbers by kind

1. Google Ads `avg_monthly_searches` and bid fields are absolute planning metrics,
   but Google rounds volumes and combines close variants.
2. Google Trends is relative interest normalized to 0–100 within one response.
3. Search Console impressions and clicks are actual observations for the connected
   property and selected date range, not estimates of the whole market.
4. `gkai` scores, confidence, opportunities and cluster summaries are derived
   calculations, not provider measurements.

Preserve these three prohibitions verbatim in meaning and never imply their inverse:

- Google Trends values are relative interest on a 0-100 scale, not search volume.
- Google Ads competition describes advertiser demand, not SEO difficulty.
- A site seed returns keyword ideas Google associates with the site, not the queries the site actually ranks for.

Read [the metrics guide](reference/metrics.md) before interpreting fields or scores.

## Read every result envelope

- `completeness: complete` means the requested result completed.
- `completeness: partial` means usable data is missing: explicitly tell the user
  what is absent using `completeness_reason`, `warnings` and `errors`.
- The CLI exits 1 for `partial` and `empty` and 2 only for a malformed command
  line. A non-zero exit still prints a valid envelope, so parse stdout before
  calling a run failed.
- `completeness: empty` means there is no usable data; state the reason and do not
  turn missing values into zeroes.
- Read `data_quality.sources` for availability and actual use, then report every
  relevant entry in `data_quality.caveats`. Distinguish `absolute_metrics`,
  `relative_metrics` and `derived_metrics`.

## Examples

- [Niche research](examples/niche-research.md)
- [Competitor research](examples/competitor-research.md)
- [Existing-site opportunities](examples/existing-site.md)

