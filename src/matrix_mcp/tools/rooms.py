"""Room-level tools: list joined rooms, get room info, list members."""

from __future__ import annotations

import asyncio
from typing import Any

from matrix_mcp.client import MatrixClient
from matrix_mcp.validation import validate_limit, validate_room_id


async def list_joined_rooms(
    client: MatrixClient, *, limit: int = 50, cursor: int = 0
) -> dict[str, Any]:
    """List rooms the configured user is joined to, with name/topic/encryption status.

    Pagination is cursor-based (`cursor` is an offset into the alphabetically
    sorted full list; pass the returned `next_cursor` on the next call).
    """
    validate_limit(limit, lo=1, hi=200)
    if not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    all_ids = sorted(await client.joined_rooms())
    end = cursor + limit
    page = all_ids[cursor:end]
    next_cursor = end if end < len(all_ids) else None

    async def hydrate(room_id: str) -> dict[str, Any]:
        name_state, topic_state, enc_state = await asyncio.gather(
            client.room_state(room_id, "m.room.name"),
            client.room_state(room_id, "m.room.topic"),
            client.room_state(room_id, "m.room.encryption"),
            return_exceptions=True,
        )
        name = None
        if isinstance(name_state, dict):
            name = name_state.get("name")
        topic = None
        if isinstance(topic_state, dict):
            topic = topic_state.get("topic")
        encrypted = isinstance(enc_state, dict) and enc_state.get("algorithm") is not None
        return {
            "room_id": room_id,
            "name": name,
            "topic": topic,
            "encrypted": encrypted,
        }

    rooms = await asyncio.gather(*[hydrate(rid) for rid in page])
    return {
        "rooms": rooms,
        "total": len(all_ids),
        "next_cursor": next_cursor,
    }


async def get_room_info(client: MatrixClient, room_id: str) -> dict[str, Any]:
    """Return name, topic, alias, encryption status, and member count for a room."""
    validate_room_id(room_id)
    name_state, topic_state, enc_state, alias_state, members = await asyncio.gather(
        client.room_state(room_id, "m.room.name"),
        client.room_state(room_id, "m.room.topic"),
        client.room_state(room_id, "m.room.encryption"),
        client.room_state(room_id, "m.room.canonical_alias"),
        client.joined_members(room_id),
        return_exceptions=True,
    )
    return {
        "room_id": room_id,
        "name": (name_state or {}).get("name") if isinstance(name_state, dict) else None,
        "topic": (topic_state or {}).get("topic") if isinstance(topic_state, dict) else None,
        "alias": (alias_state or {}).get("alias") if isinstance(alias_state, dict) else None,
        "encrypted": isinstance(enc_state, dict) and (enc_state or {}).get("algorithm") is not None,
        "encryption_algorithm": (enc_state or {}).get("algorithm") if isinstance(enc_state, dict) else None,
        "member_count": len((members or {}).get("joined", {})) if isinstance(members, dict) else 0,
    }


async def list_room_members(
    client: MatrixClient, room_id: str, *, limit: int = 100, cursor: int = 0
) -> dict[str, Any]:
    """List joined members of a room (display name + mxid), paginated."""
    validate_room_id(room_id)
    validate_limit(limit, lo=1, hi=500)
    if not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    d = await client.joined_members(room_id)
    joined: dict[str, dict[str, Any]] = d.get("joined", {})
    items = sorted(
        ({"user_id": uid, "display_name": info.get("display_name")} for uid, info in joined.items()),
        key=lambda x: x["user_id"],
    )
    end = cursor + limit
    page = items[cursor:end]
    next_cursor = end if end < len(items) else None
    return {
        "members": page,
        "total": len(items),
        "next_cursor": next_cursor,
    }
