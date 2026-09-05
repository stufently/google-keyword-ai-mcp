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

## Quota project

A quota project identifies the Google Cloud project used for API quota.
It is required for `authorized_user` credentials: set
`search_console_quota_project_id` in configuration or the environment variable
`GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID` to your Google Cloud project ID.
Surrounding whitespace is trimmed; an empty or whitespace-only value is refused
as an invalid configuration. The project ID is not a secret.

The setting is applied to both supported credential types when supplied.
Service accounts usually use their own project and do not need this setting.
When omitted, credentials are left unchanged.

Without a quota project, Google returns HTTP 403 with reason
`accessNotConfigured` and this message:

> Your application is authenticating by using local Application Default Credentials. The searchconsole.googleapis.com API requires a quota project, which is not set by default.

The provider reports this reason as an invalid configuration and names
`GKAI_SEARCH_CONSOLE_QUOTA_PROJECT_ID` in the error. Other 403 responses,
including `forbidden` for a property the user cannot access, remain
authentication or authorization errors.

## Collection limits and completeness

The Search Analytics API accepts at most 25,000 rows in `rowLimit`. The provider
therefore requests each day separately and pages within that day using
`startRow`, then folds the days back into the range that was asked for: clicks
and impressions add, CTR is recomputed over the totals rather than averaged, and
position is averaged by impressions — which is how Google defines those metrics,
so the arithmetic follows the API rather than approximating it. What folding
cannot recover is what the API never sent: it returns top rows, not all of them,
and drops some data when `page` or `query` is among the dimensions. Rows come
back most-clicked first, or oldest first when `date` is one of the dimensions,
which is the order the API itself uses.

About 50,000 rows per property, search type, and day is an upper bound on what
the API exposes, not a guarantee that the response is complete.
Google's wording is "a maximum of 50K rows of data **per day** per search type",
so `search_console_daily_row_cap` is a fresh allowance for each day of data and
not a budget for the whole call. Every day of the range is requested; a day that
fills its allowance and still offers a full page stops there, and the days after
it are read as normal. When any day is cut that way, `truncated` is `true`,
`truncation_reason` names the days that hit the cap, and the envelope has
`completeness` set to `partial`. Consumers must not treat such a result as a
complete export.

`dataState` accepts `all`, `final` and `hourly_all`, and Google reads an omitted
value as `final`; anything else is refused here rather than sent, because the
parameter decides whether fresh, not-yet-final rows are included.

The cap bounds what is asked for, not only what is counted afterwards: each
request asks for `min(row_limit, cap - rows_read_today)` rows. Checking the cap
only after a full page arrived would spend up to `row_limit` rows past it —
25,000 with the defaults — which would make a setting named "cap" no such
thing. The price is that a request the cap shrinks cannot prove nothing
remained, so it is reported as truncated even where the data happened to end
there. A range that finishes below the cap returns a short page, which does
prove it, and stays complete.

Search Console data for the newest days can still be incomplete. Default query
windows therefore end on the day before yesterday, not today or yesterday.

Opportunity mining uses configurable minimum impressions, position window, and
maximum CTR thresholds. Configure the `gsc_opportunity_*` settings rather than
assuming the defaults fit every site.

For large properties, the appropriate next step is a scheduled daily Search
Console bulk export to BigQuery. That backend is not implemented in this
version.
