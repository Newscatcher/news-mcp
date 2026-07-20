"""
MCP Server for the NewsCatcher News API (v3)

This server provides tools to search, monitor, and analyze global news
content via the NewsCatcher News API.

API token precedence (highest to lowest):
1. api_token tool parameter (explicit per-call)
2. x-api-token request header (recommended for hosted/gateway deployments)
3. Authorization: Bearer <token> request header
4. URL query parameter: ?apiToken=YOUR_TOKEN
5. NEWS_API_KEY environment variable
"""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastmcp import FastMCP
from fastmcp.server.http import _current_http_request
from starlette.middleware import Middleware as StarletteMiddleware

from validators import (
    AGGREGATION_BY_VALUES,
    CLUSTERING_CUTOFF_DATE,
    CLUSTERING_CUTOFF_PREV_DATE,
    CLUSTERING_VARIABLE_VALUES,
    NEWS_DOMAIN_TYPE_VALUES,
    SORT_BY_VALUES,
    clustering_straddles_cutoff,
    flatten_custom_tags,
    lint_query,
    validate_choice,
    validate_clustering_threshold,
    validate_country,
    validate_ids_or_links,
    validate_lang,
    validate_page_params,
    validate_rank,
    validate_search_in,
    validate_sentiment_range,
    validate_sources_has_filter,
    validate_sources_params,
    validate_top_n_articles_page_size,
)

# Context variable to store the API token for the current request
session_api_token: contextvars.ContextVar[str] = contextvars.ContextVar("session_api_token", default="")

# Session-level storage: mcp-session-id -> api_token
# Persists the API token across the full lifecycle of an MCP session.
_session_api_tokens: dict[str, str] = {}

# API Configuration
API_BASE_URL = os.getenv("NEWS_API_BASE_URL") or "https://v3-api.newscatcherapi.com"


class ApiTokenASGIMiddleware:
    """ASGI middleware to extract and persist the API token across MCP sessions.

    With Streamable HTTP transport, clients include ?apiToken=TOKEN only on the
    initial `initialize` request. Subsequent tool call requests use an
    `mcp-session-id` header instead. This middleware:

    1. On the initialize request: captures ?apiToken=KEY and intercepts the
       response to store the key mapped to the assigned mcp-session-id.
    2. On all subsequent requests: looks up the stored key by mcp-session-id
       and sets the session_api_token context variable for the current request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract ?apiToken= from query string
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        api_token = params.get("apiToken", [""])[0]

        # Extract mcp-session-id from request headers
        headers_dict = {k.lower(): v for k, v in scope.get("headers", [])}
        session_id = headers_dict.get(b"mcp-session-id", b"").decode("utf-8")

        # Restore token from session storage if available
        if session_id and session_id in _session_api_tokens:
            effective_token = _session_api_tokens[session_id]
        else:
            effective_token = api_token

        if effective_token:
            session_api_token.set(effective_token)

        if api_token and not session_id:
            # This is the initialize request — intercept the response to capture
            # the server-assigned mcp-session-id and store the token mapping.
            async def send_with_session_capture(message: Any) -> None:
                if message["type"] == "http.response.start":
                    resp_headers = {k.lower(): v for k, v in message.get("headers", [])}
                    new_session_id = resp_headers.get(b"mcp-session-id", b"").decode("utf-8")
                    if new_session_id:
                        _session_api_tokens[new_session_id] = api_token
                await send(message)

            await self.app(scope, receive, send_with_session_capture)
        else:
            await self.app(scope, receive, send)


# Create the FastMCP server
mcp = FastMCP(
    "Newscatcher News API",
    instructions="""This server allows you to search, monitor, and analyze global news content via the NewsCatcher News API (v3).

IMPORTANT: Every tool except check_health requires a News API token. Get one at https://www.newscatcherapi.com/
check_health never calls the upstream API and never needs a token.

## Authentication
API token is resolved in this order (first match wins):
1. `api_token` tool parameter — pass it directly in any tool call.
2. `x-api-token` HTTP header — set once in your MCP client config (recommended for hosted deployments).
3. `Authorization: Bearer <token>` HTTP header — alternative header-based auth.
4. `?apiToken=YOUR_TOKEN` URL query parameter — works only for direct server access (not forwarded by the FastMCP Gateway).
5. `NEWS_API_KEY` environment variable — set on the server host.
If no token is found, tools return `Error: API token is required.`

## Tool selection policy
- `search_articles` — full-text/boolean keyword search with the richest filter set. Use for any query-driven investigation.
- `get_latest_headlines` — recent headlines over a rolling window (`when`), no keyword required. Use for "what's new in X" without a specific query.
- `get_breaking_news` — actively-trending event clusters over a fixed internal ~24h window, no filters beyond ranking/pagination. Use for "what's the big story right now" style requests.
- `search_by_author` — all articles by one exact byline.
- `search_by_link` — look up specific already-known articles by NewsCatcher `id` or URL. Not a search tool.
- `list_sources` — browse/verify publishers, not articles.
- `get_aggregation_count` — time-bucketed article-volume counts for a query, no articles returned. Always call this before a `search_articles` pull that might exceed 10,000 results, to decide whether you need to time-chunk the date range.
- `get_subscription` — plan/quota introspection. Use to check remaining calls or to sanity-check that a key is valid.
- `check_health` — local liveness ping only (does not call the News API). Use as a zero-setup first call.
- There is no "find articles similar to this one" tool. Reach similarity via `clustering_enabled`+`clustering_threshold` (on `search_articles`/`get_latest_headlines`), `exclude_duplicates` (search only), or by requesting `include_nlp_data=true` and comparing the returned embeddings yourself.

## Defaults for simple queries — clustering, deduplication, and NLP
`search_articles`, `get_latest_headlines`, `get_breaking_news`, and `search_by_author` default
`include_nlp_data` to `true` (adds theme/sentiment/NER/summary to each article). `search_articles`
additionally defaults `clustering_enabled` and `exclude_duplicates` to `true`. Pass `false` explicitly
on any of these to opt out.
- Enabling `clustering_enabled` changes the response shape: you get `clusters_count` + `clusters`
  (each `{cluster_id, cluster_size, articles}`) instead of a flat `articles` list. Pass
  `clustering_enabled=false` if you just want a plain article list.
- Clustering and `exclude_duplicates` address the same underlying problem (near-duplicate coverage)
  differently, rather than being strictly complementary: clustering groups related articles (all
  articles kept, reorganized into groups), `exclude_duplicates` removes near-identical ones (fewer
  articles, stays flat, adds `duplicate_count`/`duplicate_articles_group_id` per result).
- `clustering_threshold` (default 0.7, range (0, 1]) controls cluster tightness. Clustering operates
  one page at a time, so raise `page_size` to at least your expected result count for coherent clusters.
- `clustering_variable` is deprecated (ignored) for articles published on/after 2026-01-01 — since
  results default to the last 7 days, this is already inert for most default-range calls today.
  The API cannot cluster across a date range that straddles 2026-01-01; when that happens
  `search_articles` splits the range at the boundary, clusters each half, and merges the result.

## Pagination and the 10,000-result cap
- `page_size` maxes out at 1000; there is a hard cap of 10,000 articles per query regardless of pagination.
- If `total_hits` reads exactly 10000, the true match count is likely higher — narrow the query or time-chunk the date range rather than trusting that number.
- Call `get_aggregation_count` first on any broad/undated query to gauge volume before paginating a `search_articles` call.
- Server-side request timeout is 30s; a `408` means: narrow the query, shorten the date range, or reduce `page_size`.

## Historical data and date ranges
- Data is indexed monthly and goes back to 2019; NLP enrichment is only available for articles indexed
  from July 2023 onward (earlier articles still return an `nlp` object, just empty `{}` — use
  `has_nlp=true` to filter to NLP-enriched articles only).
- Cross-month queries hit multiple indexes and get slower the wider the range — don't query multiple
  years in a single call. Instead: (1) call `get_aggregation_count` with `aggregation_by="day"` over
  your target range to measure actual volume/distribution, (2) pick a chunk size from the measured
  density below, (3) page fully through each chunk with `search_articles`/`get_latest_headlines` before
  moving to the next one. Chunk size by measured articles-per-period (a rough guide, not a hard rule —
  the goal is keeping each chunk well under the 10,000-per-request cap):
  - more than 10,000/hour → `"1h"` chunks, and consider narrowing the query
  - more than 10,000/day → `"6h"` or `"1h"`
  - 3,000-10,000/day → `"1d"`
  - 1,000-3,000/day → `"3d"`
  - 100-1,000/day → `"7d"`
  - fewer than 100/day → `"30d"`
  If a chunk still times out (`408`), step the size down (e.g. `"1d"` → `"6h"` → `"1h"`).
- Use a fixed `to_` date (not the default "now") for reproducible historical pulls — an open-ended
  `to_` means results shift between runs as new articles get indexed.
- On `429` (rate limited), back off and retry rather than repeating the same request immediately —
  check `get_subscription` for your concurrency/quota limits.

## Query syntax (`q` parameter — search_articles, get_aggregation_count)
The API auto-inserts `AND` between bare, unquoted, space-separated words — this is the single most
common source of broken queries, so read this before constructing a non-trivial `q`.
- **Always quote multi-word phrases.** `q="AI OR artificial intelligence"` is actually parsed as
  `"AI OR artificial AND intelligence"` (an implicit AND collides with your explicit OR at the same
  level, with no grouping) — the API rejects this with a `422`. Fix: quote the phrase,
  `q='AI OR "artificial intelligence"'`, or add explicit parentheses around each side. This server's
  validation catches the common flat form of this mistake before it reaches the API, but the safe habit
  is to always quote multi-word terms regardless of whether another operator is present.
