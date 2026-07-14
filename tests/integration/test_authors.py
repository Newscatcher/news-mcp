"""
Integration tests for search_by_author (POST /api/authors).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json


@pytest.mark.asyncio
class TestSearchByAuthor:
    async def test_known_author_returns_articles(self, mcp):
        result = await mcp.call_tool("search_by_author", {"author_name": "Reuters Staff", "page_size": 3})
        data = call_result_json(result)
        assert "articles" in data

    async def test_unknown_author_returns_empty_list_not_error(self, mcp):
        result = await mcp.call_tool("search_by_author", {"author_name": "Definitely Not A Real Byline 123456789"})
        data = call_result_json(result)
        assert isinstance(data.get("articles"), list)
