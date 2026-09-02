# CLI reference

Run `gkai <command> --help` for the complete option contract. JSON is the default
format unless shown otherwise.

## Exit codes

Every command prints its envelope to stdout and then exits according to
`completeness`, so a non-zero status is a verdict about the data, not a crash:

- `0` — `completeness: complete`.
- `1` — `completeness: partial` or `empty`. Valid JSON was still printed; read
  `completeness_reason`, `warnings` and `errors` to see what is missing.
  Absent optional credentials and exhausted budgets land here.
- `2` — the command line itself was rejected (unknown option, wrong number of
  arguments). Nothing ran and no envelope was produced.

Never report exit 1 as a failed run without parsing the envelope first.

## Availability and configuration

```bash
gkai doctor --format json
gkai config show --format json
```

`gkai doctor` reports provider and database availability. `gkai config show` prints
the effective configuration with secrets masked.

## Discovery and expansion

```bash
gkai suggest "running shoes" --language en --country US --limit 20
gkai expand "running shoes" --language en --country US --depth 2 --max-queries 100
gkai trends compare "running shoes" "trail shoes" --country US --timeframe "today 12-m"
```

`gkai suggest` performs one Autocomplete request. `gkai expand` fans out strategies
under explicit limits. `gkai trends` compares relative interest in one normalization
scope.

## Research workflows

```bash
gkai competitor competitor.com --seed-keyword shoes --language en --country US
gkai research "running shoes" --language en --country US --dry-run
gkai research "running shoes" --language en --country US --save-run
```

`gkai competitor` returns Google Ads ideas associated with a site or URL. `gkai
research` selects `niche`, `competitor`, or `site` with `--scenario auto`; budgets
include `--max-keywords`, `--max-autocomplete-queries`, `--max-ads-calls`,
`--max-trends-calls`, and `--max-runtime`.

## Google Ads

```bash
gkai ads ideas "running shoes" --url https://example.com/page --country US
gkai ads ideas --site competitor.com --country US
gkai ads historical "running shoes" "trail shoes" --country US
```

`gkai ads ideas` accepts keyword, URL, combined keyword-and-URL, or site seeds;
site seeds cannot be combined with keywords or URL. `gkai ads historical` requires
one or more keywords.

## Search Console

```bash
gkai gsc properties
gkai gsc queries "sc-domain:example.com" --days 28 --dimension query --limit 100
gkai gsc opportunities "sc-domain:example.com" --days 28 --limit 50
```

`gkai gsc properties` lists accessible properties. `gkai gsc queries` also accepts
`--start-date`, `--end-date`, `--country`, and `--search-type`. `gkai gsc
opportunities` applies configured impression, CTR and position thresholds.

## Saved runs and analysis

```bash
gkai run list --limit 20
gkai run show <run_id>
gkai run export <run_id>
gkai run resume <run_id>
gkai run rerun <run_id>
gkai score <run_id> --limit 100
gkai cluster <run_id>
gkai explain-score <run_id> "running shoes"
gkai niche analyze <run_id>
gkai keyword inspect <run_id> "running shoes"
```

`gkai run resume` continues the same run; `gkai run rerun` creates a new one.
`gkai score` returns scored keywords, `gkai cluster` lexical clusters, `gkai
explain-score` a component breakdown, `gkai niche analyze` aggregate niche factors,
and `gkai keyword inspect` provenance for one keyword.

