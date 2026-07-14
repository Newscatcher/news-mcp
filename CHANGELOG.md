# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

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
