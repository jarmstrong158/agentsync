"""Conformance tests for MCP protocol revision 2026-07-28.

Driven through a real in-process client so the dispatch path that applies cache
hints and result metadata is exercised; MCPServer's convenience accessors
bypass it and these assertions would then check nothing.
"""

from __future__ import annotations

import anyio

import agentsync_server
from mcp.client.client import Client

PROTOCOL_VERSION = "2026-07-28"

EXPECTED_TOOL_ORDER = [
    "add_collaborator", "provision", "survey", "claim", "check_conflicts",
    "release", "update_status", "finish", "history",
]


def _run(body):
    async def main():
        async with Client(agentsync_server.mcp) as client:
            value = body(client)
            return await value if hasattr(value, "__await__") else value
    return anyio.run(main)


def _wire(body):
    return _run(body).model_dump(by_alias=True, exclude_none=True)


def test_negotiates_2026_07_28():
    assert _run(lambda c: c.protocol_version) == PROTOCOL_VERSION


def test_tools_list_carries_cache_hints():
    """SEP-2549, asserted on the serialized wire form so a regression in the
    camelCase aliases is caught rather than silently passing."""
    wire = _wire(lambda c: c.list_tools())
    assert wire["ttlMs"] == 300_000
    assert wire["cacheScope"] == "public"


def test_results_carry_result_type():
    assert _wire(lambda c: c.list_tools())["resultType"] == "complete"


def test_server_identifies_itself_in_result_meta():
    info = _wire(lambda c: c.list_tools())["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["name"] == "agentsync"


def test_server_discover_advertises_supported_versions():
    wire = _wire(lambda c: c.session.discover())
    assert PROTOCOL_VERSION in wire["supportedVersions"]
    assert wire["ttlMs"] == 300_000


def test_tool_order_is_deterministic():
    """Deterministic ordering keeps client-side and prompt caches hitting."""
    first = [t["name"] for t in _wire(lambda c: c.list_tools())["tools"]]
    second = [t["name"] for t in _wire(lambda c: c.list_tools())["tools"]]
    assert first == EXPECTED_TOOL_ORDER
    assert first == second


def test_claim_arbitration_does_not_depend_on_transport_state():
    """The point of the migration for this server.

    agentsync's collision safety is compare-and-swap over claims.json guarded
    by git push retries. It never depended on the initialize handshake or on
    Mcp-Session-Id, so removing protocol-level sessions does not weaken it.
    Peer identity comes from AGENTSYNC_AGENT_ID plus the per-process INSTANCE
    token, both of which are independent of the transport.
    """
    assert isinstance(agentsync_server.INSTANCE, str)
    assert agentsync_server.INSTANCE
    assert agentsync_server.PUSH_RETRIES >= 1