- Exact phrase: wrap it in literal double-quote characters inside the `q` value, e.g. `q='"Tim Cook"'`
  (a string that starts and ends with a quote character) matches the exact phrase. Without quotes,
  `q='Tim Cook'` is equivalent to `q='Tim AND Cook'`.
- Boolean: `AND`, `OR`, `NOT`, with parentheses to control evaluation order, e.g.
  `(bitcoin OR cryptocurrency) AND (investment OR trading)`.
- Prefix shorthand: `+term` to require, `-term` to exclude.
- Wildcards: `*` (any-length) and `?` (single character) — neither can lead a term (`"*intelligence"`
  is invalid; `"technolog*"` is fine). `q="*"` alone is valid and matches all articles (useful when you
  only want to filter by other params with no keyword component).
- Proximity: `NEAR("phrase one", "phrase two", distance, in_order)` — max 4 words per phrase, max 100
  words distance, `in_order` optional (default false).
- Forbidden characters, never valid anywhere in `q`: `[ ] / \\ : ^` (and their URL-encoded equivalents).
- If results look wrong, check the response's `user_input.q` to see how the API actually parsed your
  query, then refine: too broad → add AND terms, narrow with NOT, or reduce NEAR distance; too few →
  broaden with OR, increase NEAR distance, or use a wildcard.

## Entity search and multilingual coverage
`org_entity_name`/`per_entity_name`/`loc_entity_name`/`misc_entity_name` (and `ner_name` on
search_by_author) support the same AND/OR/NOT/NEAR syntax as `q`, plus one more operator:
`COUNT("Entity Name", n, "gt")` filters to articles mentioning that entity more than n times — a proxy
for how central the entity is, not just a passing mention. Combine with `include_nlp_data=true` (the
default) to see actual mention counts in `nlp.ner_*`.

News API translates non-English articles to English at index time (translation fields available for
articles published from 2025-03-12 onward), so entity names and keywords work across languages using
their English form even when the underlying article is in another language:
- Set `search_in=["title_content", "title_content_translated"]` to search both original and translated
  text in one call.
- Omit `lang` to search across all languages; use `countries` to focus on specific regions instead.
- Use official English names in quotes, e.g. `org_entity_name='"European Union" OR "European Commission"'`
  also matches "Union européenne"/"Unión Europea" in French/Spanish articles.
