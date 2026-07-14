"""
Integration tests for get_aggregation_count (POST /api/aggregation_count).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json


@pytest.mark.asyncio
class TestGetAggregationCount:
    async def test_basic_query_returns_buckets(self, mcp):
        result = await mcp.call_tool("get_aggregation_count", {"q": "technology"})
        data = call_result_json(result)
        assert "aggregations" in data

    async def test_aggregation_by_hour(self, mcp):
        result = await mcp.call_tool("get_aggregation_count", {"q": "technology", "aggregation_by": "hour"})
        data = call_result_json(result)
        assert "aggregations" in data

    async def test_aggregation_by_month(self, mcp):
        result = await mcp.call_tool("get_aggregation_count", {"q": "technology", "aggregation_by": "month"})
        data = call_result_json(result)
        assert "aggregations" in data
