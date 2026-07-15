# Newscatcher News API MCP Server

MCP server for the NewsCatcher News API (v3) — search, latest headlines, breaking
news, author search, article lookup, source browsing, aggregation counts, and
subscription/quota info.

## Quick Start

There is no hosted deployment of this server yet — run it yourself:

```bash
git clone <this-repo>
cd news-mcp
pip install -r requirements.txt
NEWS_API_KEY=your_token python server.py
```

Get a News API token at [newscatcherapi.com](https://www.newscatcherapi.com/). Then
connect an MCP client to `http://localhost:8000/mcp`:

```json
{
  "mcpServers": {
    "news": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "x-api-token": "YOUR_API_TOKEN" }
    }
  }
}
```

Or via Claude Code CLI:

```bash
claude mcp add --transport http news "http://localhost:8000/mcp" --header "x-api-token: YOUR_API_TOKEN"
```

<!-- Once a hosted instance exists, replace the above with:
https://news-mcp.newscatcherapi.com/mcp?apiToken=YOUR_API_TOKEN
-->

See [Running](#running) below for the other ways to start the server.

## Tool To Endpoint Mapping

| MCP Tool | Endpoint |
| --- | --- |
| `search_articles` | `POST /api/search` |
| `get_latest_headlines` | `POST /api/latest_headlines` |
| `get_breaking_news` | `POST /api/breaking_news` |
| `search_by_author` | `POST /api/authors` |
| `search_by_link` | `POST /api/search_by_link` |
| `list_sources` | `POST /api/sources` |
| `get_aggregation_count` | `POST /api/aggregation_count` |
| `get_subscription` | `POST /api/subscription` |
| `check_health` | *(local liveness ping — no upstream call)* |

Every tool internally uses `POST` even though the News API v3 also supports `GET`
for each of these endpoints — POST is what NewsCatcher's own docs recommend for
production use (no URL-length limit, keeps API tokens and queries out of access
logs, and lets multi-value filters be sent as native JSON arrays).

There is no `search_similar` tool — News API v3 has no such endpoint. Reach
similarity via `clustering_enabled`/`clustering_threshold` (search/latest
headlines only), `exclude_duplicates` (search only), or by requesting
`include_nlp_data=true` and comparing the returned embeddings yourself.

## Authentication

API token precedence (highest to lowest):

1. `api_token` tool parameter
2. `x-api-token` request header
3. `Authorization: Bearer <token>` request header
4. URL query parameter `?apiToken=...`
5. `NEWS_API_KEY` environment variable

`check_health` is the only tool that does not require an API token — it's a local
liveness ping that never calls the News API (News API v3 has no public
health/version endpoint).

### Hosted deployment (FastMCP Gateway)

If you deploy this behind a stateless gateway (e.g. fastmcp.app), the gateway
forwards HTTP headers to the backend but **not** URL query parameters. Use the
`x-api-token` header or `NEWS_API_KEY` environment variable instead of `?apiToken=`.

```json
{
  "mcpServers": {
    "news": {
      "type": "http",
      "url": "https://YOUR-DEPLOYMENT.fastmcp.app/mcp",
      "headers": { "x-api-token": "YOUR_API_TOKEN" }
    }
  }
}
```

**Direct server access** (no gateway): `?apiToken=YOUR_TOKEN` in the URL still works.

## Configuration

- `NEWS_API_BASE_URL` — overrides the upstream News API v3 base URL (defaults to
  `https://v3-api.newscatcherapi.com`). Only needed to point at a non-default
  environment.

## Query Workflow Tips

- **Defaults favor richer results.** `search_articles` defaults `clustering_enabled`,
  `exclude_duplicates`, and `include_nlp_data` to `true`; `get_latest_headlines`,
  `get_breaking_news`, and `search_by_author` default `include_nlp_data` to `true`.
  Pass `false` explicitly to opt out of any of these.
  - `clustering_enabled=true` changes the response shape to `clusters_count` +
    `clusters` (each `{cluster_id, cluster_size, articles}`) instead of a flat
    `articles` list — pass `clustering_enabled=false` for a plain list.
  - Clustering and `exclude_duplicates` solve the same near-duplicate-coverage
    problem differently rather than being strictly complementary: clustering
    groups related articles (all kept, reorganized into groups), `exclude_duplicates`
    removes near-identical ones (fewer articles, stays flat).
  - `clustering_variable` is deprecated (ignored) for articles published on or
    after 2026-01-01; a date range cannot straddle 2026-01-01 when clustering is
    enabled — the API rejects it.
- **Check quota first.** `get_subscription` returns your plan tier, monthly quota,
  and remaining calls — useful before running a large batch of searches, and the
  only way to confirm your token is valid.
- **Gauge volume before a big pull.** There is a hard cap of **10,000 articles per
  query** regardless of pagination — if `total_hits` reads exactly `10000`, the
  true match count is likely higher. Call `get_aggregation_count` on a broad or
  undated `search_articles` query first, and time-chunk the date range if needed.
- **Pagination.** `page_size` maxes out at 1000. Server-side request timeout is
  30s — a `408` means narrow the query, shorten the date range, or lower `page_size`.
  Clustering operates one page at a time, so raise `page_size` to at least your
  expected result count for coherent clusters.
- **Historical data.** Data is indexed monthly and goes back to 2019; NLP
  enrichment only covers articles indexed from July 2023 onward (earlier articles
  still return an `nlp` object, just empty `{}` — use `has_nlp=true` to filter to
  NLP-enriched articles only). Don't query multiple years in a single call.
  Measure first, then chunk: call `get_aggregation_count` with `aggregation_by="day"`
  over your target range, then pick a chunk size from the measured density (see
  table below) and page through each chunk before moving to the next. If a chunk
  still times out (`408`), step the size down (`"1d"` → `"6h"` → `"1h"`). Use a
  fixed `to_` date (not the default `"now"`) for reproducible pulls — an
  open-ended `to_` means results shift between runs as new articles get indexed.
- **Rate limits.** On `429`, back off and retry rather than repeating the same
  request immediately — check `get_subscription` for your concurrency/quota limits.

**Chunk size by measured density** (from an `aggregation_by="day"` call):

| Articles per period | Chunk size |
| --- | --- |
| More than 10,000/hour | `"1h"` (consider narrowing the query) |
| More than 10,000/day | `"6h"` or `"1h"` |
| 3,000–10,000/day | `"1d"` |
| 1,000–3,000/day | `"3d"` |
| 100–1,000/day | `"7d"` |
| Fewer than 100/day | `"30d"` |
- **Custom tags.** Pass `custom_tags` as `{"taxonomy_name": ["Tag1", "Tag2"]}` —
  this server translates it to News API v3's actual wire format (dynamic dotted
  keys, `custom_tags.taxonomy_name`) for you. Only usable if your organization has
  custom tags configured on your token.
- **Checking source coverage.** `list_sources` requires at least one filter
  parameter (e.g. `lang`, `countries`, `source_name`, `source_url`, ...) — there's
  no "list everything" call. Pass a list of domains to `source_url` (with
  `include_additional_info=true`) to bulk-check coverage for many domains in one
  call instead of one call per domain; any domain absent from the response's
  `sources` isn't covered.

## Entity Search and Multilingual Coverage

`org_entity_name`/`per_entity_name`/`loc_entity_name`/`misc_entity_name` (and
`ner_name` on `search_by_author`) support the same `AND`/`OR`/`NOT`/`NEAR` syntax as
`q`, plus one more operator: `COUNT("Entity Name", n, "gt")` filters to articles
mentioning that entity more than `n` times — a proxy for how central the entity is,
not just a passing mention. Combine with `include_nlp_data=true` (the default) to
see actual mention counts in `nlp.ner_*`.

News API translates non-English articles to English at index time (translation
fields available for articles published from 2025-03-12 onward), so entity names
and keywords work across languages using their English form:

- Set `search_in=["title_content", "title_content_translated"]` to search both
  original and translated text in one call.
- Omit `lang` to search across all languages; use `countries` to focus on specific
  regions instead.
- Use official English names in quotes, e.g.
  `org_entity_name='"European Union" OR "European Commission"'` also matches
  "Union européenne"/"Unión Europea" in French/Spanish articles.
- Set `include_translation_fields=true` to get `title_translated_en`/
  `content_translated_en` and `nlp.translation_summary`/`nlp.translation_ner_*`
  back on each result (the `translation_ner_*` fields also need
  `include_nlp_data=true`, which is on by default).

## Query Syntax (`q` parameter)

Used by `search_articles` and `get_aggregation_count`. The API auto-inserts `AND`
between bare, unquoted, space-separated words — this is the single most common
source of broken queries.

> **Always quote multi-word phrases.** `q="AI OR artificial intelligence"` is
> actually parsed as `"AI OR artificial AND intelligence"` (an implicit `AND`
> collides with your explicit `OR` at the same level, with no grouping) — the API
> rejects this with a `422`. Fix: quote the phrase,
> `q='AI OR "artificial intelligence"'`, or add explicit parentheses around each
> side. This server validates the common flat form of this mistake client-side
> before it ever reaches the API.

| Goal | Query string |
| --- | --- |
| Both terms present | `bitcoin AND blockchain` |
| Either term | `bitcoin OR cryptocurrency` |
| Exclude a term | `Tesla NOT "Elon Musk"` |
| Combine groups | `(bitcoin OR cryptocurrency) AND (investment OR trading)` |
| Exact phrase | `"electric vehicles" NOT Tesla` |
| Require/exclude shorthand | `+Apple -Google` |
| Wildcard | `technolog*` matches technology, technological, technologies |
| Proximity | `NEAR("climate change", "renewable energy", 15)` |
| Match everything (filters only) | `*` |

- Wildcards (`*` any-length, `?` single character) cannot lead a term —
  `"*intelligence"` is invalid, `"technolog*"` is fine.
- `NEAR("phrase_a", "phrase_b", distance, in_order)`: max 4 words per phrase, max
  100 words distance, `in_order` optional (default `false`).
- Forbidden characters, never valid anywhere in `q`: `[ ] / \ : ^` (and their
  URL-encoded equivalents).
- If results look wrong, check the response's `user_input.q` to see how the API
  actually parsed your query. Then refine: too broad → add `AND` terms, narrow
  with `NOT`, or reduce the `NEAR` distance; too few → broaden with `OR`, increase
  the `NEAR` distance, or use a wildcard.

## Error Handling

Tools return:

- Pretty JSON string on success.
- `"Error: ..."` for validation/API errors.
- `"Unexpected error: ..."` for unhandled exceptions.

## Running

Install dependencies:

```bash
pip install -r requirements.txt
```

Run over streamable-HTTP:

```bash
python server.py
```

Or, if the `fastmcp` CLI is available:

```bash
fastmcp run server.py:mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Or via uvicorn directly (the module exposes an ASGI `app` for this):

```bash
uvicorn server:app
```

## Testing

```bash
# Unit tests (no network / no key needed)
pip install -r requirements-test.txt
pytest tests/test_server.py -v

# Integration tests against a locally running server
NEWS_API_KEY=your_token python server.py &
NEWS_API_KEY=your_token pytest tests/integration/ -v -s
```
