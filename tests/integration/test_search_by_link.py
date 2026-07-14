"""
Integration tests for search_by_link (POST /api/search_by_link).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestSearchByLink:
    async def test_lookup_by_link(self, mcp):
        # Find a real, current article via search_articles first, then look it up
        # by link -- keeps this test independent of any specific hardcoded URL.
        search_result = await mcp.call_tool("search_articles", {"q": "news", "page_size": 1})
        articles = call_result_json(search_result)["articles"]
        assert articles, "Expected at least one article from search_articles to look up"

        result = await mcp.call_tool("search_by_link", {"links": [articles[0]["link"]]})
        data = call_result_json(result)
        assert "articles" in data

    async def test_lookup_by_id(self, mcp):
        search_result = await mcp.call_tool("search_articles", {"q": "news", "page_size": 1})
        articles = call_result_json(search_result)["articles"]
        assert articles, "Expected at least one article from search_articles to look up"

        result = await mcp.call_tool("search_by_link", {"ids": [articles[0]["id"]]})
        data = call_result_json(result)
        assert "articles" in data

    async def test_both_ids_and_links_rejected(self, mcp):
        result = await mcp.call_tool("search_by_link", {"ids": ["some-id"], "links": ["https://example.com/a"]})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "not both" in text

    async def test_neither_ids_nor_links_rejected(self, mcp):
        result = await mcp.call_tool("search_by_link", {})
        text = call_result_text(result)
        assert text.startswith("Error:")
