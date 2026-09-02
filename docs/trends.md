# Google Trends

Google Trends is reached through an unofficial HTTP adapter with no third-party
wrappers. An official API exists but is in closed alpha with application-gated
access, so this project has none; `OfficialTrendsAdapter` only reserves the seam
for a future integration.

## How a request is performed

The adapter uses a three-step chain:

1. `GET /_/TrendsUi/data/batchexecute` warms up the session and obtains the
   `NID` cookie. The expected response here is HTTP 405: the method is not
   allowed, but the cookie is already set, so that response counts as success.
2. `GET /trends/api/explore` returns the descriptions of the available widgets
   together with their requests and tokens.
3. `GET /trends/api/widgetdata/*` loads the time series, the geography and the
   related queries in turn. A configured pause is held between widgets.

`explore` starts its body with `)]}'` and a newline, while `widgetdata` starts
with `)]}',` and a newline. The parser strips the common marker and discards
everything before the first JSON object; no fixed-length slice is used.

A failure of one widget yields a partial result with a warning. A failure of the
warm-up or of `explore` fails the whole request. After a configured number of
such consecutive failures the circuit breaker rejects new calls immediately, so
as not to deepen the block or waste time on a network that is bound to fail.

## Normalization and comparison

The 0-100 values are normalized only within a single request: 100 means the
maximum in that particular response. Results of separate requests cannot be
compared directly. The `gkai trends compare` command and the `analyze_trends`
MCP tool send up to five keywords in one request.

`normalization_scope` is the first 16 characters of the SHA-256 of canonical
JSON containing the keywords, country, timeframe and language. Identical
parameters produce an identical scope, different parameters a different one.
Comparing numeric values is safe only within the same scope.

## Settings

The unofficial provider is enabled by default. It can be turned off with a kill
switch:

```bash
export GKAI_TRENDS_ENABLED=false
```

The pause, cache TTL, circuit-breaker threshold and timezone are set by
`trends_pacing_seconds`, `trends_cache_ttl_seconds`,
`trends_circuit_breaker_failures` and `trends_timezone_minutes`.
