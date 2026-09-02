# Example: competitor research

## User request

> Посмотри, какие keyword themes Google связывает с competitor.com.

## Commands

Check whether Google Ads credentials are available:

```bash
gkai doctor --format json
```

Preview the competitor scenario and call estimate:

```bash
gkai research competitor.com --language en --country US --dry-run
```

After approval, save the full research when follow-up analysis is useful:

```bash
gkai research competitor.com --language en --country US --save-run
```

For the narrower question, direct site-seed ideas are also available:

```bash
gkai competitor competitor.com --language en --country US
```

## Good answer

Group returned ideas into recurring themes and cite Google Ads site seed as their
source. Report Ads volume and bids only when present. State the envelope's
`completeness`, warnings and source caveats. If Ads credentials are unavailable,
explain the `empty` result; do not manufacture themes from the domain name.

The central qualification must be explicit: these are keyword ideas Google
associates with `competitor.com`. They are not queries for which the domain ranks,
and they are not evidence about its traffic, positions or Search Console data.

If Ads competition appears, describe advertiser demand. SEO difficulty remains
unknown without SERP evidence, which this tool does not collect.

## Never say

- Do not say "competitor.com ranks for these queries."
- Do not describe site-seed ideas as the site's organic keywords.
- Do not call Ads competition SEO difficulty.
- Do not infer traffic or rankings from keyword ideas or rounded volume.

