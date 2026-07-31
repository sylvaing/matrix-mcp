"""Failure-path behaviour: upstream auth failures, rate limits, network
errors — and the guarantee that credentials never leak into tool errors."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from matrix_mcp.server import _sanitized_upstream_error, build_mcp
from tests.conftest import TOKEN


def _http_error(status: int, body: dict | str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://matrix.example.org/_matrix/x")
    if isinstance(body, dict):
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class TestSanitizer:
    def test_401_mentions_token_env_not_value(self):
        e = _http_error(401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid access token"})
        msg = str(_sanitized_upstream_error(e))
        assert "M_UNKNOWN_TOKEN" in msg
        assert "TENANT_<NAME>_TOKEN" in msg

    def test_429_includes_retry_after(self):
        e = _http_error(429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 1500})
        msg = str(_sanitized_upstream_error(e))
        assert "rate-limited" in msg and "1500 ms" in msg

    def test_429_without_retry_hint(self):
        e = _http_error(429, "slow down")
        msg = str(_sanitized_upstream_error(e))
        assert "rate-limited" in msg and "Retry later" in msg

    def test_404_hint(self):
        e = _http_error(404, {"errcode": "M_NOT_FOUND", "error": "Event not found."})
        msg = str(_sanitized_upstream_error(e))
        assert "404" in msg and "M_NOT_FOUND" in msg

    def test_5xx_generic(self):
        e = _http_error(502, "<html>bad gateway</html>")
        msg = str(_sanitized_upstream_error(e))
        assert "502" in msg
        assert "html" not in msg  # raw body is not echoed


# ---- through the MCP layer ----

@pytest.fixture()
def mcp(registry):
    return build_mcp(registry)


async def test_upstream_auth_failure_surfaces_cleanly(mcp, hs):
    hs.fail_mode = "auth"
    async with Client(mcp) as c:
        with pytest.raises(Exception) as exc_info:
            await c.call_tool("matrix_list_joined_rooms", {})
    msg = str(exc_info.value)
    assert "M_UNKNOWN_TOKEN" in msg or "rejected" in msg
    assert TOKEN not in msg  # the Matrix access token must never leak


async def test_upstream_forbidden_surfaces_cleanly(mcp, hs):
    hs.fail_mode = "forbidden"
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="M_FORBIDDEN"):
            await c.call_tool(
                "matrix_get_recent_messages", {"room_id": "!room0:example.org"}
            )


async def test_upstream_rate_limit_surfaces_retry_hint(mcp, hs):
    hs.fail_mode = "ratelimit"
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="rate-limited"):
            await c.call_tool(
                "matrix_send_message",
                {"room_id": "!room0:example.org", "body": "hi"},
            )


async def test_network_failure_surfaces_cleanly(mcp, hs):
    hs.fail_mode = "network"
    async with Client(mcp) as c:
        with pytest.raises(Exception) as exc_info:
            await c.call_tool("matrix_list_joined_rooms", {})
    msg = str(exc_info.value)
    assert "Could not reach" in msg and "ConnectError" in msg
    assert TOKEN not in msg


async def test_validation_error_reaches_mcp_client(mcp):
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="room_id"):
            await c.call_tool(
                "matrix_send_message", {"room_id": "oops", "body": "hi"}
            )
