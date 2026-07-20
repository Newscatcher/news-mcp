import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _install_test_stubs() -> None:
    """Install lightweight stubs when runtime deps are unavailable."""
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        httpx_module = types.ModuleType("httpx")

        class AsyncClient:  # pragma: no cover - only used in fallback envs
            def __init__(self, *args, **kwargs):
                raise RuntimeError("httpx.AsyncClient stub should be patched in tests.")

        httpx_module.AsyncClient = AsyncClient
        sys.modules["httpx"] = httpx_module

    try:
        import fastmcp  # noqa: F401
    except ModuleNotFoundError:
        fastmcp_module = types.ModuleType("fastmcp")
        http_module = types.ModuleType("fastmcp.server.http")
        server_module = types.ModuleType("fastmcp.server")

        class FastMCP:  # pragma: no cover - only used in fallback envs
            def __init__(self, *args, **kwargs):
                pass

            def tool(self):
                def decorator(func):
                    return func

                return decorator

            def http_app(self, *args, **kwargs):
                return None

            def run(self, *args, **kwargs):
                pass

        import contextvars as _contextvars

        http_module._current_http_request = _contextvars.ContextVar("_current_http_request", default=None)

        fastmcp_module.FastMCP = FastMCP

        sys.modules["fastmcp"] = fastmcp_module
        sys.modules["fastmcp.server"] = server_module
        sys.modules["fastmcp.server.http"] = http_module

    try:
        import starlette.middleware  # noqa: F401
    except ModuleNotFoundError:
        starlette_module = types.ModuleType("starlette")
        middleware_module = types.ModuleType("starlette.middleware")

        class Middleware:  # pragma: no cover - only used in fallback envs
            def __init__(self, *args, **kwargs):
                pass

        middleware_module.Middleware = Middleware
        sys.modules["starlette"] = starlette_module
        sys.modules["starlette.middleware"] = middleware_module


_install_test_stubs()

import server
import validators


class DummyResponse:
    def __init__(self, payload: dict | None, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        if self._payload is None:
            raise json.JSONDecodeError("no payload", "", 0)
        return self._payload


class DummyAsyncClient:
    def __init__(self, payload: dict | None, status_code: int = 200, text: str = "") -> None:
        self._response = DummyResponse(payload, status_code, text)
        self.post_kwargs: dict | None = None
        self.client_kwargs: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, path: str, headers: dict | None = None, json=None):
        self.post_kwargs = {"path": path, "headers": headers, "json": json}
        return self._response


class AsyncClientFactory:
    def __init__(self, payload: dict | None, status_code: int = 200, text: str = "") -> None:
        self.client = DummyAsyncClient(payload, status_code, text)

    def __call__(self, *args, **kwargs):
        self.client.client_kwargs = kwargs
        return self.client


class ApiTokenPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.get("NEWS_API_KEY")
        self._token = server.session_api_token.set("")
        os.environ.pop("NEWS_API_KEY", None)

    def tearDown(self) -> None:
        server.session_api_token.reset(self._token)
        if self._old_env is None:
            os.environ.pop("NEWS_API_KEY", None)
        else:
            os.environ["NEWS_API_KEY"] = self._old_env

    def test_get_api_token_precedence(self) -> None:
        os.environ["NEWS_API_KEY"] = "env_token"
        session_token = server.session_api_token.set("session_token")
        try:
            self.assertEqual(server.get_api_token("explicit_token"), "explicit_token")
            self.assertEqual(server.get_api_token(""), "session_token")
        finally:
            server.session_api_token.reset(session_token)

        self.assertEqual(server.get_api_token(""), "env_token")

    def test_get_api_token_missing_raises(self) -> None:
        with self.assertRaises(ValueError):
            server.get_api_token("")


class ApiRequestAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_env = os.environ.get("NEWS_API_KEY")
        self._token = server.session_api_token.set("")
        os.environ.pop("NEWS_API_KEY", None)

    async def asyncTearDown(self) -> None:
        server.session_api_token.reset(self._token)
        if self._old_env is None:
            os.environ.pop("NEWS_API_KEY", None)
        else:
            os.environ["NEWS_API_KEY"] = self._old_env

    async def test_make_api_request_sends_x_api_token_header(self) -> None:
        """News API v3's auth header is x-api-token."""
        factory = AsyncClientFactory({"active": True})
        with patch("server.httpx.AsyncClient", side_effect=factory):
            result = await server.make_api_request(api_token="my_token", path="/api/subscription", json_data=None)

        self.assertEqual(result, {"active": True})
        assert factory.client.post_kwargs is not None
        self.assertEqual(factory.client.post_kwargs["headers"]["x-api-token"], "my_token")

    async def test_make_api_request_requires_token(self) -> None:
        with patch("server.httpx.AsyncClient") as mock_client:
            with self.assertRaises(ValueError):
                await server.make_api_request(api_token="", path="/api/search", json_data={"q": "test"})
        mock_client.assert_not_called()

    async def test_make_api_request_empty_body_returns_empty_dict(self) -> None:
        """API returns 200 with an empty body (seen on some list-style endpoints)."""
        factory = AsyncClientFactory(None, status_code=200, text="")
        with patch("server.httpx.AsyncClient", side_effect=factory):
            result = await server.make_api_request(api_token="token", path="/api/subscription", json_data=None)
        self.assertEqual(result, {})

    async def test_make_api_request_error_envelope_uses_message_field(self) -> None:
        """News API's error envelope is {message, status_code, status}."""
        factory = AsyncClientFactory(
            {"message": "Invalid query", "status_code": 422, "status": "Unprocessable Entity"},
            status_code=422,
        )
        with patch("server.httpx.AsyncClient", side_effect=factory):
            with self.assertRaises(ValueError) as ctx:
                await server.make_api_request(api_token="token", path="/api/search", json_data={"q": "bad[query]"})
        self.assertIn("Invalid query", str(ctx.exception))
        self.assertIn("422", str(ctx.exception))

    async def test_make_api_request_non_json_error_falls_back_to_text(self) -> None:
        """News API's 500 responses are text/plain, not JSON."""
        factory = AsyncClientFactory(None, status_code=500, text="Internal Server Error")
        with patch("server.httpx.AsyncClient", side_effect=factory):
            with self.assertRaises(ValueError) as ctx:
                await server.make_api_request(api_token="token", path="/api/search", json_data={"q": "x"})
        self.assertIn("Internal Server Error", str(ctx.exception))
        self.assertIn("500", str(ctx.exception))


