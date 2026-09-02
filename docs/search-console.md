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
`startRow`.

About 50,000 rows per property, search type, and day is an upper bound on what
the API exposes, not a guarantee that the response is complete. When collection
reaches the configured daily cap *and rows remained to read*, `truncated` is
`true`, `truncation_reason` explains which boundary was reached, and the
envelope has `completeness` set to `partial`. Consumers must not treat such a
result as a complete export. A range whose last row happens to land exactly on
the cap was read in full and stays complete: the cap was spent, not hit.

Search Console data for the newest days can still be incomplete. Default query
windows therefore end on the day before yesterday, not today or yesterday.

Opportunity mining uses configurable minimum impressions, position window, and
maximum CTR thresholds. Configure the `gsc_opportunity_*` settings rather than
assuming the defaults fit every site.

For large properties, the appropriate next step is a scheduled daily Search
Console bulk export to BigQuery. That backend is not implemented in this
version.
