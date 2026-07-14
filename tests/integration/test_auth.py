"""
Integration tests for API token auth across tools.

Tests:
- Tools that require auth return a clear error with an invalid token.
- check_health (the only no-auth tool) succeeds regardless of token.
- Explicit api_token parameter is honored (proves param precedence).
"""

from __future__ import annotations

import pytest

from conftest import call_result_text

# Tools that require a valid API token (will 401/403/fail with a bad token).
# get_subscription is the cheapest real call -- News API v3 has no free/no-auth
# endpoint of its own to probe with instead.
AUTH_REQUIRED_TOOLS = [
    ("get_subscription", {}),
    ("list_sources", {"lang": ["en"]}),
    ("search_articles", {"q": "test"}),
    ("get_aggregation_count", {"q": "test"}),
]

# Tools that explicitly do NOT require an API token.
# check_health never calls the upstream API at all -- News API v3 has no public
# health/version endpoint.
NO_AUTH_TOOLS = [
    ("check_health", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", NO_AUTH_TOOLS)
async def test_no_auth_tools_succeed_without_token(mcp, tool_name, kwargs):
    """check_health must not fail due to a missing/invalid API token."""
    result = await mcp.call_tool(tool_name, {**kwargs, "api_token": "INVALID_TOKEN_FOR_TEST"})
    text = call_result_text(result)
    assert not text.startswith("Error:"), f"{tool_name} should never need auth, got: {text}"
    assert not text.startswith("Unexpected error:"), f"{tool_name} crashed: {text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", AUTH_REQUIRED_TOOLS)
async def test_auth_required_tools_fail_with_bad_token(mcp, tool_name, kwargs):
    """Tools that need auth must return a clean error with an invalid token."""
    result = await mcp.call_tool(tool_name, {**kwargs, "api_token": "INVALID_TOKEN_XYZ"})
    text = call_result_text(result)
    assert text.startswith("Error:"), f"{tool_name} should return 'Error: ...' with invalid token, got: {text!r}"


@pytest.mark.asyncio
async def test_missing_token_returns_error_message(mcp):
    """
    When no valid API token is available, auth-required tools must return a
    helpful error string -- not crash with an exception.
    """
    result = await mcp.call_tool("get_subscription", {"api_token": "INVALID_TOKEN_XYZ"})
    text = call_result_text(result)
    assert text.startswith("Error:"), f"Expected error message, got: {text!r}"


@pytest.mark.asyncio
async def test_explicit_api_token_param_is_used(mcp):
    """
    Passing api_token explicitly should be used (even if env has a different token).
    With a bad explicit token we expect an auth error, confirming the param was used.
    """
    result = await mcp.call_tool("get_subscription", {"api_token": "DEFINITELY_WRONG_TOKEN_12345"})
    text = call_result_text(result)
    assert text.startswith("Error:"), f"Expected auth error with wrong explicit token, got: {text!r}"