- Set `include_translation_fields=true` to get `title_translated_en`/`content_translated_en` and
  `nlp.translation_summary`/`nlp.translation_ner_*` back on each result — the `translation_ner_*` fields
  need `include_nlp_data=true` too (on by default) to appear.""",
)


def _token_from_session() -> str:
    """Look up the API token for the current MCP session.

    Checks request sources in order:
    1. session_api_token ContextVar (set by ASGI middleware in the current task).
    2. _current_http_request ContextVar — inspects the HTTP request for:
       a. ?apiToken= query param (direct server access, no gateway)
       b. x-api-token header (gateway deployment — header is forwarded by FastMCP Gateway)
       c. Authorization: Bearer <token> header (gateway deployment, alternative)
       d. _session_api_tokens lookup by mcp-session-id (stateful session fallback)
    """
    url_token = session_api_token.get("")
    if url_token:
        return url_token

    try:
        request = _current_http_request.get()
        if request is not None:
            # ?apiToken= query param — works for direct server access (no gateway)
            direct_token = request.query_params.get("apiToken", "")
            if direct_token:
                return direct_token

            # x-api-token header — works through FastMCP Gateway (headers are forwarded)
            header_token = request.headers.get("x-api-token", "")
            if header_token:
                return header_token

            # Authorization: Bearer <token> — alternative header-based auth
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                bearer_token = auth_header[7:].strip()
                if bearer_token:
                    return bearer_token

            # Session-ID lookup — stateful sessions only (no gateway)
            session_id = request.headers.get("mcp-session-id", "")
            if session_id:
                return _session_api_tokens.get(session_id, "")
    except Exception:
        pass

    return ""


def get_api_token(api_token: str = "") -> str:
    """Get the API token from parameter, HTTP headers, URL session, or environment variable.

    Priority order:
    1. api_token parameter (explicit in tool call)
    2. x-api-token header or Authorization: Bearer header (via _token_from_session)
    3. ?apiToken= URL query parameter (direct server access only)
    4. NEWS_API_KEY environment variable

    Every News API v3 endpoint requires auth -- there is no public no-auth
    endpoint, so there is no "optional" variant of this function --
    check_health simply never calls it at all.
    """
    if api_token:
        return api_token

    session_token = _token_from_session()
    if session_token:
        return session_token

    env_token = os.environ.get("NEWS_API_KEY", "")
    if env_token:
        return env_token

    raise ValueError(
        "API token is required. Provide it via one of: "
        "1) api_token as a parameter in each HTTP request, "
        "2) x-api-token HTTP header (recommended for hosted deployments), "
        "3) Authorization: Bearer <token> HTTP header, "
        "4) ?apiToken=YOUR_TOKEN URL parameter, "
        "5) NEWS_API_KEY environment variable."
    )


async def make_api_request(api_token: str, path: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST a JSON request to News API v3 and return the decoded JSON body.

    News API v3 supports GET and POST identically on every endpoint; this server
    always uses POST -- NewsCatcher's own docs recommend POST for production (no
    URL-length limit, keeps keys/queries out of access logs, and lets multi-value
    filters be sent as native JSON arrays instead of comma-joined strings).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-token": get_api_token(api_token),
    }

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0) as client:
        response = await client.post(path, headers=headers, json=json_data)

        if response.status_code >= 400:
            # News API's error envelope is {"message", "status_code", "status"}.
            # Its 500 responses come back as text/plain rather than JSON, which
            # the except below already falls back to response.text for, with
            # no special-casing needed.
            try:
                error_data = response.json()
                if isinstance(error_data, dict) and "message" in error_data:
                    error_msg = error_data["message"]
                else:
                    error_msg = response.text or f"HTTP {response.status_code}"
            except Exception:
                error_msg = response.text or f"HTTP {response.status_code}"

            raise ValueError(f"API Error ({response.status_code}): {error_msg}")

        try:
            return response.json()
        except json.JSONDecodeError:
            if response.text and response.text.strip():
                raise ValueError(f"API returned non-JSON response: {response.text[:500]}")
            return {}


def _add_field(body: dict[str, Any], key: str, value: Any) -> None:
    """Set body[key] = value if value was actually provided (not None).

    False/0/"" are meaningful, distinct values for several params here (e.g.
    ranked_only=False, is_headline=False), so the check is `is not None`, not
    plain truthiness.
    """
    if value is not None:
        body[key] = value


def _add_list_field(body: dict[str, Any], key: str, value: list[str] | None) -> None:
    """Set body[key] = value if a non-empty list was provided.

    FastMCP validates incoming tool-call arguments against each parameter's
    JSON schema before this function ever runs, so `value` is guaranteed to
    already be a real list or None -- no comma-string coercion is needed here.
    """
    if value:
        body[key] = value


def _project_result(result: Any, fields: list[str] | None) -> Any:
    """Opt-in output projection. When `fields` is provided, trim every returned
    article -- both a top-level `articles` list and articles nested under
    `clusters` -- to just those top-level keys. No-op when `fields` is None.

    News API v3 returns ~40 fields per article (plus large `all_links` /
    `all_domain_links` / `nlp` structures), which can blow an agent's context on a
    single call. This lets a caller ask for only what it needs (e.g.
    fields=["title","link","published_date","domain_url","summary"]) without
    changing behaviour for callers that don't pass it.
    """
    if not fields or not isinstance(result, dict):
        return result
    keep = set(fields)

    def proj(a: Any) -> Any:
        return {k: v for k, v in a.items() if k in keep} if isinstance(a, dict) else a

    if isinstance(result.get("articles"), list):
        result["articles"] = [proj(a) for a in result["articles"]]
    if isinstance(result.get("clusters"), list):
        for c in result["clusters"]:
            if isinstance(c, dict) and isinstance(c.get("articles"), list):
                c["articles"] = [proj(a) for a in c["articles"]]
    return result


def build_source(fields: list[str] | None, clustered: bool) -> str | None:
    """Turn a caller's list of article field names into the News API `_source`
    value: a comma-separated string of dotted paths that trims the response
    SERVER-SIDE (so the ~40 fields/article, incl. the large `content` body, never
    cross the wire). Returns None when fields is None (no projection).

    The path prefix depends on the response shape, which the API is strict about:
    a flat result nests articles under `articles.*`; a clustered result nests them
    under `clusters.articles.*` (and using the wrong prefix silently drops the whole
    articles/clusters payload). A few structural top-level keys are always kept.
    """
    if not fields:
        return None
    if clustered:
        top = ["total_hits", "page", "page_size", "clusters_count",
               "clusters.cluster_id", "clusters.cluster_size"]
        prefix = "clusters.articles."
    else:
        top = ["total_hits", "page", "page_size", "total_pages"]
        prefix = "articles."
    return ",".join(top + [prefix + f for f in fields])


async def _search_clustered_across_cutoff(api_token: str, body: dict[str, Any]) -> dict[str, Any]:
    """Clustering can't span the 2026-01-01 boundary (the API 422s). When a clustered
    search's date range straddles it, run the two halves -- each entirely on one side,
    so each is a legal clustered request -- and merge into one clustered response.
    This follows the API's own rule ("keep the range entirely before or on/after
    2026-01-01") while preserving clustering for the caller's full range.
    """
    before = {**body, "to_": CLUSTERING_CUTOFF_PREV_DATE}   # [from_ .. 2025-12-31]
    after = {**body, "from_": CLUSTERING_CUTOFF_DATE}        # [2026-01-01 .. to_]
    ra = await make_api_request(api_token=api_token, path="/api/search", json_data=before)
    rb = await make_api_request(api_token=api_token, path="/api/search", json_data=after)
    return {
        "status": "ok",
        "total_hits": (ra.get("total_hits") or 0) + (rb.get("total_hits") or 0),
        "clusters_count": (ra.get("clusters_count") or 0) + (rb.get("clusters_count") or 0),
        "clusters": (ra.get("clusters") or []) + (rb.get("clusters") or []),
        "page": body.get("page", 1),
        "page_size": body.get("page_size"),
        "date_range_split": {
            "reason": "clustering cannot span 2026-01-01; range was split at the boundary and results merged",
            "before": {"from_": body.get("from_"), "to_": CLUSTERING_CUTOFF_PREV_DATE,
                       "clusters_count": ra.get("clusters_count"), "total_hits": ra.get("total_hits")},
            "after": {"from_": CLUSTERING_CUTOFF_DATE, "to_": body.get("to_"),
                      "clusters_count": rb.get("clusters_count"), "total_hits": rb.get("total_hits")},
        },
    }


# --- Tools -------------------------------------------------------------------


@mcp.tool()
async def search_articles(
    q: str,
    api_token: str = "",
    search_in: list[str] | None = None,
    include_translation_fields: bool | None = None,
    predefined_sources: list[str] | None = None,
    source_name: str | None = None,
    sources: list[str] | None = None,
    not_sources: list[str] | None = None,
    lang: list[str] | None = None,
    not_lang: list[str] | None = None,
    countries: list[str] | None = None,
    not_countries: list[str] | None = None,
    not_author_name: str | None = None,
    from_: str | None = None,
    to_: str | None = None,
    published_date_precision: str | None = None,
    by_parse_date: bool | None = None,
    sort_by: str | None = None,
    ranked_only: bool | None = None,
    from_rank: int | None = None,
    to_rank: int | None = None,
    is_headline: bool | None = None,
    is_opinion: bool | None = None,
    is_paid_content: bool | None = None,
    parent_url: str | None = None,
    all_links: list[str] | None = None,
    all_domain_links: list[str] | None = None,
    all_links_text: list[str] | None = None,
    additional_domain_info: bool | None = None,
    is_news_domain: bool | None = None,
    news_domain_type: str | None = None,
    news_type: list[str] | None = None,
    word_count_min: int | None = None,
    word_count_max: int | None = None,
    page: int = 1,
    page_size: int = 100,
    clustering_enabled: bool | None = True,
    clustering_variable: str | None = None,
    clustering_threshold: float | None = None,
    include_nlp_data: bool | None = True,
    has_nlp: bool | None = None,
    theme: list[str] | None = None,
    not_theme: list[str] | None = None,
    org_entity_name: str | None = None,
    per_entity_name: str | None = None,
    loc_entity_name: str | None = None,
    misc_entity_name: str | None = None,
    title_sentiment_min: float | None = None,
    title_sentiment_max: float | None = None,
    content_sentiment_min: float | None = None,
    content_sentiment_max: float | None = None,
    custom_tags: dict[str, list[str]] | None = None,
    exclude_duplicates: bool | None = True,
    robots_compliant: bool | None = None,
    fields: list[str] | None = None,
) -> str:
    """
    Full-text/boolean keyword search over global news articles. The richest tool
    in this server -- use it for any query-driven investigation.

    Use when: you have a keyword, phrase, or boolean query to search for.
    Do not use when: you have no query at all (use get_latest_headlines or
    get_breaking_news instead), or you already know the specific article's
    id/URL (use search_by_link instead).

    Key rules:
    - clustering_enabled, exclude_duplicates, and include_nlp_data all default to
      true for richer, deduplicated, NLP-enriched results -- pass false to opt out
      of any of them. clustering_enabled changes the response shape (see Returns).
    - fields: pass a list of article keys (e.g. ["title","link","published_date",
      "domain_url","summary","nlp"]) to trim each returned article to just those --
      News API v3 returns ~40 fields per article (the biggest being the large
      all_links/all_domain_links/all_links_text arrays), so this keeps large,
      enriched result sets within an agent's context budget. Omit for full objects.
    - Hard cap: 10,000 matched articles per query regardless of pagination. Call
      get_aggregation_count first on broad/undated queries to measure actual volume
      and time-chunk the date range accordingly (denser topics need hourly chunks,
      sparse ones can use a month -- see this server's instructions for the sizing
      table and the full clustering/deduplication guidance).
    - If a clustered search's date range straddles 2026-01-01 (which the API cannot
      cluster across), this tool automatically splits it at the boundary, runs each
      half clustered, and merges the result (adds a `date_range_split` note). You do
      not need to split the range yourself.

    Args:
        q: Boolean/keyword query. Supports AND/OR/NOT, +term/-term, exact phrases
            in escaped quotes, wildcards (* and ?, cannot lead a term), and
            NEAR("a","b",distance,in_order). CRITICAL: always quote multi-word
            phrases -- unquoted, the API inserts AND between bare words, so
            q='AI OR artificial intelligence' is parsed as
            q='AI OR artificial AND intelligence' and gets a 422; write
            q='AI OR "artificial intelligence"' instead. Forbidden characters:
            [ ] / \\ : ^. "*" alone matches all articles (useful for filter-only
            queries). api_token: News API token.
            Optional if provided via x-api-token header or NEWS_API_KEY env var.
        search_in: Which fields to search q in. Max 2 of: title, content, summary,
            title_content (default), title_translated, content_translated,
            summary_translated, title_content_translated. For multilingual coverage,
            pass ["title_content", "title_content_translated"] to search original and
            translated text together (see this server's instructions).
        include_translation_fields: Add title_translated_en/content_translated_en and
            nlp.translation_summary/translation_ner_* to results (needs include_nlp_data
            too for the nlp.* fields). See this server's instructions for multilingual tips.
        predefined_sources: Top-N sources per country, e.g. ["top 50 US", "top 20 GB"].
        source_name: Fuzzy match on publisher display name (comma-separated string).
        sources: Include only these domains/subdomains.
        not_sources: Exclude these domains/subdomains.
        lang: Include only these ISO 639-1 language codes.
        not_lang: Exclude these ISO 639-1 language codes.
        countries: Include only these ISO 3166-1 alpha-2 publisher countries.
        not_countries: Exclude these ISO 3166-1 alpha-2 publisher countries.
        not_author_name: Exclude these author names (comma-separated string).
        from_: Start of date range. ISO 8601/"YYYY-MM-DD[ HH:MM:SS]" or natural
            language ("7 days ago"). Defaults to "7 days ago" if omitted. For
            historical pulls, chunk wide ranges into ~30-day windows or less.
        to_: End of date range, same formats. Defaults to "now" if omitted.
        published_date_precision: Filter by date precision: "full", "timezone
            unknown", or "date" (documented values, not a hard enum).
        by_parse_date: Filter/sort by NewsCatcher's parse date instead of the
            publish date; also populates `parse_date` on results.
        sort_by: "relevancy" (default), "date", or "rank".
        ranked_only: If False, include unranked sources (rank 999999). Defaults
            to True if omitted.
        from_rank: Minimum source popularity rank (1-999999, lower = more popular).
        to_rank: Maximum source popularity rank (1-999999).
        is_headline: Filter to (True) or exclude (False) homepage-featured articles.
        is_opinion: Filter to (True) or exclude (False) opinion pieces.
        is_paid_content: False = only fully public text; True = include
            paywalled/partial articles.
        parent_url: Filter by categorical/section URL(s) (comma-separated string).
        all_links: Filter to articles mentioning these full URLs.
        all_domain_links: Filter to articles mentioning these domains.
        all_links_text: Filter to articles with link anchor text containing these
            terms; populates `all_links_data` on results.
        additional_domain_info: Add is_news_domain/news_domain_type/news_type to
            each result's source info.
        is_news_domain: Filter to (True) or exclude (False) recognized news domains.
        news_domain_type: "Original Content", "Aggregator", "Press Releases",
            "Republisher", or "Other".
        news_type: Open-vocabulary content-type filter (e.g. "Tech News and
            Updates", "Sports News and Blogs") -- see the docs' Enumerated
            Parameters page for the full documented list.
        word_count_min: Minimum article word count.
        word_count_max: Maximum article word count.
        page: Page number, 1-indexed. Defaults to 1.
        page_size: Results per page, max 1000. Defaults to 100. Clustering
            operates one page at a time -- raise this to your expected result
            count for coherent clusters.
        clustering_enabled: Group results into near-duplicate/related clusters.
            Defaults to True -- the response then has `clusters`/`clusters_count`
            instead of a flat `articles` list (see Returns). Pass False for a
            plain article list.
        clustering_variable: "content" (default), "title", or "summary" -- ignored
            (deprecated) for articles published on/after 2026-01-01.
        clustering_threshold: Similarity threshold in (0, 1], default 0.7. Higher
            = tighter/smaller clusters.
        include_nlp_data: Populate each article's `nlp` block (theme, sentiment,
            NER, summary, embeddings). Defaults to True.
        has_nlp: Filter to only articles that have NLP data (indexed July 2023+).
        theme: Filter to these themes (e.g. "Business", "Tech", "Politics") --
            open vocabulary, not a hard enum.
        not_theme: Exclude these themes.
        org_entity_name: Filter by organization name mentioned in the article.
            Sent upstream as ORG_entity_name.
        per_entity_name: Filter by person name mentioned. Sent upstream as
            PER_entity_name.
        loc_entity_name: Filter by location name mentioned. Sent upstream as
            LOC_entity_name.
        misc_entity_name: Filter by miscellaneous entity name mentioned. Sent
            upstream as MISC_entity_name. All four support AND/OR/NOT/NEAR/COUNT(...)
            syntax -- see this server's instructions for entity search + multilingual
            search guidance.
        title_sentiment_min: Minimum title sentiment score, -1.0 to 1.0.
        title_sentiment_max: Maximum title sentiment score, -1.0 to 1.0.
        content_sentiment_min: Minimum content sentiment score, -1.0 to 1.0.
        content_sentiment_max: Maximum content sentiment score, -1.0 to 1.0.
        custom_tags: Organization-specific tag filter, e.g. {"my_taxonomy":
            ["Tag1", "Tag2"]}. Only usable if your key has custom tags configured.
        exclude_duplicates: Suppress near-identical articles (adds duplicate_count/
            duplicate_articles_group_id instead of clustering them). Defaults to
            True; addresses the same near-duplicate-coverage problem as
            clustering_enabled in a different way (see this server's instructions).
        robots_compliant: Filter by whether the source's robots.txt permits scraping.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`, and
        (by default, since clustering_enabled defaults to True) `clusters_count` +
        `clusters` (each `{cluster_id, cluster_size, articles}`) -- pass
        clustering_enabled=False for a flat `articles` list instead. Also includes
        `user_input` echoing back the parameters actually applied.

    Common API errors:
        - 401: missing or invalid API token.
        - 403: a requested field/filter isn't included in your plan.
        - 422: invalid query syntax or an invalid parameter combination.
        - 408: query too broad/slow -- narrow the query, date range, or page_size.
        - 429: rate limited -- check get_subscription for your concurrency limit.
    """
    try:
        lint_query(q)
        validate_search_in(search_in)
        validate_choice(sort_by, SORT_BY_VALUES, "sort_by")
        validate_choice(news_domain_type, NEWS_DOMAIN_TYPE_VALUES, "news_domain_type")
        validate_choice(clustering_variable, CLUSTERING_VARIABLE_VALUES, "clustering_variable")
        validate_page_params(page, page_size)
        validate_rank(from_rank, "from_rank")
        validate_rank(to_rank, "to_rank")
        validate_clustering_threshold(clustering_threshold)
        validate_sentiment_range(title_sentiment_min, "title_sentiment_min")
        validate_sentiment_range(title_sentiment_max, "title_sentiment_max")
        validate_sentiment_range(content_sentiment_min, "content_sentiment_min")
        validate_sentiment_range(content_sentiment_max, "content_sentiment_max")
        validate_lang(lang, "lang")
        validate_lang(not_lang, "not_lang")
        validate_country(countries, "countries")
        validate_country(not_countries, "not_countries")
        # NB: a clustered range straddling 2026-01-01 is handled below by splitting at
        # the boundary (not rejected), so we intentionally do NOT pre-raise here.

        body: dict[str, Any] = {"q": q, "page": page, "page_size": page_size}
        _add_list_field(body, "search_in", search_in)
        _add_field(body, "include_translation_fields", include_translation_fields)
        _add_list_field(body, "predefined_sources", predefined_sources)
        _add_field(body, "source_name", source_name)
        _add_list_field(body, "sources", sources)
        _add_list_field(body, "not_sources", not_sources)
        _add_list_field(body, "lang", lang)
        _add_list_field(body, "not_lang", not_lang)
        _add_list_field(body, "countries", countries)
        _add_list_field(body, "not_countries", not_countries)
        _add_field(body, "not_author_name", not_author_name)
        _add_field(body, "from_", from_)
        _add_field(body, "to_", to_)
        _add_field(body, "published_date_precision", published_date_precision)
        _add_field(body, "by_parse_date", by_parse_date)
        _add_field(body, "sort_by", sort_by)
        _add_field(body, "ranked_only", ranked_only)
        _add_field(body, "from_rank", from_rank)
        _add_field(body, "to_rank", to_rank)
        _add_field(body, "is_headline", is_headline)
        _add_field(body, "is_opinion", is_opinion)
        _add_field(body, "is_paid_content", is_paid_content)
        _add_field(body, "parent_url", parent_url)
        _add_list_field(body, "all_links", all_links)
        _add_list_field(body, "all_domain_links", all_domain_links)
        _add_list_field(body, "all_links_text", all_links_text)
        _add_field(body, "additional_domain_info", additional_domain_info)
        _add_field(body, "is_news_domain", is_news_domain)
        _add_field(body, "news_domain_type", news_domain_type)
        _add_list_field(body, "news_type", news_type)
        _add_field(body, "word_count_min", word_count_min)
        _add_field(body, "word_count_max", word_count_max)
        _add_field(body, "clustering_enabled", clustering_enabled)
        _add_field(body, "clustering_variable", clustering_variable)
        _add_field(body, "clustering_threshold", clustering_threshold)
        _add_field(body, "include_nlp_data", include_nlp_data)
        _add_field(body, "has_nlp", has_nlp)
        _add_list_field(body, "theme", theme)
        _add_list_field(body, "not_theme", not_theme)
        _add_field(body, "ORG_entity_name", org_entity_name)
        _add_field(body, "PER_entity_name", per_entity_name)
        _add_field(body, "LOC_entity_name", loc_entity_name)
        _add_field(body, "MISC_entity_name", misc_entity_name)
        _add_field(body, "title_sentiment_min", title_sentiment_min)
        _add_field(body, "title_sentiment_max", title_sentiment_max)
        _add_field(body, "content_sentiment_min", content_sentiment_min)
        _add_field(body, "content_sentiment_max", content_sentiment_max)
        body.update(flatten_custom_tags(custom_tags))
        _add_field(body, "exclude_duplicates", exclude_duplicates)
        _add_field(body, "robots_compliant", robots_compliant)

        clustered = body.get("clustering_enabled") is True
        source = build_source(fields, clustered)
        if source is not None:
            body["_source"] = source  # server-side field trim; inherited by both split halves
        if clustered and clustering_straddles_cutoff(from_, to_):
            result = await _search_clustered_across_cutoff(api_token, body)
        else:
            result = await make_api_request(api_token=api_token, path="/api/search", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_latest_headlines(
    api_token: str = "",
    when: str | None = None,
    by_parse_date: bool | None = None,
    sort_by: str | None = None,
    lang: list[str] | None = None,
    not_lang: list[str] | None = None,
    countries: list[str] | None = None,
    not_countries: list[str] | None = None,
    predefined_sources: list[str] | None = None,
    sources: list[str] | None = None,
    not_sources: list[str] | None = None,
    not_author_name: str | None = None,
    ranked_only: bool | None = None,
    is_headline: bool | None = None,
    is_opinion: bool | None = None,
    is_paid_content: bool | None = None,
    parent_url: str | None = None,
    all_links: list[str] | None = None,
    all_domain_links: list[str] | None = None,
    all_links_text: list[str] | None = None,
    word_count_min: int | None = None,
    word_count_max: int | None = None,
    page: int = 1,
    page_size: int = 100,
    clustering_enabled: bool | None = True,
    clustering_variable: str | None = None,
    clustering_threshold: float | None = None,
    include_translation_fields: bool | None = None,
    include_nlp_data: bool | None = True,
    has_nlp: bool | None = None,
    theme: list[str] | None = None,
    not_theme: list[str] | None = None,
    org_entity_name: str | None = None,
    per_entity_name: str | None = None,
    loc_entity_name: str | None = None,
    misc_entity_name: str | None = None,
    title_sentiment_min: float | None = None,
    title_sentiment_max: float | None = None,
    content_sentiment_min: float | None = None,
    content_sentiment_max: float | None = None,
    custom_tags: dict[str, list[str]] | None = None,
    robots_compliant: bool | None = None,
    fields: list[str] | None = None,
) -> str:
    """
    Recent headlines over a rolling time window -- no keyword query required.

    Use when: you want "what's new" for a set of filters (language, country,
    sources, theme, entities, ...) without a specific search term.
    Do not use when: you have a keyword/boolean query (use search_articles) or
    you want only the biggest trending stories right now (use get_breaking_news).

    Key rules: clustering_enabled and include_nlp_data default to true here too
    (see search_articles's Key rules for the full clustering/response-shape
    notes) -- this endpoint has no exclude_duplicates. Note there is also no
    from_rank/to_rank filter here (unlike search_articles) -- use ranked_only if
    you just want to exclude unranked sources.

    Args:
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        when: Rolling window, e.g. "7d" (default), "30d", "1h", "24h".
        by_parse_date: Use NewsCatcher's parse date instead of publish date;
            populates `parse_date` on results.
        sort_by: "relevancy" (default), "date", or "rank".
        lang: Include only these ISO 639-1 language codes.
        not_lang: Exclude these ISO 639-1 language codes.
        countries: Include only these ISO 3166-1 alpha-2 publisher countries.
        not_countries: Exclude these ISO 3166-1 alpha-2 publisher countries.
        predefined_sources: Top-N sources per country, e.g. ["top 50 US"].
        sources: Include only these domains/subdomains.
        not_sources: Exclude these domains/subdomains.
        not_author_name: Exclude these author names (comma-separated string).
        ranked_only: If False, include unranked sources. Defaults to True if omitted.
        is_headline: Filter to (True) or exclude (False) homepage-featured articles.
        is_opinion: Filter to (True) or exclude (False) opinion pieces.
        is_paid_content: False = only fully public text; True = include paywalled articles.
        parent_url: Filter by categorical/section URL(s) (comma-separated string).
        all_links: Filter to articles mentioning these full URLs.
        all_domain_links: Filter to articles mentioning these domains.
        all_links_text: Filter to articles with link anchor text containing these
            terms; populates `all_links_data` on results.
        word_count_min: Minimum article word count.
        word_count_max: Maximum article word count.
        page: Page number, 1-indexed. Defaults to 1.
        page_size: Results per page, max 1000. Defaults to 100. Clustering
            operates one page at a time -- raise this to your expected result
            count for coherent clusters.
        clustering_enabled: Group results into near-duplicate/related clusters.
            Defaults to True -- the response then has `clusters`/`clusters_count`
            instead of a flat `articles` list (see Returns). Pass False for a
            plain article list.
        clustering_variable: "content" (default), "title", or "summary" -- ignored
            (deprecated) for articles published on/after 2026-01-01.
        clustering_threshold: Similarity threshold in (0, 1], default 0.7.
        include_translation_fields: Add title_translated_en/content_translated_en and
            nlp.translation_summary/translation_ner_* to results (needs include_nlp_data
            too for the nlp.* fields). See this server's instructions for multilingual tips.
        include_nlp_data: Populate each article's `nlp` block. Defaults to True.
        has_nlp: Filter to only articles that have NLP data (indexed July 2023+).
        theme: Filter to these themes -- open vocabulary, not a hard enum.
        not_theme: Exclude these themes.
        org_entity_name: Filter by organization name mentioned. Sent upstream as ORG_entity_name.
        per_entity_name: Filter by person name mentioned. Sent upstream as PER_entity_name.
        loc_entity_name: Filter by location name mentioned. Sent upstream as LOC_entity_name.
        misc_entity_name: Filter by miscellaneous entity name mentioned. Sent upstream as
            MISC_entity_name. All four support AND/OR/NOT/NEAR/COUNT(...) syntax -- see this
            server's instructions for entity search + multilingual search guidance.
        title_sentiment_min: Minimum title sentiment score, -1.0 to 1.0.
        title_sentiment_max: Maximum title sentiment score, -1.0 to 1.0.
        content_sentiment_min: Minimum content sentiment score, -1.0 to 1.0.
        content_sentiment_max: Maximum content sentiment score, -1.0 to 1.0.
        custom_tags: Organization-specific tag filter, e.g. {"my_taxonomy": ["Tag1"]}.
        robots_compliant: Filter by whether the source's robots.txt permits scraping.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`, and
        (by default, since clustering_enabled defaults to True) `clusters_count` +
        `clusters` instead of a flat `articles` list -- pass clustering_enabled=False
        for a plain article list.

    Common API errors:
        - 401: missing or invalid API token.
        - 403: a requested field/filter isn't included in your plan.
        - 422: invalid parameter combination (e.g. bad `when` format).
    """
    try:
        validate_choice(sort_by, SORT_BY_VALUES, "sort_by")
        validate_choice(clustering_variable, CLUSTERING_VARIABLE_VALUES, "clustering_variable")
        validate_page_params(page, page_size)
        validate_clustering_threshold(clustering_threshold)
        validate_sentiment_range(title_sentiment_min, "title_sentiment_min")
        validate_sentiment_range(title_sentiment_max, "title_sentiment_max")
        validate_sentiment_range(content_sentiment_min, "content_sentiment_min")
        validate_sentiment_range(content_sentiment_max, "content_sentiment_max")
        validate_lang(lang, "lang")
        validate_lang(not_lang, "not_lang")
        validate_country(countries, "countries")
        validate_country(not_countries, "not_countries")

        body: dict[str, Any] = {"page": page, "page_size": page_size}
        _add_field(body, "when", when)
        _add_field(body, "by_parse_date", by_parse_date)
        _add_field(body, "sort_by", sort_by)
        _add_list_field(body, "lang", lang)
        _add_list_field(body, "not_lang", not_lang)
        _add_list_field(body, "countries", countries)
        _add_list_field(body, "not_countries", not_countries)
        _add_list_field(body, "predefined_sources", predefined_sources)
        _add_list_field(body, "sources", sources)
        _add_list_field(body, "not_sources", not_sources)
        _add_field(body, "not_author_name", not_author_name)
        _add_field(body, "ranked_only", ranked_only)
        _add_field(body, "is_headline", is_headline)
        _add_field(body, "is_opinion", is_opinion)
        _add_field(body, "is_paid_content", is_paid_content)
        _add_field(body, "parent_url", parent_url)
        _add_list_field(body, "all_links", all_links)
        _add_list_field(body, "all_domain_links", all_domain_links)
        _add_list_field(body, "all_links_text", all_links_text)
        _add_field(body, "word_count_min", word_count_min)
        _add_field(body, "word_count_max", word_count_max)
        _add_field(body, "clustering_enabled", clustering_enabled)
        _add_field(body, "clustering_variable", clustering_variable)
        _add_field(body, "clustering_threshold", clustering_threshold)
        _add_field(body, "include_translation_fields", include_translation_fields)
        _add_field(body, "include_nlp_data", include_nlp_data)
        _add_field(body, "has_nlp", has_nlp)
        _add_list_field(body, "theme", theme)
        _add_list_field(body, "not_theme", not_theme)
        _add_field(body, "ORG_entity_name", org_entity_name)
        _add_field(body, "PER_entity_name", per_entity_name)
        _add_field(body, "LOC_entity_name", loc_entity_name)
        _add_field(body, "MISC_entity_name", misc_entity_name)
        _add_field(body, "title_sentiment_min", title_sentiment_min)
        _add_field(body, "title_sentiment_max", title_sentiment_max)
        _add_field(body, "content_sentiment_min", content_sentiment_min)
        _add_field(body, "content_sentiment_max", content_sentiment_max)
        body.update(flatten_custom_tags(custom_tags))
        _add_field(body, "robots_compliant", robots_compliant)

        source = build_source(fields, body.get("clustering_enabled") is True)
        if source is not None:
            body["_source"] = source
        result = await make_api_request(api_token=api_token, path="/api/latest_headlines", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_breaking_news(
    api_token: str = "",
    sort_by: str | None = None,
    ranked_only: bool | None = None,
    from_rank: int | None = None,
    to_rank: int | None = None,
    page: int = 1,
    page_size: int = 100,
    top_n_articles: int | None = None,
    include_translation_fields: bool | None = None,
    include_nlp_data: bool | None = True,
    has_nlp: bool | None = None,
    theme: list[str] | None = None,
    not_theme: list[str] | None = None,
    org_entity_name: str | None = None,
    per_entity_name: str | None = None,
    loc_entity_name: str | None = None,
    misc_entity_name: str | None = None,
    title_sentiment_min: float | None = None,
    title_sentiment_max: float | None = None,
    content_sentiment_min: float | None = None,
    content_sentiment_max: float | None = None,
    fields: list[str] | None = None,
) -> str:
    """
    Actively-trending news event clusters, ordered by how heavily each is covered.

    Use when: the user wants "what's the big story right now" -- no query, no
    date range, no language/country/source filters exist on this endpoint at all;
    the time scope is a fixed internal window (~24h) that NewsCatcher controls.
    Do not use when: you have a specific topic/keyword (use search_articles) or
    want a general recent-headlines feed rather than only the trending stories
    (use get_latest_headlines).

    Key rules:
    - include_nlp_data defaults to true here for richer results (pass false to
      opt out); there's no clustering_enabled/exclude_duplicates on this endpoint
      -- events are already NewsCatcher's own trending-story groupings.
    - top_n_articles * page_size must not exceed 1000 (validated client-side).
    - Each returned event's article objects are a reduced subset of the usual
      Article Object (no all_links_data/robots_compliant/custom_tags/
      additional_domain_info) -- don't expect those fields here.

    Args:
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        sort_by: "relevancy" (default), "date", or "rank" -- applies within each event's articles.
        ranked_only: If False, include unranked sources. Defaults to True if omitted.
        from_rank: Minimum source popularity rank (1-999999).
        to_rank: Maximum source popularity rank (1-999999).
        page: Page number over the list of breaking-news events, 1-indexed. Defaults to 1.
        page_size: Events per page, max 1000. Defaults to 100.
        top_n_articles: Articles to include per event, 1-100, default 1.
            top_n_articles * page_size must not exceed 1000.
        include_translation_fields: Add title_translated_en/content_translated_en and
            nlp.translation_summary/translation_ner_* to results (needs include_nlp_data
            too for the nlp.* fields). See this server's instructions for multilingual tips.
        include_nlp_data: Populate each article's `nlp` block. Defaults to True.
        has_nlp: Filter to only articles that have NLP data (indexed July 2023+).
        theme: Filter to these themes -- open vocabulary, not a hard enum.
        not_theme: Exclude these themes.
        org_entity_name: Filter by organization name mentioned. Sent upstream as ORG_entity_name.
        per_entity_name: Filter by person name mentioned. Sent upstream as PER_entity_name.
        loc_entity_name: Filter by location name mentioned. Sent upstream as LOC_entity_name.
        misc_entity_name: Filter by miscellaneous entity name mentioned. Sent upstream as
            MISC_entity_name. All four support AND/OR/NOT/NEAR/COUNT(...) syntax -- see this
            server's instructions for entity search + multilingual search guidance.
        title_sentiment_min: Minimum title sentiment score, -1.0 to 1.0.
        title_sentiment_max: Maximum title sentiment score, -1.0 to 1.0.
        content_sentiment_min: Minimum content sentiment score, -1.0 to 1.0.
        content_sentiment_max: Maximum content sentiment score, -1.0 to 1.0.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`, and
        `breaking_news_events` (each `{event_id, articles_count, articles}`),
        ordered by cluster size (most-covered stories first).

    Common API errors:
        - 401: missing or invalid API token.
        - 403: a requested field/filter isn't included in your plan.
        - 422: top_n_articles * page_size exceeds 1000, or other invalid params.
    """
    try:
        validate_choice(sort_by, SORT_BY_VALUES, "sort_by")
        validate_page_params(page, page_size)
        validate_rank(from_rank, "from_rank")
        validate_rank(to_rank, "to_rank")
        validate_top_n_articles_page_size(top_n_articles, page_size)
        validate_sentiment_range(title_sentiment_min, "title_sentiment_min")
        validate_sentiment_range(title_sentiment_max, "title_sentiment_max")
        validate_sentiment_range(content_sentiment_min, "content_sentiment_min")
        validate_sentiment_range(content_sentiment_max, "content_sentiment_max")

        body: dict[str, Any] = {"page": page, "page_size": page_size}
        _add_field(body, "sort_by", sort_by)
        _add_field(body, "ranked_only", ranked_only)
        _add_field(body, "from_rank", from_rank)
        _add_field(body, "to_rank", to_rank)
        _add_field(body, "top_n_articles", top_n_articles)
        _add_field(body, "include_translation_fields", include_translation_fields)
        _add_field(body, "include_nlp_data", include_nlp_data)
        _add_field(body, "has_nlp", has_nlp)
        _add_list_field(body, "theme", theme)
        _add_list_field(body, "not_theme", not_theme)
        _add_field(body, "ORG_entity_name", org_entity_name)
        _add_field(body, "PER_entity_name", per_entity_name)
        _add_field(body, "LOC_entity_name", loc_entity_name)
        _add_field(body, "MISC_entity_name", misc_entity_name)
        _add_field(body, "title_sentiment_min", title_sentiment_min)
        _add_field(body, "title_sentiment_max", title_sentiment_max)
        _add_field(body, "content_sentiment_min", content_sentiment_min)
        _add_field(body, "content_sentiment_max", content_sentiment_max)

        result = await make_api_request(api_token=api_token, path="/api/breaking_news", json_data=body)
        return json.dumps(_project_result(result, fields), indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def search_by_author(
    author_name: str,
    api_token: str = "",
    not_author_name: str | None = None,
    predefined_sources: list[str] | None = None,
    sources: list[str] | None = None,
    not_sources: list[str] | None = None,
    lang: list[str] | None = None,
    not_lang: list[str] | None = None,
    countries: list[str] | None = None,
    not_countries: list[str] | None = None,
    from_: str | None = None,
    to_: str | None = None,
    published_date_precision: str | None = None,
    by_parse_date: bool | None = None,
    sort_by: str | None = None,
    ranked_only: bool | None = None,
    from_rank: int | None = None,
    to_rank: int | None = None,
    is_headline: bool | None = None,
    is_opinion: bool | None = None,
    is_paid_content: bool | None = None,
    parent_url: str | None = None,
    all_links: list[str] | None = None,
    all_domain_links: list[str] | None = None,
    all_links_text: list[str] | None = None,
    word_count_min: int | None = None,
    word_count_max: int | None = None,
    page: int = 1,
    page_size: int = 100,
    include_translation_fields: bool | None = None,
    include_nlp_data: bool | None = True,
    has_nlp: bool | None = None,
    theme: list[str] | None = None,
    not_theme: list[str] | None = None,
    ner_name: str | None = None,
    title_sentiment_min: float | None = None,
    title_sentiment_max: float | None = None,
    content_sentiment_min: float | None = None,
    content_sentiment_max: float | None = None,
    custom_tags: dict[str, list[str]] | None = None,
    robots_compliant: bool | None = None,
    fields: list[str] | None = None,
) -> str:
    """
    All articles written by one specific byline (exact match).

    Use when: you know the exact author name and want their article history.
    Do not use when: you have a general keyword query (use search_articles) --
    this endpoint has no `q`/`search_in` and no clustering.

    Note: unlike search_articles' 4-way ORG/PER/LOC/MISC entity split, this
    endpoint exposes a single unified `ner_name` filter. include_nlp_data
    defaults to true here too, for richer results -- pass false to opt out.

    Args:
        author_name: Exact author name to search for (required).
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        not_author_name: Exclude these other author names (comma-separated string).
        predefined_sources: Top-N sources per country, e.g. ["top 50 US"].
        sources: Include only these domains/subdomains.
        not_sources: Exclude these domains/subdomains.
        lang: Include only these ISO 639-1 language codes.
        not_lang: Exclude these ISO 639-1 language codes.
        countries: Include only these ISO 3166-1 alpha-2 publisher countries.
        not_countries: Exclude these ISO 3166-1 alpha-2 publisher countries.
        from_: Start of date range. Defaults to "7 days ago" if omitted.
        to_: End of date range. Defaults to "now" if omitted.
        published_date_precision: "full", "timezone unknown", or "date".
        by_parse_date: Use NewsCatcher's parse date instead of publish date.
        sort_by: "relevancy" (default), "date", or "rank".
        ranked_only: If False, include unranked sources. Defaults to True if omitted.
        from_rank: Minimum source popularity rank (1-999999).
        to_rank: Maximum source popularity rank (1-999999).
        is_headline: Filter to (True) or exclude (False) homepage-featured articles.
        is_opinion: Filter to (True) or exclude (False) opinion pieces.
        is_paid_content: False = only fully public text; True = include paywalled articles.
        parent_url: Filter by categorical/section URL(s) (comma-separated string).
        all_links: Filter to articles mentioning these full URLs.
        all_domain_links: Filter to articles mentioning these domains.
        all_links_text: Filter to articles with link anchor text containing these terms.
        word_count_min: Minimum article word count.
        word_count_max: Maximum article word count.
        page: Page number, 1-indexed. Defaults to 1.
        page_size: Results per page, max 1000. Defaults to 100.
        include_translation_fields: Add title_translated_en/content_translated_en and
            nlp.translation_summary/translation_ner_* to results (needs include_nlp_data
            too for the nlp.* fields). See this server's instructions for multilingual tips.
        include_nlp_data: Populate each article's `nlp` block. Defaults to True.
        has_nlp: Filter to only articles that have NLP data (indexed July 2023+).
        theme: Filter to these themes -- open vocabulary, not a hard enum.
        not_theme: Exclude these themes.
        ner_name: Filter by any named entity mentioned (person, org, location, or
            misc -- unified, unlike search_articles' 4-way split). Supports
            AND/OR/NOT/NEAR/COUNT(...) syntax -- see this server's instructions.
        title_sentiment_min: Minimum title sentiment score, -1.0 to 1.0.
        title_sentiment_max: Maximum title sentiment score, -1.0 to 1.0.
        content_sentiment_min: Minimum content sentiment score, -1.0 to 1.0.
        content_sentiment_max: Maximum content sentiment score, -1.0 to 1.0.
        custom_tags: Organization-specific tag filter, e.g. {"my_taxonomy": ["Tag1"]}.
        robots_compliant: Filter by whether the source's robots.txt permits scraping.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`,
        `articles` (empty list if no matches found), `user_input`.

    Common API errors:
        - 401: missing or invalid API token.
        - 403: a requested field/filter isn't included in your plan.
        - 422: invalid parameter combination.
    """
    try:
        validate_choice(sort_by, SORT_BY_VALUES, "sort_by")
        validate_page_params(page, page_size)
        validate_rank(from_rank, "from_rank")
        validate_rank(to_rank, "to_rank")
        validate_sentiment_range(title_sentiment_min, "title_sentiment_min")
        validate_sentiment_range(title_sentiment_max, "title_sentiment_max")
        validate_sentiment_range(content_sentiment_min, "content_sentiment_min")
        validate_sentiment_range(content_sentiment_max, "content_sentiment_max")
        validate_lang(lang, "lang")
        validate_lang(not_lang, "not_lang")
        validate_country(countries, "countries")
        validate_country(not_countries, "not_countries")

        body: dict[str, Any] = {"author_name": author_name, "page": page, "page_size": page_size}
        _add_field(body, "not_author_name", not_author_name)
        _add_list_field(body, "predefined_sources", predefined_sources)
        _add_list_field(body, "sources", sources)
        _add_list_field(body, "not_sources", not_sources)
        _add_list_field(body, "lang", lang)
        _add_list_field(body, "not_lang", not_lang)
        _add_list_field(body, "countries", countries)
        _add_list_field(body, "not_countries", not_countries)
        _add_field(body, "from_", from_)
        _add_field(body, "to_", to_)
        _add_field(body, "published_date_precision", published_date_precision)
        _add_field(body, "by_parse_date", by_parse_date)
        _add_field(body, "sort_by", sort_by)
        _add_field(body, "ranked_only", ranked_only)
        _add_field(body, "from_rank", from_rank)
        _add_field(body, "to_rank", to_rank)
        _add_field(body, "is_headline", is_headline)
        _add_field(body, "is_opinion", is_opinion)
        _add_field(body, "is_paid_content", is_paid_content)
        _add_field(body, "parent_url", parent_url)
        _add_list_field(body, "all_links", all_links)
        _add_list_field(body, "all_domain_links", all_domain_links)
        _add_list_field(body, "all_links_text", all_links_text)
        _add_field(body, "word_count_min", word_count_min)
        _add_field(body, "word_count_max", word_count_max)
        _add_field(body, "include_translation_fields", include_translation_fields)
        _add_field(body, "include_nlp_data", include_nlp_data)
        _add_field(body, "has_nlp", has_nlp)
        _add_list_field(body, "theme", theme)
        _add_list_field(body, "not_theme", not_theme)
        _add_field(body, "ner_name", ner_name)
        _add_field(body, "title_sentiment_min", title_sentiment_min)
        _add_field(body, "title_sentiment_max", title_sentiment_max)
        _add_field(body, "content_sentiment_min", content_sentiment_min)
        _add_field(body, "content_sentiment_max", content_sentiment_max)
        body.update(flatten_custom_tags(custom_tags))
        _add_field(body, "robots_compliant", robots_compliant)

        source = build_source(fields, clustered=False)
        if source is not None:
            body["_source"] = source
        result = await make_api_request(api_token=api_token, path="/api/authors", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def search_by_link(
    api_token: str = "",
    ids: list[str] | None = None,
    links: list[str] | None = None,
    from_: str | None = None,
    to_: str | None = None,
    page: int = 1,
    page_size: int = 100,
    robots_compliant: bool | None = None,
    fields: list[str] | None = None,
) -> str:
    """
    Look up specific, already-known articles by NewsCatcher id or URL.

    Use when: you already have article id(s) (from a prior search's `id` field)
    or URL(s) and want their full current data.
    Do not use when: you're searching for articles you don't already have a
    reference to -- use search_articles instead. This is a lookup, not a search:
    it has no `q`, no language/country/theme/NLP/clustering filters.

    Key rule: provide exactly one of ids or links, never both.

    Args:
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        ids: NewsCatcher article `id` values (from a prior search result's `id`
            field). Mutually exclusive with links.
        links: Full article URLs. Mutually exclusive with ids.
        from_: Start of date range. Defaults to "1 month ago" if omitted (a wider
            default than the other search tools, since you're looking up specific
            known articles that may be older).
        to_: End of date range. Defaults to "now" if omitted.
        page: Page number, 1-indexed. Defaults to 1.
        page_size: Results per page, max 1000. Defaults to 100.
        robots_compliant: Filter by whether the source's robots.txt permits scraping.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`, `articles`.

    Common API errors:
        - 401: missing or invalid API token.
        - 422: both ids and links provided (or neither).
    """
    try:
        validate_ids_or_links(ids, links)
        validate_page_params(page, page_size)

        body: dict[str, Any] = {"page": page, "page_size": page_size}
        _add_list_field(body, "ids", ids)
        _add_list_field(body, "links", links)
        _add_field(body, "from_", from_)
        _add_field(body, "to_", to_)
        _add_field(body, "robots_compliant", robots_compliant)

        source = build_source(fields, clustered=False)
        if source is not None:
            body["_source"] = source
        result = await make_api_request(api_token=api_token, path="/api/search_by_link", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_sources(
    api_token: str = "",
    lang: list[str] | None = None,
    countries: list[str] | None = None,
    predefined_sources: list[str] | None = None,
    source_name: str | None = None,
    source_url: list[str] | None = None,
    include_additional_info: bool | None = None,
    is_news_domain: bool | None = None,
    news_domain_type: str | None = None,
    news_type: list[str] | None = None,
    from_rank: int | None = None,
    to_rank: int | None = None,
) -> str:
    """
    Browse and verify the publisher index -- returns sources, not articles.

    Use when: you want to check whether one or many domains are indexed
    (coverage verification), look up rank/country/type, or browse top sources
    for a country before searching.
    Do not use when: you want articles -- use search_articles/get_latest_headlines.

    Key rules:
    - At least one filter parameter is required -- there is no "list everything"
      call with zero filters.
    - source_url can only be combined with include_additional_info=True.
    - There is no pagination on this endpoint and no not_* exclude variants.

    Args:
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        lang: Include only sources publishing in these ISO 639-1 language codes.
        countries: Include only sources in these ISO 3166-1 alpha-2 countries.
        predefined_sources: Top-N sources per country, e.g. ["top 50 US"].
        source_name: Fuzzy (partial) match on publisher display name -- use for
            broad discovery, e.g. source_name="sport" finds any source with
            "sport" in its name.
        source_url: One or many exact domains to check coverage for, e.g.
            ["si.com", "sportskeeda.com"] -- pass many at once to bulk-check
            coverage instead of one tool call per domain. Requires
            include_additional_info=True.
        include_additional_info: Add nb_articles_for_7d/country/rank/
            is_news_domain/news_domain_type/news_type/robots_compliant to each result.
        is_news_domain: Filter to (True) or exclude (False) recognized news domains.
        news_domain_type: "Original Content", "Aggregator", "Press Releases",
            "Republisher", or "Other".
        news_type: Open-vocabulary content-type filter -- see the docs'
            Enumerated Parameters page for the full documented list.
        from_rank: Minimum source popularity rank (1-999999). Use with to_rank to
            restrict to high-authority publications only.
        to_rank: Maximum source popularity rank (1-999999).

    Returns:
        JSON with `message` (e.g. a plan-based row-limit note), `sources` (each
        either a bare domain string or `{name_source, domain_url, logo,
        additional_info}`), `user_input`. A domain absent from `sources` when
        checked via source_url is not covered by News API -- report uncovered
        domains to support@newscatcherapi.com if coverage is needed.

    Common API errors:
        - 401: missing or invalid API token.
        - 422: no filter parameter provided, or source_url provided without
          include_additional_info=True.
    """
    try:
        validate_choice(news_domain_type, NEWS_DOMAIN_TYPE_VALUES, "news_domain_type")
        validate_sources_params(source_url, include_additional_info)
        validate_sources_has_filter(
            lang,
            countries,
            predefined_sources,
            source_name,
            source_url,
            is_news_domain,
            news_domain_type,
            news_type,
            from_rank,
            to_rank,
        )
        validate_rank(from_rank, "from_rank")
        validate_rank(to_rank, "to_rank")
        validate_lang(lang, "lang")
        validate_country(countries, "countries")

        body: dict[str, Any] = {}
        _add_list_field(body, "lang", lang)
        _add_list_field(body, "countries", countries)
        _add_list_field(body, "predefined_sources", predefined_sources)
        _add_field(body, "source_name", source_name)
        _add_list_field(body, "source_url", source_url)
        _add_field(body, "include_additional_info", include_additional_info)
        _add_field(body, "is_news_domain", is_news_domain)
        _add_field(body, "news_domain_type", news_domain_type)
        _add_list_field(body, "news_type", news_type)
        _add_field(body, "from_rank", from_rank)
        _add_field(body, "to_rank", to_rank)

        result = await make_api_request(api_token=api_token, path="/api/sources", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_aggregation_count(
    q: str,
    api_token: str = "",
    aggregation_by: str | None = None,
    search_in: list[str] | None = None,
    predefined_sources: list[str] | None = None,
    sources: list[str] | None = None,
    not_sources: list[str] | None = None,
    lang: list[str] | None = None,
    not_lang: list[str] | None = None,
    countries: list[str] | None = None,
    not_countries: list[str] | None = None,
    not_author_name: str | None = None,
    from_: str | None = None,
    to_: str | None = None,
    published_date_precision: str | None = None,
    by_parse_date: bool | None = None,
    sort_by: str | None = None,
    ranked_only: bool | None = None,
    from_rank: int | None = None,
    to_rank: int | None = None,
    is_headline: bool | None = None,
    is_opinion: bool | None = None,
    is_paid_content: bool | None = None,
    parent_url: str | None = None,
    all_links: list[str] | None = None,
    all_domain_links: list[str] | None = None,
    all_links_text: list[str] | None = None,
    word_count_min: int | None = None,
    word_count_max: int | None = None,
    page: int = 1,
    page_size: int = 100,
    include_nlp_data: bool | None = None,
    has_nlp: bool | None = None,
    theme: list[str] | None = None,
    not_theme: list[str] | None = None,
    org_entity_name: str | None = None,
    per_entity_name: str | None = None,
    loc_entity_name: str | None = None,
    misc_entity_name: str | None = None,
    title_sentiment_min: float | None = None,
    title_sentiment_max: float | None = None,
    content_sentiment_min: float | None = None,
    content_sentiment_max: float | None = None,
    robots_compliant: bool | None = None,
) -> str:
    """
    Time-bucketed article-volume counts for a query -- no articles returned.

    Use when: you want to gauge how many articles match a query/date range
    BEFORE running search_articles, especially for broad or undated queries
    that risk exceeding the 10,000-result cap. Also the recommended first step
    before any large historical pull: call this with aggregation_by="day" over
    the target range to measure actual volume, then size search_articles time
    chunks from that density (see this server's instructions for the lookup
    table -- denser topics need hourly chunks, sparse ones can use a month).
    Do not use when: you actually want the articles themselves -- use search_articles.

    Args:
        q: Same boolean/keyword query syntax as search_articles -- see that
            tool's docstring for the full grammar. CRITICAL: always quote
            multi-word phrases (unquoted, the API inserts AND between bare words,
            so q='AI OR artificial intelligence' is parsed as
            q='AI OR artificial AND intelligence' and gets a 422).
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.
        aggregation_by: Bucket size: "day" (default), "hour", or "month".
        search_in: Which fields to search q in. Max 2 of: title, content, summary,
            title_content (default), title_translated, content_translated,
            summary_translated, title_content_translated. For multilingual coverage,
            pass ["title_content", "title_content_translated"] to search original and
            translated text together (see this server's instructions).
        predefined_sources: Top-N sources per country, e.g. ["top 50 US"].
        sources: Include only these domains/subdomains.
        not_sources: Exclude these domains/subdomains.
        lang: Include only these ISO 639-1 language codes.
        not_lang: Exclude these ISO 639-1 language codes.
        countries: Include only these ISO 3166-1 alpha-2 publisher countries.
        not_countries: Exclude these ISO 3166-1 alpha-2 publisher countries.
        not_author_name: Exclude these author names (comma-separated string).
        from_: Start of date range. Defaults to "7 days ago" if omitted.
        to_: End of date range. Defaults to "now" if omitted.
        published_date_precision: "full", "timezone unknown", or "date".
        by_parse_date: Use NewsCatcher's parse date instead of publish date.
        sort_by: "relevancy" (default), "date", or "rank".
        ranked_only: If False, include unranked sources. Defaults to True if omitted.
        from_rank: Minimum source popularity rank (1-999999).
        to_rank: Maximum source popularity rank (1-999999).
        is_headline: Filter to (True) or exclude (False) homepage-featured articles.
        is_opinion: Filter to (True) or exclude (False) opinion pieces.
        is_paid_content: False = only fully public text; True = include paywalled articles.
        parent_url: Filter by categorical/section URL(s) (comma-separated string).
        all_links: Filter to articles mentioning these full URLs.
        all_domain_links: Filter to articles mentioning these domains.
        all_links_text: Filter to articles with link anchor text containing these terms.
        word_count_min: Minimum article word count.
        word_count_max: Maximum article word count.
        page: Page number over the aggregation buckets, 1-indexed. Defaults to 1.
        page_size: Buckets per page, max 1000. Defaults to 100.
        include_nlp_data: Consider NLP-derived fields when counting. No articles
            are returned by this endpoint, so unlike the article-returning tools
            this defaults to None/omitted rather than True.
        has_nlp: Filter to only articles that have NLP data (indexed July 2023+).
        theme: Filter to these themes -- open vocabulary, not a hard enum.
        not_theme: Exclude these themes.
        org_entity_name: Filter by organization name mentioned. Sent upstream as ORG_entity_name.
        per_entity_name: Filter by person name mentioned. Sent upstream as PER_entity_name.
        loc_entity_name: Filter by location name mentioned. Sent upstream as LOC_entity_name.
        misc_entity_name: Filter by miscellaneous entity name mentioned. Sent upstream as
            MISC_entity_name. All four support AND/OR/NOT/NEAR/COUNT(...) syntax -- see this
            server's instructions for entity search + multilingual search guidance.
        title_sentiment_min: Minimum title sentiment score, -1.0 to 1.0.
        title_sentiment_max: Maximum title sentiment score, -1.0 to 1.0.
        content_sentiment_min: Minimum content sentiment score, -1.0 to 1.0.
        content_sentiment_max: Maximum content sentiment score, -1.0 to 1.0.
        robots_compliant: Filter by whether the source's robots.txt permits scraping.

    Returns:
        JSON with `status`, `total_hits`, `page`, `total_pages`, `page_size`,
        `aggregations` (one or more `{aggregation_count: [{time_frame, article_count}]}`),
        `user_input`.

    Common API errors:
        - 401: missing or invalid API token.
        - 403: a requested field/filter isn't included in your plan.
        - 422: invalid query syntax or parameter combination.
    """
    try:
        lint_query(q)
        validate_choice(aggregation_by, AGGREGATION_BY_VALUES, "aggregation_by")
        validate_search_in(search_in)
        validate_choice(sort_by, SORT_BY_VALUES, "sort_by")
        validate_page_params(page, page_size)
        validate_rank(from_rank, "from_rank")
        validate_rank(to_rank, "to_rank")
        validate_sentiment_range(title_sentiment_min, "title_sentiment_min")
        validate_sentiment_range(title_sentiment_max, "title_sentiment_max")
        validate_sentiment_range(content_sentiment_min, "content_sentiment_min")
        validate_sentiment_range(content_sentiment_max, "content_sentiment_max")
        validate_lang(lang, "lang")
        validate_lang(not_lang, "not_lang")
        validate_country(countries, "countries")
        validate_country(not_countries, "not_countries")

        body: dict[str, Any] = {"q": q, "page": page, "page_size": page_size}
        _add_field(body, "aggregation_by", aggregation_by)
        _add_list_field(body, "search_in", search_in)
        _add_list_field(body, "predefined_sources", predefined_sources)
        _add_list_field(body, "sources", sources)
        _add_list_field(body, "not_sources", not_sources)
        _add_list_field(body, "lang", lang)
        _add_list_field(body, "not_lang", not_lang)
        _add_list_field(body, "countries", countries)
        _add_list_field(body, "not_countries", not_countries)
        _add_field(body, "not_author_name", not_author_name)
        _add_field(body, "from_", from_)
        _add_field(body, "to_", to_)
        _add_field(body, "published_date_precision", published_date_precision)
        _add_field(body, "by_parse_date", by_parse_date)
        _add_field(body, "sort_by", sort_by)
        _add_field(body, "ranked_only", ranked_only)
        _add_field(body, "from_rank", from_rank)
        _add_field(body, "to_rank", to_rank)
        _add_field(body, "is_headline", is_headline)
        _add_field(body, "is_opinion", is_opinion)
        _add_field(body, "is_paid_content", is_paid_content)
        _add_field(body, "parent_url", parent_url)
        _add_list_field(body, "all_links", all_links)
        _add_list_field(body, "all_domain_links", all_domain_links)
        _add_list_field(body, "all_links_text", all_links_text)
        _add_field(body, "word_count_min", word_count_min)
        _add_field(body, "word_count_max", word_count_max)
        _add_field(body, "include_nlp_data", include_nlp_data)
        _add_field(body, "has_nlp", has_nlp)
        _add_list_field(body, "theme", theme)
        _add_list_field(body, "not_theme", not_theme)
        _add_field(body, "ORG_entity_name", org_entity_name)
        _add_field(body, "PER_entity_name", per_entity_name)
        _add_field(body, "LOC_entity_name", loc_entity_name)
        _add_field(body, "MISC_entity_name", misc_entity_name)
        _add_field(body, "title_sentiment_min", title_sentiment_min)
        _add_field(body, "title_sentiment_max", title_sentiment_max)
        _add_field(body, "content_sentiment_min", content_sentiment_min)
        _add_field(body, "content_sentiment_max", content_sentiment_max)
        _add_field(body, "robots_compliant", robots_compliant)

        result = await make_api_request(api_token=api_token, path="/api/aggregation_count", json_data=body)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_subscription(api_token: str = "") -> str:
    """
    Check your News API plan, quota, and remaining calls.

    Use when: you want to confirm a key is valid, see which plan tier you're on,
    or check remaining calls before running a large batch of searches.

    Args:
        api_token: News API token. Optional if provided via x-api-token header or
            NEWS_API_KEY env var.

    Returns:
        JSON with `active` (bool), `concurrent_calls` (int), `plan` (string,
        e.g. "v3_nlp"), `plan_calls` (monthly quota), `remaining_calls`,
        `historical_days` (how far back you can query).

    Common API errors:
        - 401: missing or invalid API token.
    """
    try:
        result = await make_api_request(api_token=api_token, path="/api/subscription", json_data=None)
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def check_health(api_token: str = "") -> str:
    """
    Local liveness ping -- confirms this MCP server process is up and responding.

    Unlike every other tool here, this never calls the News API and never needs
    a key: News API v3 has no public health/version endpoint, so this exists
    purely as a zero-setup first call for any new MCP client, and to poll
    server readiness in CI.

    Args:
        api_token: Ignored. Present only for calling-convention consistency with
            every other tool in this server.

    Returns:
        JSON with `status: "ok"` and `server: "news-mcp"`.
    """
    return json.dumps({"status": "ok", "server": "news-mcp"}, indent=2)


# Patch mcp.http_app to always inject ApiTokenASGIMiddleware, regardless of how the
# server is invoked (uvicorn server:app, fastmcp run server.py:mcp, python server.py, etc.)
_original_http_app = mcp.http_app


def _http_app_with_api_token_middleware(*args: Any, middleware: list | None = None, **kwargs: Any) -> Any:
    mw = [StarletteMiddleware(ApiTokenASGIMiddleware)]
    if middleware:
        mw = mw + list(middleware)
    return _original_http_app(*args, middleware=mw, **kwargs)


mcp.http_app = _http_app_with_api_token_middleware  # type: ignore[method-assign]

# Module-level ASGI app for deployment via `uvicorn server:app`
app = mcp.http_app()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
