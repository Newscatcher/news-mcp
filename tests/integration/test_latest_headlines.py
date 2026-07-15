"""
Integration tests for get_latest_headlines (POST /api/latest_headlines).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json


@pytest.mark.asyncio
class TestGetLatestHeadlines:
    async def test_no_query_returns_recent_headlines(self, mcp):
        result = await mcp.call_tool("get_latest_headlines", {"page_size": 5})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data
        assert data["page_size"] == 5

    async def test_when_window_param(self, mcp):
        result = await mcp.call_tool("get_latest_headlines", {"when": "24h", "page_size": 3})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data

    async def test_country_filter(self, mcp):
        result = await mcp.call_tool("get_latest_headlines", {"countries": ["US"], "page_size": 3})
        data = call_result_json(result)
        assert "clusters" in data or "articles" in data
