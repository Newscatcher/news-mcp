# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.4.6] — 2026-09-03

### Added
- PostHog MCP Analytics. `posthog.mcp.instrument()` wraps the server, so every
  invocation lands as one `$mcp_tool_call` (tool name, parameters, response, duration,
  error flag) alongside `$mcp_initialize`, `$mcp_tools_list` and `$exception`. Events go
  to the PostHog project named by `POSTHOG_PROJECT_API_KEY`; `POSTHOG_HOST` picks the
  region (defaults to `https://us.i.posthog.com`). See README "Analytics (optional)".
  - **Off by default.** No key, no analytics, no behaviour change — and any failure
    while wiring it up is logged and swallowed rather than taking the server down.
  - **Observation only.** `context`, `enable_conversation_id`, and `report_missing` are
    pinned off in `MCPAnalyticsOptions`, since each would otherwise inject an argument
    or an extra tool into the schema callers see (`context` defaults to `True`
    upstream). `tools/list` is unchanged with analytics on or off.
  - `instrument()` runs before `mcp.http_app` is monkey-patched (for
    `ApiTokenASGIMiddleware`) and before `app = mcp.http_app()` builds the ASGI app — it
    wraps FastMCP's app factories to install PostHog's stateless-session middleware,
    which mints the `Mcp-Session-Id` token that stitches `$session_id` and identity
    together across requests. Verified both middlewares end up on the built app in the
    right order.
  - A small ASGI middleware (`_AnalyticsFlushMiddleware`) flushes the client at the end
    of each HTTP request, since PostHog's client batches on a background thread that a
    frozen/recycled server instance may never get to run again.
  - Callers are attributed pseudonymously by API token, since every tool but
    `check_health` requires one: `distinct_id` is an HMAC of the token
    (`auth: keyed`), optionally salted by `POSTHOG_IDENTITY_SALT`. The raw token is
    never sent. A call with no resolvable token stays anonymous rather than
    attributed. No client-IP fallback — this server is only ever called with a
    token, so there is no meaningful keyless caller to identify by IP.
  - Credentials are not captured: the SDK redacts any argument whose key looks like an
    api key or token before the event leaves the process.
  - **Not** wired via `npx @posthog/wizard mcp-analytics` — that wizard's OAuth
    login/codegen flow only targets TypeScript/JavaScript servers built on
    `@modelcontextprotocol/sdk`, and does nothing useful against this Python `fastmcp`
    server. The `posthog.mcp` module is the Python path instead; no login step, just a
    static Project API key.

### Changed
- Added `posthog==7.39.1` to `requirements.txt`.

## [0.4.5] — 2026-09-02

### Fixed
- `fields` had no `Args:` docstring entry on any of the 5 tools that accept it
  (`search_articles`, `get_latest_headlines`, `get_breaking_news`,
  `search_by_author`, `search_by_link`), so FastMCP generated no schema
  `description` for it at all -- confirmed by introspecting the live tool
  schema (`fields` came back as a bare `anyOf`/`default` with no
  `description` key, unlike every other parameter). An agent reading only
  the per-parameter schema, rather than the long prose "Key rules" section,
  saw `fields` with zero explanation. Found via a question about whether
  `fields=null` returns all fields.

### Docs
- Every `fields` entry (new Args docstrings, the shared server instructions,
  README) now states explicitly: `fields=[]` **or** `fields=null` (both
  behave identically -- `build_source`/`_project_result` treat `None` and
  `[]` the same, `if not fields`) return full, untrimmed objects, but
  **simply omitting `fields` from the call does not** -- omission applies
  the tool's own default, `DEFAULT_ARTICLE_FIELDS` (the lean 9-key set),
  the same as any other call. This distinction (explicit `null`/`[]` vs.
  omission) was previously undocumented and is easy to get backwards.

---

## [0.4.4] — 2026-09-02

### Changed
- `search_articles`/`get_latest_headlines`'s `page_size` default dropped from
  `100` to `50`. These are the only two tools where the default is now sized
  for the default *clustered* view (`clustering_enabled=true`,
  `cluster_top_n_articles=3`, both already the default) rather than a flat
  article dump -- a basic/broad request now pulls a smaller, grouped set by
  default. `get_breaking_news`, `search_by_author`, `search_by_link`, and
  `get_aggregation_count` are unaffected (still default `100`).

