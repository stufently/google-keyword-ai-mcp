# Google Search Console

Search Console commands require a JSON credentials file configured as
`search_console_credentials_path` (or `GKAI_SEARCH_CONSOLE_CREDENTIALS_PATH`).
The file must have one of these `type` values:

- `service_account`: create a service account and JSON key in Google Cloud,
  enable the Search Console API, then add the service-account email as a user of
  the Search Console property;
- `authorized_user`: obtain an OAuth client in Google Cloud and create an
  authorized-user JSON file with the Search Console read-only scope on a
  browser-capable workstation, then copy that file to the server.

This version does not implement interactive OAuth. Both the sandbox and the
target server are browserless, so authentication always loads an existing file.
Only the read-only scope is requested.

## Collection limits and completeness

The Search Analytics API accepts at most 25,000 rows in `rowLimit`. The provider
therefore requests each day separately and pages within that day using
`startRow`, then folds the days back into the range that was asked for: clicks
and impressions add, CTR is recomputed over the totals rather than averaged, and
position is averaged by impressions — which is how Google averages it over a
range, so the result matches a single ranged request rather than approximating
one. Rows come back most-clicked first.

About 50,000 rows per property, search type, and day is an upper bound on what
the API exposes, not a guarantee that the response is complete. When collection
reaches the configured daily cap *and rows remained to read*, `truncated` is
`true`, `truncation_reason` explains which boundary was reached, and the
envelope has `completeness` set to `partial`. Consumers must not treat such a
result as a complete export.

The cap bounds what is asked for, not only what is counted afterwards: each
request asks for `min(row_limit, cap - rows_fetched)` rows. Checking the cap
only after a full page arrived would spend up to `row_limit` rows past it —
25,000 with the defaults — which would make a setting named "cap" no such
thing. The price is that a request the cap shrinks cannot prove nothing
remained, so it is reported as truncated even where the data happened to end
there. A day that finishes below the cap returns a short page, which does prove
it, and stays complete.

Search Console data for the newest days can still be incomplete. Default query
windows therefore end on the day before yesterday, not today or yesterday.

Opportunity mining uses configurable minimum impressions, position window, and
maximum CTR thresholds. Configure the `gsc_opportunity_*` settings rather than
assuming the defaults fit every site.

For large properties, the appropriate next step is a scheduled daily Search
Console bulk export to BigQuery. That backend is not implemented in this
version.
