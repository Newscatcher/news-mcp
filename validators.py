"""Validation and coercion helpers for the News API v3 MCP tools.

Framework-agnostic (no fastmcp/httpx imports) so it can be unit-tested and
reused independently of the MCP server itself.

Guiding principle: only hard-validate constraints the News API v3 OpenAPI
spec itself encodes as machine-checkable (a real `enum`, a `minimum`/
`maximum`, an explicit "not both" rule). Several fields the docs describe
with a list of "suggested" values (theme, news_type, search_in,
published_date_precision) are plain `string` fields in the actual schema,
not closed vocabularies -- hard-rejecting unknown values there would reject
a legitimate value the API would happily accept.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, TypedDict

# --- Real, schema-enforced enums --------------------------------------------
# Confirmed against the live OpenAPI spec: these four fields are actual
# `enum` arrays. Nothing else in this module hard-validates against a fixed
# vocabulary -- see the module docstring.
SORT_BY_VALUES = ("relevancy", "date", "rank")
AGGREGATION_BY_VALUES = ("day", "hour", "month")
NEWS_DOMAIN_TYPE_VALUES = ("Original Content", "Aggregator", "Press Releases", "Republisher", "Other")
CLUSTERING_VARIABLE_VALUES = ("content", "title", "summary")

# search_in's allowed values ARE documented (8 known values) but the field
# itself is plain `string` in the schema -- so validate_search_in below only
# enforces the confirmed max-2 cardinality rule, not this vocabulary.
SEARCH_IN_VALUES = (
    "title",
    "content",
    "summary",
    "title_content",
    "title_translated",
    "content_translated",
    "summary_translated",
    "title_content_translated",
)

# Characters News API v3's `q` grammar forbids outright, plus their
# percent-encoded equivalents, per the Advanced Querying guide.
_FORBIDDEN_Q_CHARS = ("[", "]", "/", "\\", ":", "^")
_FORBIDDEN_Q_ENCODED = ("%5B", "%5D", "%2F", "%5C", "%3A", "%5E")

# Clustering (clustering_variable / date-range straddling) changed behavior
# for articles published on/after this date -- confirmed in the docs.
CLUSTERING_MODEL_CUTOFF = datetime(2026, 1, 1)


# --- Response shape hints (loose, optional-heavy -- not enforced) ----------
# These exist for readability/IDE hints only. Tools pass upstream JSON
# through ~verbatim rather than validating responses against these, since at
# least one confirmed real field (duplicate_count/duplicate_articles_group_id,
# populated when exclude_duplicates=true) is already absent from the formal
# ArticleEntity schema -- i.e. the live API surface already outruns its own
# spec, so a strict response model would be wrong on day one.


class LinkDataItem(TypedDict, total=False):
    domain_url: str
    link: str
    text: str


class ArticleObject(TypedDict, total=False):
    title: str
    link: str
    domain_url: str
    full_domain_url: str
    parent_url: str
    rank: int
    id: str
    score: float
    author: str
    authors: list[str] | str
    journalists: list[str] | str | None
    published_date: str
    published_date_precision: str
    updated_date: str | None
    updated_date_precision: str | None
    parse_date: str | None
    name_source: str
    is_headline: bool
    paid_content: bool
    country: str
    rights: str
    media: str
    language: str
    description: str
    content: str
    title_translated_en: str | None
    content_translated_en: str | None
    word_count: int
    is_opinion: bool
    twitter_account: str | None
    all_links: list[str] | str
    all_domain_links: list[str] | str
    all_links_data: list[LinkDataItem]
    nlp: dict[str, Any]
    robots_compliant: bool
    custom_tags: dict[str, list[str]]
    additional_domain_info: dict[str, Any]
    duplicate_count: int
    duplicate_articles_group_id: str


class SearchResponse(TypedDict, total=False):
    status: str
    total_hits: int
    page: int
    total_pages: int
    page_size: int
    articles: list[ArticleObject]
    user_input: dict[str, Any]


class ClusterItem(TypedDict, total=False):
    cluster_id: str
    cluster_size: int
    articles: list[ArticleObject]


class ClusteredSearchResponse(TypedDict, total=False):
    status: str
    total_hits: int
    page: int
    total_pages: int
    page_size: int
    clusters_count: int
    clusters: list[ClusterItem]
    user_input: dict[str, Any]


class BreakingNewsEvent(TypedDict, total=False):
    event_id: str
    articles_count: int
    articles: list[ArticleObject]


class BreakingNewsResponse(TypedDict, total=False):
    status: str
    total_hits: int
    page: int
    total_pages: int
    page_size: int
    breaking_news_events: list[BreakingNewsEvent]
    user_input: dict[str, Any]


class SourceInfo(TypedDict, total=False):
    name_source: str
    domain_url: str
    logo: str | None
    additional_info: dict[str, Any]


class SourcesResponse(TypedDict, total=False):
    message: str
    sources: list[SourceInfo | str]
    user_input: dict[str, Any]


class AggregationBucket(TypedDict, total=False):
    time_frame: str
    article_count: int


class AggregationItem(TypedDict, total=False):
    aggregation_count: list[AggregationBucket]


class AggregationCountResponse(TypedDict, total=False):
    status: str
    total_hits: int
    page: int
    total_pages: int
    page_size: int
    aggregations: AggregationItem | list[AggregationItem]
    user_input: dict[str, Any]


class SubscriptionResponse(TypedDict, total=False):
    active: bool
    concurrent_calls: int
    plan: str
    plan_calls: int
    remaining_calls: int
    historical_days: int


# --- Validators --------------------------------------------------------------


def validate_choice(value: str | None, allowed: tuple[str, ...], field_name: str) -> None:
    """Raise ValueError if value is not None and not one of the allowed choices."""
    if value is not None and value not in allowed:
        raise ValueError(f"{field_name} must be one of {list(allowed)}, got {value!r}")


def validate_page_params(page: int | None, page_size: int | None, max_page_size: int = 1000) -> None:
    """Raise ValueError if page < 1 or page_size is outside [1, max_page_size]."""
    if page is not None and page < 1:
        raise ValueError(f"page must be >= 1, got {page}")
    if page_size is not None and not (1 <= page_size <= max_page_size):
        raise ValueError(f"page_size must be between 1 and {max_page_size}, got {page_size}")


def validate_search_in(values: list[str] | None) -> None:
    """Enforce only the confirmed business rule (max 2 values). search_in's
    documented values are examples on a plain string field, not a schema
    enum, so unrecognized values are intentionally not rejected here."""
    if values is None:
        return
    if len(values) > 2:
        raise ValueError(f"search_in accepts at most 2 values, got {len(values)}: {values}")


def validate_top_n_articles_page_size(top_n_articles: int | None, page_size: int | None) -> None:
    """Breaking News: top_n_articles * page_size must not exceed 1000 (confirmed constraint)."""
    if top_n_articles is None or page_size is None:
        return
    product = top_n_articles * page_size
    if product > 1000:
        raise ValueError(
            f"top_n_articles ({top_n_articles}) * page_size ({page_size}) = {product} exceeds the maximum of 1000"
        )


def validate_ids_or_links(ids: list[str] | None, links: list[str] | None) -> None:
    """search_by_link requires exactly one of ids/links -- documented as
    mutually exclusive ("either ... but not both")."""
    if ids and links:
        raise ValueError("Provide either ids or links, not both")
    if not ids and not links:
        raise ValueError("Provide one of ids or links")


def validate_sentiment_range(value: float | None, field_name: str) -> None:
    """Sentiment fields range from -1.0 to 1.0."""
    if value is not None and not (-1.0 <= value <= 1.0):
        raise ValueError(f"{field_name} must be between -1.0 and 1.0, got {value}")


def validate_rank(value: int | None, field_name: str) -> None:
    """from_rank/to_rank range from 1 to 999999 (lower rank = more popular source)."""
    if value is not None and not (1 <= value <= 999999):
        raise ValueError(f"{field_name} must be between 1 and 999999, got {value}")


def validate_clustering_threshold(value: float | None) -> None:
    """clustering_threshold is exclusive-min 0, inclusive-max 1."""
    if value is not None and not (0 < value <= 1):
        raise ValueError(f"clustering_threshold must be in (0, 1], got {value}")


def validate_sources_params(source_url: list[str] | None, include_additional_info: bool | None) -> None:
    """Sources endpoint: source_url (one or many domains -- used for bulk coverage
    checks) is only documented as combinable with include_additional_info."""
    if source_url is not None and not include_additional_info:
        raise ValueError("source_url can only be used together with include_additional_info=True")


def validate_sources_has_filter(
    lang: list[str] | None,
    countries: list[str] | None,
    predefined_sources: list[str] | None,
    source_name: str | None,
    source_url: list[str] | None,
    is_news_domain: bool | None,
    news_domain_type: str | None,
    news_type: list[str] | None,
    from_rank: int | None,
    to_rank: int | None,
) -> None:
    """The /sources endpoint documents that at least one filter parameter is
    required -- an empty-filter call isn't a valid "list everything" query here."""
    provided = (
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
    if all(value is None for value in provided):
        raise ValueError(
            "list_sources requires at least one filter parameter (e.g. lang, countries, "
            "source_name, source_url, is_news_domain, news_domain_type, news_type, from_rank, to_rank)"
        )


def flatten_custom_tags(custom_tags: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Expand the tool's ergonomic {"my_taxonomy": ["Tag1", "Tag2"]} shape into the
    wire format News API v3 actually expects: dynamic dotted top-level keys,
    {"custom_tags.my_taxonomy": ["Tag1", "Tag2"]}.

    This is the single highest-risk translation in this module: sending a naively
    nested {"custom_tags": {...}} body is *silently ignored* by the API (no error),
    so it has to be handled explicitly rather than passed through as-is. The
    flattening that hides this lives only inside NewsCatcher's own Python SDK
    samples, which this project deliberately doesn't depend on.
    """
    if not custom_tags:
        return {}
    return {f"custom_tags.{taxonomy}": tags for taxonomy, tags in custom_tags.items()}


def validate_clustering_date_range(clustering_enabled: bool | None, from_: str | None, to_: str | None) -> None:
    """Best-effort only: clustering hard-errors upstream if a query's date range spans
    both sides of 2026-01-01 (the article-embedding model changed that day). from_/to_
    also legally accept free-form natural language ("7 days ago"), so this silently
    no-ops on anything that doesn't parse as an ISO date/datetime -- the upstream API
    is the source of truth for natural-language ranges.
    """
    if not clustering_enabled or not from_ or not to_:
        return
    try:
        from_dt = datetime.fromisoformat(from_.strip().replace(" ", "T"))
        to_dt = datetime.fromisoformat(to_.strip().replace(" ", "T"))
    except ValueError:
        return
    if from_dt < CLUSTERING_MODEL_CUTOFF <= to_dt:
        raise ValueError(
            "clustering_enabled cannot be used with a date range spanning both before and "
            "after 2026-01-01 (from_/to_ straddle the boundary) -- narrow the range to one side"
        )


CLUSTERING_CUTOFF_DATE = CLUSTERING_MODEL_CUTOFF.date().isoformat()  # "2026-01-01" (after-half from_)
CLUSTERING_CUTOFF_PREV_DATE = (CLUSTERING_MODEL_CUTOFF - timedelta(days=1)).date().isoformat()  # "2025-12-31" (before-half to_)


def clustering_straddles_cutoff(from_: str | None, to_: str | None) -> bool:
    """True when a from_/to_ ISO date range spans across the 2026-01-01 clustering
    cutoff (from_ before it, to_ on/after it). Used to decide whether to split a
    clustered search into two boundary-safe halves. Silently False on non-ISO /
    natural-language ranges (the upstream API stays the source of truth)."""
    if not from_ or not to_:
        return False
    try:
        from_dt = datetime.fromisoformat(from_.strip().replace(" ", "T"))
        to_dt = datetime.fromisoformat(to_.strip().replace(" ", "T"))
    except ValueError:
        return False
    # Strict on the upper bound: a range ending exactly at 2026-01-01 00:00:00 is
    # accepted by the API (verified), so it is NOT a straddle and must not be split.
    return from_dt < CLUSTERING_MODEL_CUTOFF < to_dt


_Q_OPERATOR_TOKENS = {"AND", "OR", "NOT", "NEAR", "__QUOTED__", "__GROUP__"}
_QUOTED_SPAN_RE = re.compile(r'"[^"]*"')
_PAREN_GROUP_RE = re.compile(r"\([^()]*\)")
_TOKEN_RE = re.compile(r"[^\s()]+")


def lint_query(q: str) -> None:
    """Conservative q-syntax lint against News API v3's documented `q` grammar
    (see the Advanced Querying guide). This is NOT a full grammar parser -- the
    boolean/NEAR/phrase grammar has real depth, so this deliberately only rejects
    patterns the API is *documented* to always reject, rather than trying to
    second-guess anything more subtle (that risks false-positively rejecting a
    valid complex query, which is worse than letting an upstream 422 surface
    through the existing uniform error handling).

    Checks, in order:
    1. Non-empty.
    2. Forbidden literal characters `[ ] / \\ : ^` and their URL-encoded equivalents.
    3. A wildcard (`*` or `?`) leading any individual term -- checked per-term
       across the whole query, not just at the very start of the string (a leading
       wildcard is invalid wherever it occurs, e.g. "bitcoin OR *crypto"). The
       standalone match-all query `q="*"` is explicitly allowed.
    4. The single most common and highest-cost mistake: mixing an *unquoted*
       multi-word phrase with an explicit `OR`/`NOT` elsewhere in the query. The
       API auto-inserts `AND` between bare space-separated words, so
       `"AI OR artificial intelligence"` is actually parsed as
       `"AI OR artificial AND intelligence"` -- mixed operators at the same
       level with no explicit grouping, which is a documented, unconditional 422.
       This check flags the common flat (non-nested) form of that mistake before
       it ever reaches the API.
    """
    if not q or not q.strip():
        raise ValueError("q must not be empty")
    for ch in _FORBIDDEN_Q_CHARS:
        if ch in q:
            raise ValueError(f"q contains a forbidden character {ch!r} ({list(_FORBIDDEN_Q_CHARS)} are not allowed)")
    for enc in _FORBIDDEN_Q_ENCODED:
        if enc in q:
            raise ValueError(f"q contains a forbidden URL-encoded character {enc!r}")

    # Exact-match phrases are exempt from the wildcard/word-adjacency checks below --
    # replace each "..." span with a single opaque placeholder token before scanning.
    unquoted = _QUOTED_SPAN_RE.sub(" __QUOTED__ ", q)
    tokens = _TOKEN_RE.findall(unquoted)

    for term in tokens:
        if term != "__QUOTED__" and len(term) > 1 and term[0] in "*?":
            raise ValueError(
                f"q contains a wildcard at the start of a term ({term!r}) -- wildcards (* and ?) "
                "cannot lead a term. A wildcard elsewhere in the term is fine (e.g. 'technolog*'); "
                "a bare '*' on its own (match-all) is also fine."
            )

    # The flat-mixed-operator check below must NOT fire on *grouped* queries:
    # parentheses and quoted phrases are valid grouping the API accepts, e.g.
    # '(natural gas) AND (demand OR supply)' or 'AI OR "artificial intelligence"'.
    # Collapse both quoted spans and balanced parenthesised groups (innermost-out,
    # to handle nesting) to opaque placeholder tokens so only ungrouped, top-level
    # bare-word runs remain to be judged. A mistake nested *inside* a group is left
    # to surface as an upstream 422 -- this lint only claims the common flat form
    # (see the docstring), and false-positively rejecting a valid grouped query is
    # worse than letting a rarer nested one through.
    reduced = _QUOTED_SPAN_RE.sub(" __QUOTED__ ", q)
    prev = None
    while prev != reduced:
        prev = reduced
        reduced = _PAREN_GROUP_RE.sub(" __GROUP__ ", reduced)
    flat_tokens = _TOKEN_RE.findall(reduced)

    has_or_or_not = any(token.upper() in ("OR", "NOT") for token in flat_tokens)
    if has_or_or_not:
        consecutive_bare_words = 0
        for token in flat_tokens:
            is_operator_or_quoted = token.upper() in _Q_OPERATOR_TOKENS or token.startswith(("+", "-"))
            if is_operator_or_quoted:
                consecutive_bare_words = 0
                continue
            consecutive_bare_words += 1
            if consecutive_bare_words >= 2:
                raise ValueError(
                    "q mixes an unquoted multi-word phrase with an explicit OR/NOT operator -- the API "
                    "auto-inserts AND between bare words, so e.g. 'AI OR artificial intelligence' is "
                    "actually parsed as 'AI OR artificial AND intelligence' (mixed operators at the same "
                    "level, no grouping), which the API always rejects with a 422. Quote the multi-word "
                    'phrase instead, e.g. q=\'AI OR "artificial intelligence"\', or add explicit '
                    "parentheses around each side."
                )
