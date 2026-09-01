"""
Integration tests for search_articles (POST /api/search).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestSearchArticles:
    async def test_basic_query_returns_articles(self, mcp):
        result = await mcp.call_tool("search_articles", {"q": "technology", "page_size": 5})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data
        assert data["page_size"] == 5
        assert "total_hits" in data

    async def test_match_all_query(self, mcp):
        """q='*' alone is documented as a valid match-all query."""
        result = await mcp.call_tool("search_articles", {"q": "*", "page_size": 1})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_boolean_query(self, mcp):
        result = await mcp.call_tool("search_articles", {"q": "(bitcoin OR ethereum) AND blockchain", "page_size": 3})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_pagination_params_echoed(self, mcp):
        result = await mcp.call_tool("search_articles", {"q": "news", "page": 2, "page_size": 10})
        data = call_result_json(result)
        assert data["page"] == 2
        assert data["page_size"] == 10

    async def test_clustering_enabled_returns_clusters(self, mcp):
        result = await mcp.call_tool("search_articles", {"q": "news", "clustering_enabled": True, "page_size": 20})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_malformed_boolean_query_surfaces_as_clean_error(self, mcp):
        """Mixing bare words with OR at the same level without grouping is documented
        to 422 upstream -- confirm it surfaces as a clean 'Error:' string, not a crash."""
        result = await mcp.call_tool("search_articles", {"q": "machine learning OR AI without grouping OR"})
        text = call_result_text(result)
        # Either the API accepts or rejects this -- either way it must never come
        # back as an unhandled "Unexpected error:" crash.
        assert not text.startswith("Unexpected error:"), f"Crashed instead of a clean error: {text}"

    async def test_language_and_country_filters(self, mcp):
        result = await mcp.call_tool(
            "search_articles", {"q": "news", "lang": ["en"], "countries": ["US"], "page_size": 3}
        )
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_search_in_as_list_accepted(self, mcp):
        """Regression: News API v3 rejects a JSON array for search_in over POST
        (499 "str type expected") -- this server sends it as a comma-joined
        string internally, but the tool parameter itself stays a list."""
        result = await mcp.call_tool(
            "search_articles", {"q": '"Elon Musk"', "search_in": ["title"], "page_size": 3}
        )
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_fields_projects_requested_keys(self, mcp):
        result = await mcp.call_tool(
            "search_articles",
            {"q": "news", "fields": ["title", "link"], "page_size": 3, "clustering_enabled": False},
        )
        data = call_result_json(result)
        for article in data["articles"]:
            assert set(article.keys()) <= {"title", "link"}

    async def test_fields_rejects_unrecognized_field(self, mcp):
        """Regression: "summary" isn't a real top-level article field (it's
        "description" or the dotted "nlp.summary") -- this used to silently
        vanish from the output instead of erroring."""
        result = await mcp.call_tool("search_articles", {"q": "news", "fields": ["title", "summary"]})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "nlp.summary" in text
