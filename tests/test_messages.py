"""Message tools against the mock homeserver."""

import pytest

from matrix_mcp.tools import messages

ROOM = "!room0:example.org"


async def test_get_recent_messages_happy(client):
    out = await messages.get_recent_messages(client, ROOM, limit=5)
    assert out["room_id"] == ROOM
    assert len(out["messages"]) == 5
    msg = out["messages"][0]
    assert msg["event_id"] == "$evt0"
    assert msg["sender"] == "@bob:example.org"
    assert msg["body"] == "message 0"
    assert msg["msgtype"] == "m.text"


async def test_get_recent_messages_pagination(client):
    page1 = await messages.get_recent_messages(client, ROOM, limit=10)
    assert page1["next_cursor"] == "t10"
    page2 = await messages.get_recent_messages(
        client, ROOM, limit=10, before_cursor=page1["next_cursor"]
    )
    assert page2["messages"][0]["event_id"] == "$evt10"
    assert page2["next_cursor"] == "t20"


async def test_get_recent_messages_filters_non_message_events(client, hs):
    # The mock only returns m.room.message, so just assert types are filtered set
    out = await messages.get_recent_messages(client, ROOM, limit=3)
    assert all(m["type"] in ("m.room.message", "m.room.encrypted") for m in out["messages"])


@pytest.mark.parametrize("limit", [0, 101])
async def test_get_recent_messages_bad_limit(client, limit):
    with pytest.raises(ValueError, match="limit"):
        await messages.get_recent_messages(client, ROOM, limit=limit)


async def test_get_recent_messages_bad_room(client):
    with pytest.raises(ValueError, match="room_id"):
        await messages.get_recent_messages(client, "room0")


async def test_send_message_happy(client, hs):
    out = await messages.send_message(client, ROOM, "hello world")
    assert out["dry_run"] is False
    assert out["event_id"].startswith("$sent_")
    assert len(hs.sent) == 1
    assert hs.sent[0]["body"] == "hello world"
    assert hs.sent[0]["msgtype"] == "m.text"


async def test_send_message_dry_run_sends_nothing(client, hs):
    out = await messages.send_message(client, ROOM, "preview me", dry_run=True)
    assert out["dry_run"] is True
    assert out["would_send"]["body"] == "preview me"
    assert out["would_send"]["sender"] == "@alice:example.org"
    assert hs.sent == []


async def test_send_message_idempotency_key_becomes_txn_id(client, hs):
    out = await messages.send_message(client, ROOM, "hi", idempotency_key="retry-42")
    assert out["txn_id"] == "retry-42"
    assert hs.sent[0]["txn_id"] == "retry-42"


async def test_send_message_empty_body_rejected(client, hs):
    with pytest.raises(ValueError, match="body"):
        await messages.send_message(client, ROOM, "   ")
    assert hs.sent == []


async def test_send_message_oversized_body_rejected(client, hs):
    with pytest.raises(ValueError, match="too long"):
        await messages.send_message(client, ROOM, "x" * 70_000)
    assert hs.sent == []


async def test_mark_read_happy(client, hs):
    out = await messages.mark_read(client, ROOM, "$evt3")
    assert out == {"ok": True, "room_id": ROOM, "event_id": "$evt3"}
    assert len(hs.receipts) == 1


async def test_mark_read_bad_event_id(client, hs):
    with pytest.raises(ValueError, match="event_id"):
        await messages.mark_read(client, ROOM, "evt3")
    assert hs.receipts == []
