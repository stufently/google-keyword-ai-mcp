# Workflow guide

Every workflow starts with `gkai doctor --format json`. Read all four provider rows
and the database status before choosing the sequence. Do not ask the user to select
an API.

## Topic or niche

1. Preview the automatically selected niche scenario:

   ```bash
   gkai research "running shoes" --language en --country US --dry-run
   ```

2. Explain its estimated Autocomplete, Ads and Trends calls. If the user accepts,
   save a substantial run:

   ```bash
   gkai research "running shoes" --language en --country US --save-run
   ```

3. Start with the returned keywords and `data_quality`; use `gkai score <run_id>`,
   `gkai cluster <run_id>` and `gkai niche analyze <run_id>` only when a run ID was
   saved.

The cheap-first order is cached results, Autocomplete expansion and filtering,
Google Ads enrichment, then Trends. If Ads is unavailable, retain Autocomplete
ideas and say that absolute search volume and bid data are absent. If Trends is
unavailable, do not infer momentum from Ads. If Autocomplete is unavailable, use
available Ads ideas; an `empty` result remains empty rather than guessed.

## Competitor domain or URL

1. Preview automatic scenario selection:

   ```bash
   gkai research competitor.com --language en --country US --dry-run
   ```

2. Run with `--save-run` when later comparison, scoring or export matters:

   ```bash
   gkai research competitor.com --language en --country US --save-run
   ```

3. For direct Google Ads site/URL seed ideas without the full pipeline, use:

   ```bash
   gkai competitor competitor.com --language en --country US
   ```

Google Ads is the useful first source. Its result contains ideas Google associates
with the supplied site; it is not ranking-query evidence. If Ads is unavailable,
the research scenario can fall back to Autocomplete only when a `--seed-keyword`
is supplied. Otherwise report the `empty` reason and stop.

## Existing site with Search Console

1. Confirm Search Console is available, then list accessible property identifiers:

   ```bash
   gkai gsc properties
   ```

2. Find high-impression, low-CTR or weak-position rows:

   ```bash
   gkai gsc opportunities "sc-domain:example.com" --days 28
   ```

3. Use `gkai gsc queries` for the underlying property observations. For the full
   site pipeline, preview and then save an explicit site run:

   ```bash
   gkai research "sc-domain:example.com" --scenario site --dry-run
   gkai research "sc-domain:example.com" --scenario site --save-run
   ```

If Search Console is unavailable, do not substitute competitor site-seed ideas:
they answer a different question. Explain the missing credentials reported by
`doctor`; the command returns `empty` with a reason rather than ranking data.

## Saved runs

Use `--save-run` for large call budgets, expensive provider work, comparisons that
need provenance, or any research that may need continuation. The run commands are:

```bash
gkai run list
gkai run show <run_id>
gkai run export <run_id>
gkai run resume <run_id>
gkai run rerun <run_id>
```

`resume` continues the same run and reuses valid checkpoints. `rerun` creates a new
run from the same request. `show` explains stage state; `export` returns the stored
result. Read [project run documentation](../../../../docs/runs.md) for checkpoint
and version rules.