class ValidationHelperTests(unittest.TestCase):
    def test_validate_choice(self) -> None:
        validators.validate_choice("date", validators.SORT_BY_VALUES, "sort_by")
        validators.validate_choice(None, validators.SORT_BY_VALUES, "sort_by")
        with self.assertRaises(ValueError):
            validators.validate_choice("popularity", validators.SORT_BY_VALUES, "sort_by")

    def test_validate_page_params(self) -> None:
        validators.validate_page_params(1, 100)
        validators.validate_page_params(None, None)
        with self.assertRaises(ValueError):
            validators.validate_page_params(0, 100)
        with self.assertRaises(ValueError):
            validators.validate_page_params(1, 1001)
        with self.assertRaises(ValueError):
            validators.validate_page_params(1, 0)

    def test_validate_search_in(self) -> None:
        validators.validate_search_in(["title", "content"])
        validators.validate_search_in(None)
        with self.assertRaises(ValueError):
            validators.validate_search_in(["title", "content", "summary"])

    def test_validate_lang(self) -> None:
        # valid codes (either casing) and empty inputs are accepted
        validators.validate_lang(["en", "es", "de"])
        validators.validate_lang(["EN"])
        validators.validate_lang(["cn", "tw"])  # NewsCatcher Chinese codes
        validators.validate_lang(None)
        validators.validate_lang([])
        # the real-world error classes are rejected
        with self.assertRaises(ValueError):
            validators.validate_lang(["english"])  # full name
        with self.assertRaises(ValueError):
            validators.validate_lang(["zh"])  # ISO Chinese, not accepted upstream
        with self.assertRaises(ValueError):
            validators.validate_lang(["en", "xx"])  # one bad code in a list

    def test_validate_lang_hint_mentions_chinese_deviation(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validators.validate_lang(["zh"])
        self.assertIn("cn", str(ctx.exception))
        self.assertIn("zh", str(ctx.exception))

    def test_validate_country(self) -> None:
        validators.validate_country(["US", "GB", "DE"])
        validators.validate_country(["us"])  # lowercase accepted
        validators.validate_country(None)
        validators.validate_country([])
        with self.assertRaises(ValueError):
            validators.validate_country(["USA"])  # 3-letter
        with self.assertRaises(ValueError):
            validators.validate_country(["UK"])  # common mistake -> GB
        with self.assertRaises(ValueError):
            validators.validate_country(["XK"])  # user-assigned, not officially ISO
        with self.assertRaises(ValueError):
            validators.validate_country(["United States"])  # full name

    def test_lang_country_code_lists_wellformed(self) -> None:
        # every code is a distinct two-letter token; the Chinese deviation holds
        self.assertTrue(all(len(c) == 2 for c in validators.LANG_CODES))
        self.assertTrue(all(len(c) == 2 for c in validators.COUNTRY_CODES))
        self.assertIn("cn", validators.LANG_CODES)
        self.assertNotIn("zh", validators.LANG_CODES)

    def test_validate_top_n_articles_page_size(self) -> None:
        validators.validate_top_n_articles_page_size(10, 100)
        validators.validate_top_n_articles_page_size(None, 100)
        with self.assertRaises(ValueError):
            validators.validate_top_n_articles_page_size(20, 100)

    def test_validate_ids_or_links(self) -> None:
        validators.validate_ids_or_links(["id1"], None)
        validators.validate_ids_or_links(None, ["https://example.com/a"])
        with self.assertRaises(ValueError):
            validators.validate_ids_or_links(["id1"], ["https://example.com/a"])
        with self.assertRaises(ValueError):
            validators.validate_ids_or_links(None, None)

    def test_validate_sentiment_range(self) -> None:
        validators.validate_sentiment_range(-1.0, "title_sentiment_min")
        validators.validate_sentiment_range(1.0, "title_sentiment_min")
        validators.validate_sentiment_range(None, "title_sentiment_min")
        with self.assertRaises(ValueError):
            validators.validate_sentiment_range(1.5, "title_sentiment_min")

    def test_validate_rank(self) -> None:
        validators.validate_rank(1, "from_rank")
        validators.validate_rank(999999, "from_rank")
        validators.validate_rank(None, "from_rank")
        with self.assertRaises(ValueError):
            validators.validate_rank(0, "from_rank")
        with self.assertRaises(ValueError):
            validators.validate_rank(1000000, "from_rank")

    def test_validate_clustering_threshold(self) -> None:
        validators.validate_clustering_threshold(0.7)
        validators.validate_clustering_threshold(1.0)
        validators.validate_clustering_threshold(None)
        with self.assertRaises(ValueError):
            validators.validate_clustering_threshold(0)
        with self.assertRaises(ValueError):
            validators.validate_clustering_threshold(1.5)

    def test_validate_sources_params(self) -> None:
        validators.validate_sources_params(None, None)
        validators.validate_sources_params(["bbc.com"], True)
        validators.validate_sources_params(["bbc.com", "cnn.com"], True)
        with self.assertRaises(ValueError):
            validators.validate_sources_params(["bbc.com"], None)
        with self.assertRaises(ValueError):
            validators.validate_sources_params(["bbc.com"], False)

    def test_validate_sources_has_filter(self) -> None:
        validators.validate_sources_has_filter(
            ["en"], None, None, None, None, None, None, None, None, None
        )
        validators.validate_sources_has_filter(
            None, None, None, None, None, False, None, None, None, None
        )
        with self.assertRaises(ValueError):
            validators.validate_sources_has_filter(
                None, None, None, None, None, None, None, None, None, None
            )

    def test_flatten_custom_tags(self) -> None:
        self.assertEqual(validators.flatten_custom_tags(None), {})
        self.assertEqual(validators.flatten_custom_tags({}), {})
        self.assertEqual(
            validators.flatten_custom_tags({"my_taxonomy": ["Tag1", "Tag2"]}),
            {"custom_tags.my_taxonomy": ["Tag1", "Tag2"]},
        )

    def test_validate_clustering_date_range(self) -> None:
        validators.validate_clustering_date_range(True, "2025-06-01", "2025-06-30")
        validators.validate_clustering_date_range(True, "7 days ago", "now")
        validators.validate_clustering_date_range(False, "2025-06-01", "2026-06-30")
        validators.validate_clustering_date_range(None, None, None)
        with self.assertRaises(ValueError):
            validators.validate_clustering_date_range(True, "2025-12-01", "2026-01-15")

    def test_lint_query(self) -> None:
        validators.lint_query("bitcoin AND ethereum")
        validators.lint_query("*")
        validators.lint_query('"Tim Cook"')
        validators.lint_query("(bitcoin OR cryptocurrency) AND (investment OR trading)")
        validators.lint_query('Tesla NOT "Elon Musk"')
        validators.lint_query("+Apple -Google")
        validators.lint_query('NEAR("climate change", "renewable energy", 15)')
        with self.assertRaises(ValueError):
            validators.lint_query("")
        with self.assertRaises(ValueError):
            validators.lint_query("bad[query]")
        with self.assertRaises(ValueError):
            validators.lint_query("*intelligence")
        with self.assertRaises(ValueError):
            validators.lint_query("bitcoin OR *crypto")

    def test_lint_query_catches_unquoted_phrase_mixed_with_or_not(self) -> None:
        """The documented "AI OR artificial intelligence" -> 422 gotcha."""
        with self.assertRaises(ValueError) as ctx:
            validators.lint_query("AI OR artificial intelligence")
        self.assertIn("unquoted multi-word phrase", str(ctx.exception))
        # Quoting the phrase (or using a single bare word) fixes it:
        validators.lint_query('AI OR "artificial intelligence"')
        validators.lint_query("bitcoin OR cryptocurrency")
        with self.assertRaises(ValueError):
            validators.lint_query('"electric vehicles" NOT Tesla is great')


def _unwrap(tool_func):
    """Return the underlying async function from a FastMCP FunctionTool wrapper."""
    return tool_func.fn if hasattr(tool_func, "fn") else tool_func


class ToolBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_tool_call(self, tool_func, tool_kwargs: dict, expected_path: str, expected_json) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            result = await _unwrap(tool_func)(**tool_kwargs)

        self.assertEqual(result, json.dumps({"ok": True}, indent=2))
        mock_api.assert_awaited_once()
        called = mock_api.await_args.kwargs
        self.assertEqual(called["path"], expected_path)
        self.assertEqual(called.get("json_data"), expected_json)

    async def test_tool_request_mapping(self) -> None:
        # search_articles/get_latest_headlines/get_breaking_news/search_by_author default
        # clustering_enabled/exclude_duplicates/include_nlp_data to True -- these keys are
        # expected in every request body for those tools even when the caller omits them.
        cases = [
            (
                server.search_articles,
                {"q": "acquisitions"},
                "/api/search",
                {
                    "q": "acquisitions",
                    "page": 1,
                    "page_size": 100,
                    "clustering_enabled": True,
                    "include_nlp_data": True,
                    "exclude_duplicates": True,
                },
            ),
            (
                server.search_articles,
                {"q": "ai", "lang": ["en", "es"], "page": 2, "page_size": 50, "sort_by": "date"},
                "/api/search",
                {
                    "q": "ai",
                    "page": 2,
                    "page_size": 50,
                    "lang": ["en", "es"],
                    "sort_by": "date",
                    "clustering_enabled": True,
                    "include_nlp_data": True,
                    "exclude_duplicates": True,
                },
            ),
            (
                server.get_latest_headlines,
                {},
                "/api/latest_headlines",
                {"page": 1, "page_size": 100, "clustering_enabled": True, "include_nlp_data": True},
            ),
            (
                server.get_latest_headlines,
                {"when": "24h", "countries": ["US"]},
                "/api/latest_headlines",
                {
                    "page": 1,
                    "page_size": 100,
                    "when": "24h",
                    "countries": ["US"],
                    "clustering_enabled": True,
                    "include_nlp_data": True,
                },
            ),
            (
                server.get_breaking_news,
                {},
                "/api/breaking_news",
                {"page": 1, "page_size": 100, "include_nlp_data": True},
            ),
            (
                server.get_breaking_news,
                {"top_n_articles": 5, "sort_by": "rank"},
                "/api/breaking_news",
                {"page": 1, "page_size": 100, "top_n_articles": 5, "sort_by": "rank", "include_nlp_data": True},
            ),
            (
                server.search_by_author,
                {"author_name": "Jane Doe"},
                "/api/authors",
                {"author_name": "Jane Doe", "page": 1, "page_size": 100, "include_nlp_data": True},
            ),
            (
                server.search_by_link,
                {"ids": ["abc123"]},
                "/api/search_by_link",
                {"page": 1, "page_size": 100, "ids": ["abc123"]},
            ),
            (
                server.search_by_link,
                {"links": ["https://example.com/article"]},
                "/api/search_by_link",
                {"page": 1, "page_size": 100, "links": ["https://example.com/article"]},
            ),
            (
                server.list_sources,
                {"source_url": ["bbc.com"], "include_additional_info": True},
                "/api/sources",
                {"source_url": ["bbc.com"], "include_additional_info": True},
            ),
            (
                server.list_sources,
                {"source_url": ["bbc.com", "cnn.com"], "include_additional_info": True},
                "/api/sources",
                {"source_url": ["bbc.com", "cnn.com"], "include_additional_info": True},
            ),
            (
                server.get_aggregation_count,
                {"q": "climate change"},
                "/api/aggregation_count",
                {"q": "climate change", "page": 1, "page_size": 100},
            ),
            (server.get_subscription, {}, "/api/subscription", None),
        ]
        for tool_func, kwargs, expected_path, expected_json in cases:
            with self.subTest(tool=tool_func.__name__, kwargs=kwargs):
                await self._assert_tool_call(tool_func, kwargs, expected_path, expected_json)

    async def test_search_articles_defaults_can_be_disabled(self) -> None:
        """Passing False explicitly overrides the clustering/dedup/NLP defaults."""
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            await _unwrap(server.search_articles)(
                q="ai", clustering_enabled=False, include_nlp_data=False, exclude_duplicates=False
            )
        called = mock_api.await_args.kwargs["json_data"]
        self.assertEqual(called["clustering_enabled"], False)
        self.assertEqual(called["include_nlp_data"], False)
        self.assertEqual(called["exclude_duplicates"], False)

    async def test_get_aggregation_count_does_not_default_include_nlp_data(self) -> None:
        """Unlike the article-returning tools, get_aggregation_count returns no
        articles, so include_nlp_data is left as None/omitted rather than True."""
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            await _unwrap(server.get_aggregation_count)(q="ai")
        called = mock_api.await_args.kwargs["json_data"]
        self.assertNotIn("include_nlp_data", called)

    async def test_check_health_never_calls_make_api_request(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            result = await _unwrap(server.check_health)()
        mock_api.assert_not_called()
        self.assertEqual(json.loads(result), {"status": "ok", "server": "news-mcp"})

    async def test_custom_tags_flattened_in_request_body(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            await _unwrap(server.search_articles)(q="ai", custom_tags={"my_taxonomy": ["Tag1", "Tag2"]})
        called = mock_api.await_args.kwargs
        self.assertEqual(called["json_data"]["custom_tags.my_taxonomy"], ["Tag1", "Tag2"])
        self.assertNotIn("custom_tags", called["json_data"])

    async def test_entity_name_params_translated_to_upstream_keys(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            await _unwrap(server.search_articles)(q="ai", org_entity_name="Acme Corp")
        called = mock_api.await_args.kwargs
        self.assertEqual(called["json_data"]["ORG_entity_name"], "Acme Corp")
        self.assertNotIn("org_entity_name", called["json_data"])

    async def test_tool_validations_fail_early(self) -> None:
        invalid_calls = [
            (server.search_articles, {"q": ""}, "must not be empty"),
            (server.search_articles, {"q": "bad[query]"}, "forbidden character"),
            (server.search_articles, {"q": "bitcoin OR *crypto"}, "wildcard at the start of a term"),
            (server.search_articles, {"q": "AI OR artificial intelligence"}, "unquoted multi-word phrase"),
            (server.get_aggregation_count, {"q": "AI OR artificial intelligence"}, "unquoted multi-word phrase"),
            (server.search_articles, {"q": "ai", "page": 0}, "page must be >= 1"),
            (server.search_articles, {"q": "ai", "page_size": 1001}, "page_size must be between"),
            (server.search_articles, {"q": "ai", "sort_by": "popularity"}, "sort_by must be one of"),
            (server.search_articles, {"q": "ai", "news_domain_type": "Blog"}, "news_domain_type must be one of"),
            (server.search_articles, {"q": "ai", "clustering_variable": "body"}, "clustering_variable must be one of"),
            (server.search_articles, {"q": "ai", "search_in": ["title", "content", "summary"]}, "at most 2 values"),
            (server.search_articles, {"q": "ai", "title_sentiment_min": 2.0}, "must be between -1.0 and 1.0"),
            (server.search_articles, {"q": "ai", "clustering_threshold": 0}, "must be in (0, 1]"),
            (server.search_articles, {"q": "ai", "from_rank": 0}, "must be between 1 and 999999"),
            (server.get_breaking_news, {"top_n_articles": 20}, "exceeds the maximum of 1000"),
            (server.get_aggregation_count, {"q": "ai", "aggregation_by": "week"}, "aggregation_by must be one of"),
            (server.search_by_link, {"ids": ["a"], "links": ["https://x.com"]}, "not both"),
            (server.search_by_link, {}, "Provide one of ids or links"),
            (server.list_sources, {}, "requires at least one filter parameter"),
            (server.list_sources, {"source_url": ["bbc.com"]}, "only be used together with"),
            # NB: a clustered search straddling 2026-01-01 no longer fails early -- it is
            # split at the boundary and merged (see ClusteringStraddleTests + integration).
        ]
        for tool_func, kwargs, expected_message_fragment in invalid_calls:
            with self.subTest(tool=tool_func.__name__, kwargs=kwargs):
                with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
                    result = await _unwrap(tool_func)(**kwargs)
                mock_api.assert_not_called()
                self.assertTrue(result.startswith("Error: "), result)
                self.assertIn(expected_message_fragment, result)


class GroupedQueryLintTests(unittest.TestCase):
    """F0: lint_query must NOT reject valid parenthesised / quoted grouped queries
    (real production queries were being wrongly blocked), while still catching the
    flat unquoted-phrase-with-OR/NOT mistake."""

    VALID_GROUPED = [
        "(natural gas) AND (demand OR supply OR futures OR producer)",
        "((World Cup 2026) OR (FIFA World Cup 2026) OR (World Cup soccer))",
        '("pediatric" OR "pediatrician" OR "child health") NOT "global newswire"',
        "(government robotics) OR (robotics)",
        "Germany ransomware attack AND (manufacturing OR healthcare OR defense)",
        'AI OR "artificial intelligence"',
        "((Taylor Swift) OR (Taylor Swift's) OR (Taylor AND Swift)) NOT crossword",
    ]
    STILL_INVALID = [
        "AI OR artificial intelligence",
        "apple OR google news",
        "Tesla NOT elon musk",
    ]

    def test_grouped_queries_allowed(self) -> None:
        for q in self.VALID_GROUPED:
            with self.subTest(q=q):
                validators.lint_query(q)  # must not raise

    def test_flat_mixed_operator_still_caught(self) -> None:
        for q in self.STILL_INVALID:
            with self.subTest(q=q):
                with self.assertRaises(ValueError):
                    validators.lint_query(q)


class ProjectResultTests(unittest.TestCase):
    """F1: _project_result trims article fields when `fields` is given, for both a
    flat `articles` list and articles nested under `clusters`; no-op otherwise."""

    def test_noop_without_fields(self) -> None:
        r = {"articles": [{"title": "t", "link": "l", "content": "big"}]}
        self.assertEqual(server._project_result(r, None), r)

    def test_projects_flat_articles(self) -> None:
        r = {"total_hits": 5, "articles": [{"title": "t", "link": "l", "content": "big", "nlp": {}}]}
        out = server._project_result(r, ["title", "link"])
        self.assertEqual(out["articles"][0], {"title": "t", "link": "l"})
        self.assertEqual(out["total_hits"], 5)

    def test_projects_clustered_articles(self) -> None:
        r = {"clusters": [{"cluster_id": "c1", "cluster_size": 1,
                           "articles": [{"title": "t", "link": "l", "content": "big"}]}]}
        out = server._project_result(r, ["title"])
        self.assertEqual(out["clusters"][0]["articles"][0], {"title": "t"})


class BuildSourceTests(unittest.TestCase):
    """`fields` -> `_source` (server-side field selection). Path prefix is shape-aware:
    flat results use `articles.*`, clustered results use `clusters.articles.*`."""

    def test_none_returns_none(self) -> None:
        self.assertIsNone(server.build_source(None, clustered=False))
        self.assertIsNone(server.build_source([], clustered=True))

    def test_flat_prefix(self) -> None:
        s = server.build_source(["title", "link", "nlp.summary"], clustered=False)
        self.assertIn("articles.title", s)
        self.assertIn("articles.nlp.summary", s)
        self.assertNotIn("clusters.articles", s)
        self.assertIn("total_hits", s)

    def test_clustered_prefix(self) -> None:
        s = server.build_source(["title", "link"], clustered=True)
        self.assertIn("clusters.articles.title", s)
        self.assertIn("clusters.cluster_id", s)
        self.assertIn("clusters.cluster_size", s)
        # must NOT emit the flat prefix as a standalone path (would drop clusters)
        self.assertNotIn(",articles.title", "," + s)


class ClusteringStraddleTests(unittest.TestCase):
    """Detector that decides whether a clustered search must be split at the
    2026-01-01 boundary (matches the API's own accept/reject behavior)."""

    def test_straddle_true(self) -> None:
        self.assertTrue(validators.clustering_straddles_cutoff("2025-11-01", "2026-02-01"))

    def test_before_only_false(self) -> None:
        self.assertFalse(validators.clustering_straddles_cutoff("2025-10-01", "2025-12-31"))

    def test_after_only_false(self) -> None:
        self.assertFalse(validators.clustering_straddles_cutoff("2026-02-01", "2026-07-01"))

    def test_ends_exactly_at_cutoff_is_not_straddle(self) -> None:
        # API accepts a range ending at 2026-01-01 (verified), so we must not split it.
        self.assertFalse(validators.clustering_straddles_cutoff("2025-11-01", "2026-01-01"))

    def test_non_iso_range_false(self) -> None:
        self.assertFalse(validators.clustering_straddles_cutoff("7 days ago", "now"))

    def test_missing_dates_false(self) -> None:
        self.assertFalse(validators.clustering_straddles_cutoff(None, None))


if __name__ == "__main__":
    unittest.main()
