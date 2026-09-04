# Privacy and local data

The tool stores local state in one SQLite database:

```text
${XDG_DATA_HOME:-~/.local/share}/google-keyword-ai-mcp/gkai.sqlite3
```

Set `GKAI_DATA_DIR` to choose another directory.

## Stored locally

- Parsed provider-response cache entries with provider, endpoint, account scope,
  parser version and expiry metadata.
- Saved research runs, results, stage checkpoints, budgets, errors and timestamps.
- A configuration snapshot for each run. Secret values are masked before storage.

Developer tokens, OAuth client secrets, refresh tokens and credential-file contents
are not written to the database. Secrets remain in `GKAI_...` environment variables
or credential files you manage. Raw HTTP responses from authenticated Google Ads
and Search Console APIs are not retained by default; normalized cached results and
saved research output may contain the metrics and query rows used by the tool.

To remove all cached responses and reclaim their unused database pages without
losing saved run history, stop active writers and run:

```bash
gkai cache purge --all --vacuum
```

To remove all local cache **and** run history, stop active `gkai` processes and
delete the `gkai.sqlite3` file plus its `-wal` and `-shm` sidecars in the same
directory. This is irreversible; credential files are separate and are not removed.

## Data sent to providers

Autocomplete and Trends requests send keywords, locale, geography and timeframe to
Google's unofficial endpoints. Google Ads sends seeds, customer/account context and
market criteria. Search Console sends the property, date range, dimensions and
filters using your credential identity. These requests are subject to Google's
terms, logging, quotas and privacy practices; do not submit sensitive query seeds
unless that disclosure is acceptable.
