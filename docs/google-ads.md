# Google Ads Keyword Planner

The Google Ads provider is optional: without the full set of credentials the
other sources keep working and `gkai doctor` reports `missing credentials`.

You need a developer token, a customer ID, an OAuth client ID, a client secret
and a refresh token. They are obtained from the Google Ads API Center and the
Google Cloud Console. For an MCC you may additionally supply a login customer
ID. Settings are passed through the `google_ads_*` fields or the
`GKAI_GOOGLE_ADS_*` environment variables.

Keyword Planner supports four mutually exclusive seed modes:

- `keyword_seed` — one or more keywords;
- `url_seed` — the URL of a page;
- `keyword_and_url_seed` — keywords together with a URL;
- `site_seed` — a whole site.

Exactly one mode is filled in: `site_seed` cannot be combined with keywords or
a URL.

API bids arrive in micros and are divided by 1,000,000 before being returned.
Only currency-unit values remain in the response; no `_micros` fields are
exposed.

The Keyword Planning API is limited to roughly one request per second per
customer ID. The CLI and the MCP server run in different processes, so the
shared limit is enforced with an interprocess file lock rather than a local
semaphore.

Ideas are cached for a week and historical metrics for 30 days, because Google
refreshes the latter about once a month. The cache key includes the customer
ID so that accounts never see each other's data.

Each ideas request asks for `google_ads_page_size` (default 1000) rows per
page. The pager is walked at most `google_ads_max_pages` times (default 20,
`GKAI_GOOGLE_ADS_MAX_PAGES`). Stopping at the ceiling marks the answer
truncated and skips the cache: a partial page stored for a week would otherwise
be served as complete, including to a later run with a higher ceiling.

Country criteria IDs come from the official `geotargets-2026-08-12.csv`, and
language IDs from the Google Ads `codes-formats` page; both were captured on
2026-09-02. The project code `zh` is mapped to `zh_CN` (1017). Google Ads has
no Kazakh `kk`, so that market is rejected rather than substituted with a
similar ID.

Keep in mind:

- `ads_competition` is advertiser competition, not SEO difficulty;
- a site seed yields keyword ideas Google associates with the site, not the
  queries the site ranks for;
- Google rounds volumes and merges close variants, so volume is not an exact
  request counter.
