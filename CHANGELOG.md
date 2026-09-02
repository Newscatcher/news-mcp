# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

##  [0.4.0] — 2026-09-01

### Fixed
- `search_in`/`theme`/`not_theme`/`predefined_sources` are now sent as
  comma-joined strings instead of JSON arrays. News API v3 rejects a JSON array
  for these four multi-value filters over POST with a `499 "str type expected"`
  (`422` for `predefined_sources`), even though it accepts arrays for every
  other multi-value filter (`lang`, `countries`, `sources`, etc.) — confirmed
  against the live API. Found via a production report of Claude Desktop
  hitting `search_articles(search_in=["title"])`. The tool parameters
  themselves stay plain lists; only the outgoing request body changed.
- `get_breaking_news`'s `fields` param was a silent no-op: `_project_result`
  only recognized the `articles`/`clusters` response shapes, not
  `get_breaking_news`'s own `breaking_news_events[].articles` shape, so the
  full ~40-field article always passed through regardless of what `fields`
  requested.
- `fields` now validates against a real article schema
  (`validators.ARTICLE_FIELDS`/`NLP_SUBFIELDS`, enumerated live against the API
  with every enrichment flag on) instead of silently dropping unrecognized
  keys. News API v3's `_source` mechanism drops any unmatched path with no
  error, so a guessed field name (this tool's own docstring used to suggest a
  non-existent top-level `"summary"` field) used to produce a quietly
  incomplete result with no signal anything was wrong. Wired into all 5 tools
  that accept `fields`; an unrecognized value now returns an immediate
  corrective error (e.g. pointing to `"description"` or `"nlp.summary"`
  instead of `"summary"`).

### Changed
- `fields` now defaults to a lean, generally-useful set instead of the full
  ~40-field article object, on all 5 tools that accept it (`search_articles`,
  `get_latest_headlines`, `get_breaking_news`, `search_by_author`,
  `search_by_link`): `title`, `link`, `published_date`, `domain_url`, `author`,
  `language`, `nlp.summary`, `nlp.translation_summary`, `nlp.theme`
  (`validators.DEFAULT_ARTICLE_FIELDS`). This is a real behavior change for
  every caller that omits `fields` -- output is now trimmed by default rather
  than the full object. Pass `fields=[]` explicitly to opt back into full,
  untrimmed objects (e.g. the user asks what else is available).
- `include_translation_fields` now defaults to `true` on `search_articles`,
  `get_latest_headlines`, `get_breaking_news`, and `search_by_author` (previously
  unset/off) -- needed so `nlp.translation_summary` in the new default `fields`
  set actually populates for non-English articles. News API only populates one
  of `nlp.summary`/`nlp.translation_summary` per article; present it as
  whichever one is actually non-null. `search_by_link` has no such toggle and
  always leaves `nlp.translation_summary` empty.
- `_project_result` (the client-side trim `get_breaking_news` uses instead of
  the other four tools' server-side `_source`) now supports one level of dotted
  paths (e.g. `"nlp.summary"`), matching what `build_source` already supported
  -- needed for the new default's `nlp.*` fields to actually trim
  `get_breaking_news`'s output instead of silently dropping the whole `nlp`
  block (its response shape has no `_source` equivalent).

### Docs
- README now leads with the hosted deployment
  (`https://news-mcp.newscatcherapi.com/mcp`) instead of "no hosted deployment
  yet."
- Documented three natural-language-mapping traps found via live testing: the
  default `sort_by` is `"relevancy"` not `"date"` (a "most recent" request
  needs `sort_by="date"` explicitly or it can return results hours stale), a
  domain-level `sources` filter spans every language edition of a publisher,
  and a full-text `AND` match can be topically tangential to the query.
- `search_in`: clarified that most queries should leave it unset (the
  `title_content` default already covers standard recall), reserve `["title"]`
  for high-precision single-entity tracking, and use the `*_translated`
  variants (alone or alongside the default) for multilingual coverage under
  the same English-language query.
- `predefined_sources`: documented across all 5 tools that have it as the
  right parameter when a user wants "top"/"major"/"reputable" sources for a
  country, rather than manually enumerating domains.
- `exclude_duplicates`: documented when to override the default (`true`) to
  `false` — explicit user request to include duplicates, or an exhaustive
  mention count for a specific company/person, where deduplication would
  undercount genuinely separate mentions. Also confirmed and documented that
  it only has an effect on `search_articles` (silently ignored elsewhere,
  including where the API accepts it without erroring).
- Entity search: NER filters now explicitly called out as the right tool for
  company/person/location queries, complementing rather than replacing
  keyword `q` search.
- New "consider running multiple searches" guidance (README: "When One Search
  Isn't Enough"; server instructions similarly): when coverage genuinely
  matters, weigh NER vs. keyword, default vs. translated `search_in`,
  `predefined_sources`/`ranked_only` for major vs. niche coverage,
  `is_headline` filtered vs. unfiltered, and splitting across `theme`
  values — framed as judgment calls per query, not a fixed checklist.
- New README section documenting the `fields` output-projection parameter
  end-to-end (previously undocumented there).

### Tests
- Live integration run against the hosted deployment (43/43 passing) plus new
  regression coverage for comma-joined list-field serialization, `fields`
  validation (unit + integration), `get_breaking_news` fields projection, the
  new default `fields`/`include_translation_fields` values end-to-end per tool,
  the `fields=[]` escape hatch, and `_project_result`'s dotted-path support.

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
