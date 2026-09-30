"""Overview tools (DMs, unread) against the mock homeserver's filtered /sync."""

import json

import pytest

from matrix_mcp.tools import overview


async def test_sync_request_is_read_only_and_filtered(client, hs):
    await overview.get_unread(client)
    (params,) = hs.sync_params
    assert params["timeout"] == "0"
    assert params["set_presence"] == "offline"
    assert "since" not in params
    f = json.loads(params["filter"])
    assert f["room"]["timeline"]["limit"] == 1
    assert f["account_data"]["types"] == ["m.direct"]
    # Nothing written: no receipts, no sends.
    assert hs.receipts == [] and hs.sent == []


async def test_list_direct_messages_uses_m_direct(client):
    out = await overview.list_direct_messages(client)
    assert out["total"] == 2
    ids = [d["room_id"] for d in out["direct_messages"]]
    # Most recent first; group rooms excluded; m.direct entries not joined ignored.
    assert ids == ["!dm_bob:example.org", "!dm_carol:example.org"]
    bob = out["direct_messages"][0]
    assert bob["is_direct"] is True
    assert bob["dm_user_id"] == "@bob:example.org"
    assert bob["name"] == "Bob"
    assert bob["unread_count"] == 2
    assert bob["last_message"]["body"] == "tu es dispo ?"
    assert bob["last_message"]["sender_name"] == "Bob"


async def test_list_direct_messages_truncates_preview(client):
    out = await overview.list_direct_messages(client)
    carol = out["direct_messages"][1]
    assert len(carol["last_message"]["body"]) == overview.PREVIEW_CHARS + 1
    assert carol["last_message"]["body"].endswith("…")


async def test_list_direct_messages_unread_only(client):
    out = await overview.list_direct_messages(client, unread_only=True)
    assert [d["room_id"] for d in out["direct_messages"]] == ["!dm_bob:example.org"]


async def test_list_direct_messages_pagination(client):
    page1 = await overview.list_direct_messages(client, limit=1)
    assert len(page1["direct_messages"]) == 1 and page1["next_cursor"] == 1
    page2 = await overview.list_direct_messages(client, limit=1, cursor=1)
    assert page2["direct_messages"][0]["room_id"] == "!dm_carol:example.org"
    assert page2["next_cursor"] is None


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1000}, {"cursor": -1}])
async def test_list_direct_messages_bad_params(client, kwargs):
    with pytest.raises(ValueError):
        await overview.list_direct_messages(client, **kwargs)


async def test_get_unread(client):
    out = await overview.get_unread(client)
    assert [r["room_id"] for r in out["rooms"]] == [
        "!dm_bob:example.org",
        "!room0:example.org",
    ]
    assert out["total_rooms"] == 2
    assert out["total_unread"] == 7
    assert out["total_highlights"] == 1
    general = out["rooms"][1]
    assert general["name"] == "General"
    assert general["is_direct"] is False
    assert general["dm_user_id"] is None


async def test_unnamed_room_named_from_heroes_and_encrypted_preview(client):
    rooms = await overview._snapshot(client)
    room1 = next(r for r in rooms if r["room_id"] == "!room1:example.org")
    assert room1["name"] == "Bob, @carol:example.org"
    assert room1["unread_count"] == 0
    assert room1["last_message"]["encrypted"] is True
    assert room1["last_message"]["body"] is None


async def test_no_m_direct_means_no_dms(client, hs, monkeypatch):
    from tests import conftest

    monkeypatch.setitem(conftest.SYNC_OVERVIEW, "account_data", {"events": []})
    out = await overview.list_direct_messages(client)
    assert out["total"] == 0


async def test_get_unread_bad_limit(client):
    with pytest.raises(ValueError):
        await overview.get_unread(client, limit=0)
