"""
Integration tests for search_by_link (POST /api/search_by_link).
"""

from __future__ import annotations

import json

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestSearchByLink:
    async def test_lookup_by_link(self, mcp):
        # Find real, current articles via search_articles first, then look one up
        # by link -- keeps this test independent of any specific hardcoded URL.
        # Avoid the single generic word "news" as a query: it scores a degenerate
        # indexed record (a tag-generator/tracking page on a domain that happens
        # to contain "news") as the top result every time, so every candidate
        # link is rejected by the API as "not an article link". A quoted,
        # specific phrase avoids that source entirely.
        search_result = await mcp.call_tool(
            "search_articles", {"q": '"climate change"', "page_size": 5}
        )
        _data = call_result_json(search_result)
        articles = _data.get("articles") or _data.get("clusters", [{}])[0].get("articles", [])
        assert articles, "Expected at least one article from search_articles to look up"

        errors = []
        for article in articles:
            result = await mcp.call_tool("search_by_link", {"links": [article["link"]]})
            text = call_result_text(result)
            if not text.startswith("Error:"):
                assert "articles" in json.loads(text)
                return
            errors.append(text)
        pytest.fail(f"All {len(articles)} candidate links were rejected: {errors}")

    async def test_lookup_by_id(self, mcp):
        search_result = await mcp.call_tool("search_articles", {"q": "news", "page_size": 1})
        _data = call_result_json(search_result)
        articles = _data.get("articles") or _data.get("clusters", [{}])[0].get("articles", [])
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
