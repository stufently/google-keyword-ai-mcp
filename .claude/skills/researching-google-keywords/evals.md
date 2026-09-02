# Skill acceptance scenarios

These are reviewable behavioral evaluations, not executable tests. Run each with
the skill loaded and inspect the commands and final answer against every criterion.

## Eval 1: Russian niche in Thailand

**Input:** `Исследуй поисковый спрос по теме "аренда квартиры в Паттайе" для
русского языка и Таиланда.`

**Expected actions:** Run `gkai doctor --format json`; run `gkai research` with
`--language ru --country TH --dry-run`; explain sources and estimates; wait for
acceptance before the real request; use `--save-run` for the full study; read
`completeness`, warnings and `data_quality`.

**Good-answer criteria:** Distinguishes Ads absolute planning metrics, Trends
relative interest and derived scores; reports missing sources in a partial result;
does not turn missing values into zero.

**Typical errors:** Skipping doctor or dry-run, claiming Trends is volume, calling
Ads competition SEO difficulty, or issuing a confident complete answer from a
partial envelope.

## Eval 2: Competitor themes

**Input:** `Посмотри, какие keyword themes Google связывает с competitor.com.`

**Expected actions:** Run doctor; preview `gkai research competitor.com` with a
dry run; use either approved saved research or `gkai competitor competitor.com`;
stop with the reported empty reason if Google Ads is unavailable and no valid
fallback seed exists.

**Good-answer criteria:** Organizes site-seed ideas into themes and explicitly says
they are associations returned by Google, not ranking queries, traffic evidence or
Search Console data. Advertising competition is not labeled SEO difficulty.

**Typical errors:** Saying the competitor ranks for the ideas, inventing organic
positions, or hiding unavailable Ads credentials.

## Eval 3: Existing-site opportunities

**Input:** `Найди запросы моего сайта, где много показов, но страницу можно
улучшить.`

**Expected actions:** Run doctor; use `gkai gsc properties` if the property is not
known; call `gkai gsc opportunities <property>`; use queries or a dry-run site
research only when additional detail is needed; report an empty reason when Search
Console credentials are unavailable.

**Good-answer criteria:** Connects impressions, clicks, CTR and position to the
property and date window; labels opportunity as derived; names missing enrichment
for partial results.

**Typical errors:** Treating GSC impressions as global market volume, promising a
ranking improvement, or substituting competitor site-seed ideas for first-party
queries.

