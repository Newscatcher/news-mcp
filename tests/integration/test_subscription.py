"""
Integration tests for get_subscription (POST /api/subscription).
"""

from __future__ import annotations

import pytest

from conftest import call_result_json


@pytest.mark.asyncio
class TestGetSubscription:
    async def test_returns_plan_info(self, mcp):
        result = await mcp.call_tool("get_subscription", {})
        data = call_result_json(result)
        for field in ("active", "concurrent_calls", "plan", "plan_calls", "remaining_calls", "historical_days"):
            assert field in data, f"Expected '{field}' in subscription response, got: {data}"
