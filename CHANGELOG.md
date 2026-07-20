# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

##  [0.3.0] — 2026-07-20

- Adds client-side validation for `lang`/`not_lang` and `country`/`not_country` across all 5 tools that accept them (`search_articles`, `get_latest_headlines`, `search_by_author`, `list_sources`, `get_aggregation_count`). An unknown code now returns an immediate corrective error instead of a wasted API round-trip.

##  [0.2.0] — 2026-07-15

### Fixed
- `lint_query` no longer rejects valid grouped queries such as `(natural gas)
  AND (demand OR supply)`. The "unquoted multi-word phrase + OR/NOT" check
  now collapses balanced parenthesised groups and quoted phrases to
  placeholders before judging, so only ungrouped, top-level runs are flagged
  -- the flat `AI OR artificial intelligence` mistake is still caught. Grounded
  in an eval replaying 2,520 real production queries from 63 live customer
  keys; ~8-9% of them tripped this false positive, hitting power users hardest.
- `search_articles` no longer returns a 422 when a clustered search's date
  range straddles 2026-01-01 (the API cannot cluster across that boundary).
  The range is auto-split into two clustered requests -- one entirely before,
  one entirely on/after the boundary -- and merged into a single clustered
  response with a `date_range_split` note. Clustering stays on by default. A
  range ending exactly at 2026-01-01 is accepted as-is and not split, matching
  the API's own boundary check.

### Added
- Opt-in `fields` param on the article tools to trim each returned article
  (flat and nested under `clusters`) to just the requested keys -- the News
  API v3 response is ~40 fields per article and the `content` body alone can
  make a 30-article call exceed 340 KB / ~86K tokens. No-op unless passed;
  default output is unchanged.
- Regression tests for grouped-query acceptance, field projection, and the
  date-range straddle detector; updated the fail-early test and integration
  assertions to match (a straddling clustered range is now split, not
  rejected).

## [0.1.0] — 2026-07-14

### Added
- Initial release: FastMCP server for the NewsCatcher News API (v3), structurally
  mirroring an internal reference FastMCP server's auth mechanism, error handling
  convention, and test suite layout.
- Tools: `search_articles`, `get_latest_headlines`, `get_breaking_news`,
  `search_by_author`, `search_by_link`, `list_sources`, `get_aggregation_count`,
  `get_subscription`, `check_health`.
- API token auth with 5-level precedence (`api_token` tool param, `x-api-token`
  header, `Authorization: Bearer`, `?apiToken=` query param, `NEWS_API_KEY` env
  var) -- `x-api-token` is used identically at both the client-facing and
  upstream layer, matching News API v3's own header name.
- `validators.py`: enum/range validation for the fields News API v3 encodes as
  real schema constraints, `custom_tags` dotted-key wire-format translation, a
  conservative `q` syntax lint (including a check for the documented "unquoted
  multi-word phrase mixed with OR/NOT" 422 case), a per-term wildcard check, and
  a best-effort clustering/2026-01-01 date-range boundary check.
- `search_articles` defaults `clustering_enabled`, `exclude_duplicates`, and
  `include_nlp_data` to `true`; `get_latest_headlines`, `get_breaking_news`, and
  `search_by_author` default `include_nlp_data` to `true`. Pass `false` to opt out.
- `list_sources.source_url` accepts one or many domains for bulk coverage checks;
  the endpoint now requires at least one filter parameter, matching the documented
  constraint.
- Entity search (`COUNT("Entity", n, "gt")`) and multilingual/translation search
  guidance folded into the tool docstrings and server instructions.
- Unit test suite (mocked, no network) and an integration test suite (one file
  per tool, driven against a live server via the official `mcp` SDK client).

### Notes
- `iptc_tags`/`not_iptc_tags`/`iab_tags`/`not_iab_tags` are intentionally not
  exposed as tool parameters (not available via this MCP).
- No comma-string coercion for list-typed parameters: FastMCP validates tool-call
  arguments against each parameter's schema before the tool function runs, so a
  malformed string can never reach it -- an earlier draft's defensive coercion for
  this case was dead code and has been removed.
