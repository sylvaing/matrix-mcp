"""Server-level tests: tool registration, MCP round-trips, tenant routing,
and bearer auth on the HTTP transport."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from matrix_mcp.client import TenantRegistry
from matrix_mcp.server import BearerAuthMiddleware, build_mcp, create_app
from tests.conftest import ENV_TWO_TENANTS

EXPECTED_TOOLS = {
    "matrix_list_joined_rooms",
    "matrix_get_room_info",
    "matrix_list_room_members",
    "matrix_get_recent_messages",
    "matrix_send_message",
    "matrix_mark_read",
    "matrix_send_reaction",
    "matrix_send_file",
    "matrix_create_room",
    "matrix_invite_user",
    "matrix_search_messages",
}

BEARER = "test-mcp-bearer-token"


def _payload(result):
    """Extract the structured dict a tool returned, across fastmcp versions."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    content = getattr(result, "content", result)
    return json.loads(content[0].text)


@pytest.fixture()
def mcp(registry):
    return build_mcp(registry)


async def test_all_eleven_tools_registered(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


async def test_tools_listable_over_mcp_protocol(mcp):
    async with Client(mcp) as c:
        listed = await c.list_tools()
    assert {t.name for t in listed} == EXPECTED_TOOLS


async def test_call_tool_over_mcp_happy(mcp):
    async with Client(mcp) as c:
        result = await c.call_tool("matrix_list_joined_rooms", {"limit": 2})
    out = _payload(result)
    assert out["total"] == 5
    assert len(out["rooms"]) == 2


async def test_call_send_message_over_mcp(mcp, hs):
    async with Client(mcp) as c:
        result = await c.call_tool(
            "matrix_send_message",
            {"room_id": "!room0:example.org", "body": "via mcp"},
        )
    out = _payload(result)
    assert out["event_id"].startswith("$sent_")
    assert hs.sent[0]["body"] == "via mcp"


async def test_unknown_tenant_is_clean_error(mcp):
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="Unknown tenant"):
            await c.call_tool(
                "matrix_list_joined_rooms", {"tenant": "nonexistent"}
            )


async def test_two_tenants_routed_independently(transport, hs):
    registry = TenantRegistry(ENV_TWO_TENANTS, transport=transport)
    mcp = build_mcp(registry)
    async with Client(mcp) as c:
        r1 = await c.call_tool("matrix_list_joined_rooms", {"tenant": "work"})
        r2 = await c.call_tool("matrix_list_joined_rooms", {})  # primary = "main"
    assert _payload(r1)["total"] == 5
    assert _payload(r2)["total"] == 5
    hosts = {str(req.url.host) for req in hs.requests}
    assert hosts == {"matrix.example.org", "matrix.work.example"}


async def test_primary_tenant_override(transport):
    registry = TenantRegistry(ENV_TWO_TENANTS, transport=transport)
    mcp = build_mcp(registry, primary_tenant="work")
    async with Client(mcp) as c:
        result = await c.call_tool(
            "matrix_send_message",
            {"room_id": "!room0:example.org", "body": "hi", "dry_run": True},
        )
    assert _payload(result)["would_send"]["sender"] == "@alice:work.example"


def test_primary_tenant_override_must_exist(registry):
    with pytest.raises(RuntimeError, match="not a configured tenant"):
        build_mcp(registry, primary_tenant="ghost")


# ---- HTTP transport auth ----

@pytest.fixture()
def http_app(registry):
    return create_app(registry, bearer_token=BEARER)


def test_create_app_refuses_empty_bearer(registry):
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        create_app(registry, bearer_token="")


def test_health_is_unauthenticated(http_app):
    with TestClient(http_app) as tc:
        r = tc.get("/health")
    assert r.status_code == 200 and r.text == "ok"


def test_mcp_endpoint_rejects_missing_bearer(http_app):
    with TestClient(http_app) as tc:
        r = tc.post("/mcp", json={})
    assert r.status_code == 401
    # Must not advertise OAuth (some MCP clients misread WWW-Authenticate).
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_mcp_endpoint_rejects_wrong_bearer(http_app):
    with TestClient(http_app) as tc:
        r = tc.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_mcp_endpoint_rejects_bearer_prefix_of_real_token(http_app):
    with TestClient(http_app) as tc:
        r = tc.post("/mcp", json={}, headers={"Authorization": f"Bearer {BEARER[:-1]}"})
    assert r.status_code == 401


def test_mcp_endpoint_accepts_correct_bearer(http_app):
    with TestClient(http_app) as tc:
        r = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {BEARER}",
                "Accept": "application/json, text/event-stream",
            },
        )
    # Past auth: anything but 401 means the middleware admitted the request.
    assert r.status_code != 401


def test_mcp_endpoint_lives_at_exactly_slash_mcp(http_app):
    """POST /mcp must be served directly — not 307-redirected to /mcp/
    (redirects confuse some MCP clients and break curl examples)."""
    with TestClient(http_app) as tc:
        r = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {BEARER}",
                "Accept": "application/json, text/event-stream",
            },
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert "matrix_send_message" in r.text


def test_middleware_requires_nonempty_token():
    with pytest.raises(RuntimeError):
        BearerAuthMiddleware(lambda scope, receive, send: None, "")
