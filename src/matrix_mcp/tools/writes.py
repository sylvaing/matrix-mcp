"""Richer write tools: reactions, file attachments, room create, invite.

`send_message` (in messages.py) stays the workhorse; these cover the rest of
the everyday write surface. All of them support dry_run previews.
"""
from __future__ import annotations

import base64 as _b64
import os
import time
import uuid
from typing import Any

from matrix_mcp.client import MatrixClient
from matrix_mcp.validation import validate_event_id, validate_room_id, validate_user_id

# Cap decoded upload size. MCP transports JSON, so huge files arrive as huge
# base64 strings; this bounds server memory and keeps requests below typical
# homeserver upload limits. Override with MATRIX_MCP_MAX_UPLOAD_BYTES.
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

_ROOM_PRESETS = ("private_chat", "trusted_private_chat", "public_chat")


def max_upload_bytes() -> int:
    """Parse and validate MATRIX_MCP_MAX_UPLOAD_BYTES.

    Called once at server startup (build_mcp) so a malformed value fails the
    boot, and again per upload so tests and long-lived processes see the
    current environment. Raises RuntimeError with a clear message on garbage.
    """
    raw = os.environ.get("MATRIX_MCP_MAX_UPLOAD_BYTES")
    if raw is None:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"MATRIX_MCP_MAX_UPLOAD_BYTES must be an integer number of bytes, "
            f"got {raw!r}"
        ) from None
    if value <= 0:
        raise RuntimeError(
            f"MATRIX_MCP_MAX_UPLOAD_BYTES must be positive, got {value}"
        )
    return value


async def send_reaction(
    client: MatrixClient,
    room_id: str,
    target_event_id: str,
    emoji: str,
    *,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Add an emoji reaction to an existing message.

    `emoji` is the literal emoji character (e.g. "👍", "❤️", "✅") or a short
    text annotation. Stacks alongside other reactions — does not replace.

    Set dry_run=True to preview without sending.
    """
    validate_room_id(room_id)
    validate_event_id(target_event_id)
    if not emoji or not emoji.strip():
        raise ValueError("emoji must be non-empty")
    if len(emoji) > 100:
        raise ValueError("emoji/annotation too long (max 100 chars)")
    txn_id = idempotency_key or f"matrix-mcp-rxn_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    if dry_run:
        return {"dry_run": True, "would_react": {
            "room_id": room_id, "target_event_id": target_event_id,
            "emoji": emoji, "txn_id": txn_id,
        }}
    result = await client.send_reaction(room_id, target_event_id, emoji, txn_id=txn_id)
    return {"dry_run": False, "room_id": room_id, "event_id": result.get("event_id"),
            "reacted_to": target_event_id, "emoji": emoji}


async def send_file(
    client: MatrixClient,
    room_id: str,
    file_b64: str,
    filename: str,
    *,
    mime_type: str = "application/octet-stream",
    msgtype: str = "auto",
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send a file attachment to a room.

    `file_b64` is the file contents base64-encoded (MCP transports JSON;
    binary must be base64). `msgtype` is one of: "m.image" (PNG/JPEG/GIF),
    "m.video" (MP4 etc.), "m.audio" (MP3 etc.), "m.file" (everything else),
    or "auto" to infer from the mime_type.

    Set dry_run=True to preview the upload+send plan without executing.
    """
    validate_room_id(room_id)
    if not filename or not filename.strip():
        raise ValueError("filename required")
    if msgtype not in ("auto", "m.image", "m.video", "m.audio", "m.file"):
        raise ValueError(
            "msgtype must be one of: auto, m.image, m.video, m.audio, m.file"
        )
    try:
        content = _b64.b64decode(file_b64, validate=True)
    except Exception as e:
        raise ValueError(f"file_b64 is not valid base64: {e}") from None
    size = len(content)
    cap = max_upload_bytes()
    if size > cap:
        raise ValueError(
            f"file too large ({size} bytes; max {cap}). "
            "Raise MATRIX_MCP_MAX_UPLOAD_BYTES if your homeserver allows more."
        )
    if msgtype == "auto":
        if mime_type.startswith("image/"):
            msgtype = "m.image"
        elif mime_type.startswith("video/"):
            msgtype = "m.video"
        elif mime_type.startswith("audio/"):
            msgtype = "m.audio"
        else:
            msgtype = "m.file"
    txn_id = idempotency_key or f"matrix-mcp-file_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    if dry_run:
        return {"dry_run": True, "would_send": {
            "room_id": room_id, "filename": filename, "mime_type": mime_type,
            "msgtype": msgtype, "size_bytes": size, "txn_id": txn_id,
        }}
    upload = await client.upload_media(content, mime_type, filename=filename)
    mxc = upload.get("content_uri")
    if not mxc:
        raise ValueError("media upload returned no content_uri")
    sent = await client.send_file(
        room_id, mxc, filename,
        msgtype=msgtype, mime=mime_type, size=size, txn_id=txn_id,
    )
    return {"dry_run": False, "room_id": room_id,
            "event_id": sent.get("event_id"), "mxc": mxc,
            "filename": filename, "size_bytes": size, "msgtype": msgtype}


async def create_room(
    client: MatrixClient,
    name: str | None = None,
    topic: str | None = None,
    invite_user_ids: list[str] | None = None,
    is_direct: bool = False,
    preset: str = "private_chat",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a new Matrix room.

    `preset` is one of: "private_chat" (default, invite-only),
    "trusted_private_chat" (private, all members admin), "public_chat"
    (anyone can join). Set `is_direct=True` for 1:1 DM-style rooms.
    `invite_user_ids` is a list of full Matrix IDs (e.g. "@alice:example.org").
    """
    if preset not in _ROOM_PRESETS:
        raise ValueError(f"preset must be one of {_ROOM_PRESETS}")
    for uid in invite_user_ids or []:
        validate_user_id(uid)
    if dry_run:
        return {"dry_run": True, "would_create": {
            "name": name, "topic": topic, "preset": preset,
            "is_direct": is_direct, "invite": invite_user_ids or [],
        }}
    result = await client.create_room(
        name=name, topic=topic, invite=invite_user_ids,
        is_direct=is_direct, preset=preset,
    )
    return {"dry_run": False, "room_id": result.get("room_id"),
            "room_alias": result.get("room_alias")}


async def invite_user(
    client: MatrixClient,
    room_id: str,
    user_id: str,
    reason: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Invite a user to an existing room. `user_id` is a full Matrix ID
    like "@alice:example.org"."""
    validate_room_id(room_id)
    validate_user_id(user_id)
    if dry_run:
        return {"dry_run": True, "would_invite": {
            "room_id": room_id, "user_id": user_id, "reason": reason,
        }}
    result = await client.invite_user(room_id, user_id, reason=reason)
    return {"dry_run": False, "room_id": room_id, "user_id": user_id, "response": result}
