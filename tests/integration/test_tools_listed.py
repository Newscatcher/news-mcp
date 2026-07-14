"""
Integration test: verify the MCP server advertises the expected tools.

This is a fast smoke test -- doesn't call any real API endpoints,
just checks the server's tool manifest.
"""

from __future__ import annotations

import pytest

EXPECTED_TOOLS = {
    "search_articles",
    "get_latest_headlines",
    "get_breaking_news",
    "search_by_author",
    "search_by_link",
    "list_sources",
    "get_aggregation_count",
    "get_subscription",
    "check_health",
}


@pytest.mark.asyncio
async def test_all_expected_tools_registered(mcp):
    result = await mcp.list_tools()
    registered = {t.name for t in result.tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing tools: {missing}"


@pytest.mark.asyncio
async def test_tool_count(mcp):
    result = await mcp.list_tools()
    assert len(result.tools) >= len(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_each_tool_has_description(mcp):
    result = await mcp.list_tools()
    for tool in result.tools:
        assert tool.description, f"Tool '{tool.name}' has no description"


@pytest.mark.asyncio
async def test_search_articles_has_required_params(mcp):
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "search_articles"), None)
    assert tool is not None, "search_articles not found"
    schema = tool.inputSchema
    assert "q" in schema.get("properties", {}), "search_articles missing 'q' param"
    assert "q" in schema.get("required", []), "search_articles 'q' should be required"


@pytest.mark.asyncio
async def test_search_by_author_has_required_params(mcp):
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "search_by_author"), None)
    assert tool is not None, "search_by_author not found"
    assert "author_name" in tool.inputSchema.get("required", []), (
        "search_by_author 'author_name' should be required"
    )


@pytest.mark.asyncio
async def test_search_by_link_ids_and_links_are_optional_at_schema_level(mcp):
    """ids/links are each optional in the JSON schema -- the mutual-exclusivity/
    at-least-one-required rule is enforced at runtime by
    validators.validate_ids_or_links, not the schema itself."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "search_by_link"), None)
    assert tool is not None, "search_by_link not found"
    props = tool.inputSchema.get("properties", {})
    assert {"ids", "links"} <= set(props)


@pytest.mark.asyncio
async def test_get_subscription_and_check_health_have_no_required_params(mcp):
    result = await mcp.list_tools()
    tools = {t.name: t for t in result.tools}
    for name in ("get_subscription", "check_health"):
        tool = tools.get(name)
        assert tool is not None, f"{name} not found"
        assert not tool.inputSchema.get("required"), f"{name} should have no required params"
