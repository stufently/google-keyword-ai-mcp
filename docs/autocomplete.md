# Google Autocomplete

Google Keyword AI uses two Autocomplete endpoints in order:

1. `https://www.google.com/complete/search` with `client=chrome`.
2. `https://suggestqueries.google.com/complete/search` with `client=firefox`
   as a fallback.

Both are unofficial, undocumented sources. Google provides no stability or
availability guarantees for them.

Every request includes `ie=utf-8` and `oe=utf-8`. These parameters are required
to decode non-ASCII queries, including Cyrillic, as UTF-8 reliably.

The primary `client=chrome` response handles the `gl` country parameter more
accurately and can include `google:suggestrelevance`. The fallback
`client=firefox` response may be less geographically relevant and does not
provide relevance values.

`google:suggestrelevance` is Google's internal suggestion ranking weight. It is
not search frequency, search volume, or an estimate of monthly queries.

Successful responses are cached for 86,400 seconds (24 hours) by default. Set
`GKAI_AUTOCOMPLETE_CACHE_TTL_SECONDS` or
`autocomplete_cache_ttl_seconds` in `.gkai.toml` to change the TTL. Set
`GKAI_CACHE_ENABLED=false` to disable cache reads and writes.
