"""
Integration test fixtures.

Connects to a running news-mcp server exactly as any MCP client does --
via streamable-http with an x-api-token header.

Usage:
    export NEWS_API_KEY=your_key
    python server.py &
    pytest tests/integration/ -v -s
"""

from __future__ import annotations

import json
import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# No hosted news-mcp URL exists yet -- default to localhost so tests "just work"
# against a locally started server with zero extra config. pr-tests.yml sets
# MCP_SERVER_URL explicitly either way.
# TODO: point at the hosted news-mcp URL once one exists.
SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
API_KEY = os.getenv("NEWS_API_KEY", "")


@pytest.fixture()
async def mcp():
    """
    Per-test MCP ClientSession.
    Connects to the server with an x-api-token header -- same as any MCP client.
    """
    print(f"\nRunning tests against: {SERVER_URL}\n")
    headers = {"x-api-token": API_KEY} if API_KEY else {}
    try:
        async with streamablehttp_client(SERVER_URL, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except* RuntimeError as eg:
        # Suppress the anyio cancel-scope teardown bug in pytest-asyncio.
        # All actual test assertions have already run at this point.
        non_cancel_scope = [e for e in eg.exceptions if "cancel scope" not in str(e).lower()]
        if non_cancel_scope:
            raise eg


def call_result_text(result) -> str:
    """Extract raw text and always print it -- visible with pytest -s."""
    assert result.content, "Tool returned no content"
    text = result.content[0].text
    print(f"\n--- MCP response ---\n{text}\n--------------------")
    return text


def call_result_json(result) -> dict:
    """Extract, print, and parse JSON from a CallToolResult."""
    text = call_result_text(result)
    assert not text.startswith("Error:"), f"Tool returned error: {text}"
    return json.loads(text)
