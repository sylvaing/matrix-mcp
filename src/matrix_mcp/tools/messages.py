"""Message tools: read recent, send (with dry_run), mark read."""

from __future__ import annotations

import time
import uuid
from typing import Any

from matrix_mcp.client import MatrixClient
from matrix_mcp.validation import (
    validate_body,
    validate_event_id,
    validate_limit,
    validate_room_id,
)


def _normalize_event(ev: dict[str, Any]) -> dict[str, Any]:
    content = ev.get("content") or {}
    return {
        "event_id": ev.get("event_id"),
        "sender": ev.get("sender"),
        "type": ev.get("type"),
        "msgtype": content.get("msgtype"),
        "body": content.get("body"),
        "origin_server_ts": ev.get("origin_server_ts"),
    }


async def get_recent_messages(
    client: MatrixClient,
    room_id: str,
    *,
    limit: int = 20,
    before_cursor: str | None = None,
) -> dict[str, Any]:
    """Return the most recent N messages in a room, newest first.

    Pass `before_cursor` (the `next_cursor` from a prior call) to page backward
    through history. End-to-end-encrypted rooms surface ciphertext events;
    decryption is out of scope (see README §Limitations).
    """
    validate_room_id(room_id)
    validate_limit(limit, lo=1, hi=100)
    d = await client.messages(room_id, limit=limit, from_token=before_cursor, dir_="b")
    chunk = d.get("chunk", [])
    messages = [_normalize_event(ev) for ev in chunk if ev.get("type") in ("m.room.message", "m.room.encrypted")]
    return {
        "messages": messages,
        "next_cursor": d.get("end"),
        "room_id": room_id,
    }


async def send_message(
    client: MatrixClient,
    room_id: str,
    body: str,
    *,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send a plaintext message to a room.

    Set `dry_run=True` to preview what would be sent without actually sending —
    use this when previewing for a human or when the model isn't sure of the target.
    `idempotency_key` is forwarded as the Matrix transaction ID; pass a stable value
    on retry to avoid duplicate sends.
    """
    validate_room_id(room_id)
    validate_body(body)
    txn_id = idempotency_key or f"matrix-mcp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if dry_run:
        return {
            "dry_run": True,
            "would_send": {
                "room_id": room_id,
                "msgtype": "m.text",
                "body": body,
                "txn_id": txn_id,
                "sender": client.cfg.user_id,
            },
        }
    result = await client.send_message(room_id, body, txn_id=txn_id)
    return {
        "dry_run": False,
        "event_id": result.get("event_id"),
        "room_id": room_id,
        "txn_id": txn_id,
    }


async def mark_read(
    client: MatrixClient, room_id: str, event_id: str
) -> dict[str, Any]:
    """Mark a room read up to (and including) `event_id`."""
    validate_room_id(room_id)
    validate_event_id(event_id)
    await client.mark_read(room_id, event_id)
    return {"ok": True, "room_id": room_id, "event_id": event_id}
