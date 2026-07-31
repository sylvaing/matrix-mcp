"""Write tools: reactions, files, room create, invite."""

import base64

import pytest

from matrix_mcp.tools import writes

ROOM = "!room0:example.org"


# ---- reactions ----

async def test_send_reaction_happy(client, hs):
    out = await writes.send_reaction(client, ROOM, "$evt1", "👍")
    assert out["dry_run"] is False
    assert out["reacted_to"] == "$evt1"
    assert len(hs.sent) == 1
    assert hs.sent[0]["type"] == "m.reaction"
    rel = hs.sent[0]["m.relates_to"]
    assert rel == {"rel_type": "m.annotation", "event_id": "$evt1", "key": "👍"}


async def test_send_reaction_dry_run(client, hs):
    out = await writes.send_reaction(client, ROOM, "$evt1", "✅", dry_run=True)
    assert out["dry_run"] is True and hs.sent == []


@pytest.mark.parametrize("emoji", ["", "  ", "x" * 101])
async def test_send_reaction_bad_emoji(client, emoji):
    with pytest.raises(ValueError):
        await writes.send_reaction(client, ROOM, "$evt1", emoji)


async def test_send_reaction_bad_target(client):
    with pytest.raises(ValueError, match="event_id"):
        await writes.send_reaction(client, ROOM, "evt1", "👍")


# ---- files ----

async def test_send_file_happy_uploads_then_sends(client, hs):
    payload = base64.b64encode(b"\x89PNG fake image bytes").decode()
    out = await writes.send_file(
        client, ROOM, payload, "pic.png", mime_type="image/png"
    )
    assert out["dry_run"] is False
    assert out["mxc"] == "mxc://example.org/mediaid123"
    assert out["msgtype"] == "m.image"  # inferred from mime
    assert len(hs.uploads) == 1
    assert hs.uploads[0]["content_type"] == "image/png"
    assert len(hs.sent) == 1
    assert hs.sent[0]["url"] == "mxc://example.org/mediaid123"
    assert hs.sent[0]["info"]["mimetype"] == "image/png"


@pytest.mark.parametrize("mime,expected", [
    ("image/jpeg", "m.image"),
    ("video/mp4", "m.video"),
    ("audio/mpeg", "m.audio"),
    ("application/pdf", "m.file"),
])
async def test_send_file_msgtype_inference(client, mime, expected):
    payload = base64.b64encode(b"data").decode()
    out = await writes.send_file(client, ROOM, payload, "f", mime_type=mime, dry_run=True)
    assert out["would_send"]["msgtype"] == expected


async def test_send_file_dry_run_uploads_nothing(client, hs):
    payload = base64.b64encode(b"data").decode()
    out = await writes.send_file(client, ROOM, payload, "f.bin", dry_run=True)
    assert out["dry_run"] is True
    assert hs.uploads == [] and hs.sent == []


async def test_send_file_invalid_base64(client, hs):
    with pytest.raises(ValueError, match="base64"):
        await writes.send_file(client, ROOM, "!!!not-base64!!!", "f.bin")
    assert hs.uploads == []


async def test_send_file_size_cap(client, hs, monkeypatch):
    monkeypatch.setenv("MATRIX_MCP_MAX_UPLOAD_BYTES", "16")
    payload = base64.b64encode(b"x" * 17).decode()
    with pytest.raises(ValueError, match="too large"):
        await writes.send_file(client, ROOM, payload, "big.bin")
    assert hs.uploads == []


async def test_send_file_bad_msgtype(client):
    payload = base64.b64encode(b"x").decode()
    with pytest.raises(ValueError, match="msgtype"):
        await writes.send_file(client, ROOM, payload, "f", msgtype="m.sticker")


async def test_send_file_requires_filename(client):
    payload = base64.b64encode(b"x").decode()
    with pytest.raises(ValueError, match="filename"):
        await writes.send_file(client, ROOM, payload, "")


# ---- create room ----

async def test_create_room_happy(client, hs):
    out = await writes.create_room(
        client, name="Plans", topic="scheming",
        invite_user_ids=["@bob:example.org"],
    )
    assert out["room_id"] == "!newroom:example.org"
    assert hs.created_rooms[0]["name"] == "Plans"
    assert hs.created_rooms[0]["invite"] == ["@bob:example.org"]
    assert hs.created_rooms[0]["preset"] == "private_chat"


async def test_create_room_dry_run(client, hs):
    out = await writes.create_room(client, name="Nope", dry_run=True)
    assert out["dry_run"] is True and hs.created_rooms == []


async def test_create_room_bad_preset(client):
    with pytest.raises(ValueError, match="preset"):
        await writes.create_room(client, preset="chaos_chat")


async def test_create_room_bad_invitee(client):
    with pytest.raises(ValueError, match="user_id"):
        await writes.create_room(client, invite_user_ids=["bob"])


# ---- invite ----

async def test_invite_user_happy(client, hs):
    out = await writes.invite_user(client, ROOM, "@bob:example.org", reason="join us")
    assert out["dry_run"] is False
    assert hs.invites == [{"user_id": "@bob:example.org", "reason": "join us"}]


async def test_invite_user_dry_run(client, hs):
    out = await writes.invite_user(client, ROOM, "@bob:example.org", dry_run=True)
    assert out["dry_run"] is True and hs.invites == []


async def test_invite_user_bad_mxid(client, hs):
    with pytest.raises(ValueError, match="user_id"):
        await writes.invite_user(client, ROOM, "bob@example.org")
    assert hs.invites == []


# ---- upload cap configuration ----

def test_upload_cap_default():
    assert writes.max_upload_bytes() == writes.DEFAULT_MAX_UPLOAD_BYTES


def test_upload_cap_garbage_rejected(monkeypatch):
    monkeypatch.setenv("MATRIX_MCP_MAX_UPLOAD_BYTES", "ten megabytes")
    with pytest.raises(RuntimeError, match="MATRIX_MCP_MAX_UPLOAD_BYTES"):
        writes.max_upload_bytes()


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_upload_cap_nonpositive_rejected(monkeypatch, bad):
    monkeypatch.setenv("MATRIX_MCP_MAX_UPLOAD_BYTES", bad)
    with pytest.raises(RuntimeError, match="positive"):
        writes.max_upload_bytes()


def test_server_boot_fails_fast_on_bad_upload_cap(monkeypatch, registry):
    """Misconfiguration must fail at startup, not at the first upload."""
    from matrix_mcp.server import build_mcp

    monkeypatch.setenv("MATRIX_MCP_MAX_UPLOAD_BYTES", "not-a-number")
    with pytest.raises(RuntimeError, match="MATRIX_MCP_MAX_UPLOAD_BYTES"):
        build_mcp(registry)
