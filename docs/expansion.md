# Semantic expansion

`gkai expand` fans one seed out through Google Autocomplete and returns normalized,
deduplicated keyword candidates in the standard envelope.

## Strategies

- `suffix_alphabet`: `seed a`, `seed b`, and so on.
- `prefix_alphabet`: `a seed`, `b seed`, and so on.
- `digits`: `seed 0` through `seed 9`.
- `modifiers`: informational, commercial, comparison, and transactional prefixes
  and suffixes such as `how seed` or `seed reviews`.

The alphabets live in `src/google_keyword_ai/data/alphabets/<language>.txt`, one
lowercase letter per line. Modifiers live in
`src/google_keyword_ai/data/modifiers/<language>.toml`, grouped into the four
categories above, with optional `prefix` and `suffix` arrays. To add a language,
add both files under its language code. A language without packaged files
deliberately uses the English alphabet and modifier set.

## Request count and safeguards

Every seed first costs one direct query. A Russian pass with all strategies adds
33 suffix queries, 33 prefix queries, 10 digit queries, and roughly 30 modifier
queries: about 107 requests including the direct seed query. Deeper expansion can
therefore grow into tens of thousands of requests.

`max_queries` caps executed requests, `max_results` caps unique candidates,
`max_runtime` caps elapsed monotonic runtime, and `depth` caps recursive fan-out.

`depth` counts **rounds** of fan-out, not levels below the first one: `--depth 1`
queries the seed and its generated variants and stops there, `--depth 2` also
expands the keywords found in the first round. Each extra round multiplies the
request count, so raise it together with `--max-queries`.

Stopping because the requested depth was reached is a complete result, not a
partial one — there is always another level below, and reporting every ordinary
run as partial would make the exit code useless. Only the budget guards
(`max_queries`, `max_results`, `max_runtime`) mark the answer as cut short.
Every safeguard is checked before the next request. Reaching one returns the
keywords collected so far as a partial result; it is an intentional stop, not an
error. A failed non-initial request is skipped, while failure of the first direct
seed request is reported to the caller.
