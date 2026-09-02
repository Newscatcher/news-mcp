"""
Integration tests for get_breaking_news (POST /api/breaking_news).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestGetBreakingNews:
    async def test_no_params_returns_events(self, mcp):
        result = await mcp.call_tool("get_breaking_news", {"page_size": 3})
        data = call_result_json(result)
        assert "breaking_news_events" in data

    async def test_top_n_articles_within_limit(self, mcp):
        result = await mcp.call_tool("get_breaking_news", {"top_n_articles": 3, "page_size": 5})
        data = call_result_json(result)
        assert "breaking_news_events" in data

    async def test_top_n_articles_over_limit_rejected_client_side(self, mcp):
        """top_n_articles * page_size > 1000 must fail fast with a clean error,
        without ever reaching the upstream API."""
        result = await mcp.call_tool("get_breaking_news", {"top_n_articles": 50, "page_size": 100})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "1000" in text

    async def test_fields_projects_requested_keys(self, mcp):
        """Regression: fields was a silent no-op here -- _project_result didn't
        know about this tool's breaking_news_events[].articles response shape,
        so the full ~40-field article always passed through untrimmed."""
        result = await mcp.call_tool("get_breaking_news", {"page_size": 3, "fields": ["title", "nlp"]})
        data = call_result_json(result)
        for event in data["breaking_news_events"]:
            for article in event["articles"]:
                assert set(article.keys()) <= {"title", "nlp"}