### Docs
- Documented a second calling pattern for `search_articles`/`get_latest_headlines`
  alongside the clustered default: when the user wants direct articles or cares
  about specific sources rather than grouped topic coverage (tracking one
  outlet, precise source-checking, "did X cover this"), pass
  `clustering_enabled=false` and `page_size=20` for a plain, ungrouped list.
  Flagged explicitly why this matters: with clustering on, the specific
  article/source the user is after could be hidden by the
  `cluster_top_n_articles=3` cap if it isn't among the top 3 in its cluster.
  Added to the shared server instructions, both tools' docstrings, and README.

### Tests
- Updated `ToolBehaviorTests.test_tool_request_mapping`'s expected request
  bodies for the `search_articles`/`get_latest_headlines` cases that omit
  `page_size` (6 of them) from `100` to `50` to match the new default.

---

## [0.4.3] — 2026-09-02

### Added
- `cluster_top_n_articles` on `search_articles`/`get_latest_headlines` (default
  `3`, pass `None` for no cap): caps how many articles are shown per cluster.
  Clustering reorganizes results, it doesn't shrink them -- News API groups
  every matched article into a cluster and drops none, so `page_size=100`
  grouped into 50 clusters is still ~100 full article objects on the wire. A
  heavily-syndicated story's cluster (dozens of near-identical writeups of one
  event) could previously dump all of them into the response by itself.
  `cluster_size` is left untouched by the trim, so the true per-cluster total
  is always visible even when only `top_n` articles are shown. This mirrors
  `get_breaking_news`'s existing `top_n_articles`, except News API v3 has no
  server-side equivalent for these two endpoints, so it's applied client-side
  (`server._trim_cluster_articles`) after the full clustered response comes
  back -- it does not reduce the upstream API call itself, only what's
  returned to the caller. Measured on representative payloads: ~18% smaller
  when clustering only mildly consolidates results, up to ~34% when one viral
  story dominates the page with a large cluster.

### Tests
- New validator tests for `validate_cluster_top_n_articles` (valid ints, `None`
  for no cap, rejects `<= 0`).
- New behavioral tests: the cap trims `clusters[].articles` while leaving
  `cluster_size` intact, `None` disables the cap, it's a no-op when
  `clustering_enabled=False` (nothing to trim), it's never sent in the
  upstream request body (client-side only), and it applies identically on
  `get_latest_headlines`.

---

## [0.4.2] — 2026-09-02

### Changed
- Dropped `indent=2` from `json.dumps` on the 7 tools whose output is
  consumed only by a model (`search_articles`, `get_latest_headlines`,
  `get_breaking_news`, `search_by_author`, `search_by_link`, `list_sources`,
  `get_aggregation_count`) — pretty-printing exists for a human reading raw
  output, and the newlines/indentation it adds are pure token overhead for an
  LLM consumer (~17% smaller payload for the same JSON content, no data
  change). `get_subscription` and `check_health` keep `indent=2` since those
  two are the ones a person is actually likely to read directly (quota
  checks, liveness pings).

### Tests
- `ToolBehaviorTests._assert_tool_call`'s shared assertion now expects
  `indent=2` only for `get_subscription`/`check_health` and no indent for
  every other tool, matching the change above.

---

## [0.4.1] — 2026-09-02

### Docs
- Corrected the documented API token precedence (module docstring, `FastMCP`
  `instructions` text, `get_api_token`'s docstring, README): `?apiToken=` URL
  query parameter actually outranks the `x-api-token`/`Authorization: Bearer`
  headers, not the reverse as previously stated everywhere except
  `_token_from_session`'s own docstring. Found via a team report of
  `?apiToken=BOGUS` alongside a valid `x-api-token` header returning `401`
  instead of using the header. Root cause: `ApiTokenASGIMiddleware`
  unconditionally captures `?apiToken=` into the `session_api_token`
  ContextVar on any request that carries it (and remembers it for the rest of
  that session once seen on `initialize`), and `_token_from_session()` reads
  that ContextVar before ever inspecting headers. Behavior unchanged — this is
  a documentation-only fix. Only affects direct server access; a FastMCP
  Gateway deployment never forwards `?apiToken=`, so the header is effectively
  top priority there regardless.

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
