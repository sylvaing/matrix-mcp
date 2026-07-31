"""End-to-end tests against a real dockerized Synapse homeserver.

The rest of the suite runs against httpx.MockTransport; these tests exist to
prove the same code paths work against the real thing — real routing, real
auth, real event IDs, real search indexing.

They are skipped unless MATRIX_MCP_IT_BASE_URL is set. Run them with:

    bash scripts/integration-test.sh

which boots a throwaway Synapse in Docker, registers test users, exports:

- MATRIX_MCP_IT_BASE_URL      e.g. http://127.0.0.1:18008
- MATRIX_MCP_IT_ALICE_MXID    e.g. @it_alice:localhost
- MATRIX_MCP_IT_ALICE_TOKEN   Alice's access token
- MATRIX_MCP_IT_BOB_MXID      a second registered user (invite target)

then runs `pytest tests/integration` and tears the container down.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid

import pytest

from matrix_mcp.client import TenantRegistry
from matrix_mcp.tools import messages as messages_tools
from matrix_mcp.tools import rooms as rooms_tools
from matrix_mcp.tools import search as search_tools
from matrix_mcp.tools import writes as writes_tools

BASE_URL = os.environ.get("MATRIX_MCP_IT_BASE_URL")

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="no live Synapse (run via scripts/integration-test.sh)",
)

# 1x1 transparent PNG — a real, decodable image payload.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _it_env() -> dict[str, str]:
    return {
        "TENANT_IT_BASE_URL": BASE_URL or "",
        "TENANT_IT_USER_ID": os.environ["MATRIX_MCP_IT_ALICE_MXID"],
        "TENANT_IT_TOKEN": os.environ["MATRIX_MCP_IT_ALICE_TOKEN"],
    }


@pytest.fixture()
async def registry():
    reg = TenantRegistry(_it_env())  # no mock transport: real network
    yield reg
    await reg.aclose_all()


@pytest.fixture()
def client(registry):
    return registry.get("it")


@pytest.fixture()
def bob() -> str:
    return os.environ["MATRIX_MCP_IT_BOB_MXID"]


async def test_full_write_read_workflow(client, bob):
    """create room -> invite -> send -> read back -> react -> file -> mark read."""
    marker = uuid.uuid4().hex

    created = await writes_tools.create_room(
        client, name=f"it-{marker[:8]}", topic="integration run",
        invite_user_ids=[bob],
    )
    room_id = created["room_id"]
    assert room_id.startswith("!")

    sent = await messages_tools.send_message(
        client, room_id, f"integration marker {marker}"
    )
    event_id = sent["event_id"]
    assert event_id.startswith("$")

    got = await messages_tools.get_recent_messages(client, room_id, limit=10)
    bodies = [m.get("body") for m in got["messages"]]
    assert f"integration marker {marker}" in bodies

    reacted = await writes_tools.send_reaction(client, room_id, event_id, "👍")
    assert reacted["event_id"].startswith("$")

    filed = await writes_tools.send_file(
        client, room_id, base64.b64encode(PNG_1PX).decode(), "dot.png",
        mime_type="image/png",
    )
    assert filed["mxc"].startswith("mxc://")
    assert filed["msgtype"] == "m.image"

    read = await messages_tools.mark_read(client, room_id, event_id)
    assert read["ok"] is True


async def test_room_info_and_members(client, bob):
    created = await writes_tools.create_room(
        client, name="it-info", topic="metadata check", invite_user_ids=[bob],
    )
    room_id = created["room_id"]

    info = await rooms_tools.get_room_info(client, room_id)
    assert info["name"] == "it-info"
    assert info["topic"] == "metadata check"
    assert info["encrypted"] is False

    members = await rooms_tools.list_room_members(client, room_id)
    mxids = [m["user_id"] for m in members["members"]]
    assert os.environ["MATRIX_MCP_IT_ALICE_MXID"] in mxids  # bob is only invited

    rooms = await rooms_tools.list_joined_rooms(client, limit=100)
    assert room_id in [r["room_id"] for r in rooms["rooms"]]


async def test_idempotency_key_prevents_duplicate_send(client):
    created = await writes_tools.create_room(client, name="it-idem")
    room_id = created["room_id"]
    key = f"it-idem-{uuid.uuid4().hex}"

    first = await messages_tools.send_message(
        client, room_id, "exactly once", idempotency_key=key
    )
    second = await messages_tools.send_message(
        client, room_id, "exactly once", idempotency_key=key
    )
    # Same Matrix transaction ID -> the homeserver returns the same event.
    assert first["event_id"] == second["event_id"]

    got = await messages_tools.get_recent_messages(client, room_id, limit=20)
    assert [m["body"] for m in got["messages"]].count("exactly once") == 1


async def test_server_side_search_finds_sent_message(client):
    created = await writes_tools.create_room(client, name="it-search")
    room_id = created["room_id"]
    needle = f"needle{uuid.uuid4().hex[:10]}"
    await messages_tools.send_message(client, room_id, f"haystack {needle} haystack")

    hits = []
    for _ in range(20):  # indexing may lag a moment
        result = await search_tools.search_messages(client, needle, room_id=room_id)
        hits = result["hits"]
        if hits:
            break
        await asyncio.sleep(0.5)
    assert hits, f"search never returned the sent message containing {needle!r}"
    assert needle in hits[0]["body"]


async def test_bad_token_yields_sanitized_error(registry):
    """A real 401 from Synapse must surface as the sanitized hint, not a dump."""
    from matrix_mcp.server import build_mcp

    env = _it_env()
    # Deliberately NOT syt_-shaped: the scrub gate (rightly) rejects anything
    # that looks like a real Matrix access token, even a fake one.
    env["TENANT_IT_TOKEN"] = "invalid-integration-token"
    bad_registry = TenantRegistry(env)
    try:
        mcp = build_mcp(bad_registry)
        from fastmcp import Client

        async with Client(mcp) as c:
            with pytest.raises(Exception, match="TENANT_<NAME>_TOKEN"):
                await c.call_tool("matrix_list_joined_rooms", {})
    finally:
        await bad_registry.aclose_all()


def _payload(result):
    """Extract the structured dict a tool returned, across fastmcp versions."""
    import json

    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    content = getattr(result, "content", result)
    return json.loads(content[0].text)


async def test_mcp_protocol_round_trip_against_real_synapse(registry):
    """The full stack: MCP client -> FastMCP -> tool -> httpx -> live Synapse."""
    from fastmcp import Client

    from matrix_mcp.server import build_mcp

    mcp = build_mcp(registry)
    async with Client(mcp) as c:
        created = await c.call_tool(
            "matrix_create_room", {"name": "it-mcp-rt", "dry_run": False}
        )
        room_id = _payload(created)["room_id"]
        sent = await c.call_tool(
            "matrix_send_message", {"room_id": room_id, "body": "over MCP"}
        )
        assert _payload(sent)["event_id"].startswith("$")
