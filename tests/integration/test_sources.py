"""
Integration tests for list_sources (POST /api/sources).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestListSources:
    async def test_country_filter(self, mcp):
        result = await mcp.call_tool("list_sources", {"countries": ["US"]})
        data = call_result_json(result)
        assert "sources" in data

    async def test_source_name_filter(self, mcp):
        result = await mcp.call_tool("list_sources", {"source_name": "sport"})
        data = call_result_json(result)
        assert "sources" in data

    async def test_source_url_with_additional_info(self, mcp):
        result = await mcp.call_tool(
            "list_sources", {"source_url": ["bbc.com"], "include_additional_info": True}
        )
        data = call_result_json(result)
        assert "sources" in data

    async def test_bulk_source_url_coverage_check(self, mcp):
        """source_url accepts multiple domains in one call for bulk coverage checks."""
        result = await mcp.call_tool(
            "list_sources",
            {"source_url": ["bbc.com", "cnn.com", "definitely-not-a-real-domain-xyz.com"], "include_additional_info": True},
        )
        data = call_result_json(result)
        assert "sources" in data

    async def test_source_url_without_additional_info_rejected_client_side(self, mcp):
        result = await mcp.call_tool("list_sources", {"source_url": ["bbc.com"]})
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_no_filter_rejected_client_side(self, mcp):
        result = await mcp.call_tool("list_sources", {})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "at least one filter" in text
