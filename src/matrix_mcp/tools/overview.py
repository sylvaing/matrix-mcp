"""Overview tools built on one filtered /sync: direct messages and unread rooms.

Both tools answer "what's going on?" questions that would otherwise need one
request per room. They read the snapshot returned by `sync_overview` and never
write anything: no read receipts, no presence change.
"""

from __future__ import annotations

from typing import Any

from matrix_mcp.client import MatrixClient
from matrix_mcp.validation import validate_limit

# Last-message previews are for triage, not reading; full text is one
# matrix_get_recent_messages call away.
PREVIEW_CHARS = 300


def _dm_partners(sync: dict[str, Any]) -> dict[str, str]:
    """Invert the `m.direct` account-data map into {room_id: partner_user_id}."""
    for ev in (sync.get("account_data") or {}).get("events") or []:
        if ev.get("type") == "m.direct":
            content = ev.get("content") or {}
            return {
                room_id: user_id
                for user_id, room_ids in content.items()
                if isinstance(room_ids, list)
                for room_id in room_ids
            }
    return {}


def _summarize_room(
    room_id: str, room: dict[str, Any], dm_partner: str | None
) -> dict[str, Any]:
    events = [
        *((room.get("state") or {}).get("events") or []),
        *((room.get("timeline") or {}).get("events") or []),
    ]
    name = alias = None
    display_names: dict[str, str | None] = {}
    for ev in events:
        content = ev.get("content") or {}
        ev_type = ev.get("type")
        if ev_type == "m.room.name":
            name = content.get("name") or None
        elif ev_type == "m.room.canonical_alias":
            alias = content.get("alias") or None
        elif ev_type == "m.room.member" and ev.get("state_key"):
            display_names[ev["state_key"]] = content.get("displayname")

    summary = room.get("summary") or {}
    heroes = summary.get("m.heroes") or []
    if dm_partner:
        name = name or display_names.get(dm_partner) or dm_partner
    elif not name:
        name = alias or ", ".join(display_names.get(h) or h for h in heroes) or None

    last_message = None
    timeline = [
        ev for ev in (room.get("timeline") or {}).get("events") or []
        if ev.get("type") in ("m.room.message", "m.room.encrypted")
    ]
    if timeline:
        ev = timeline[-1]
        body = (ev.get("content") or {}).get("body")
        if isinstance(body, str) and len(body) > PREVIEW_CHARS:
            body = body[:PREVIEW_CHARS] + "…"
        last_message = {
            "event_id": ev.get("event_id"),
            "sender": ev.get("sender"),
            "sender_name": display_names.get(ev.get("sender")),
            "body": body,
            "encrypted": ev.get("type") == "m.room.encrypted",
            "origin_server_ts": ev.get("origin_server_ts"),
        }

    unread = room.get("unread_notifications") or {}
    return {
        "room_id": room_id,
        "name": name,
        "is_direct": dm_partner is not None,
        "dm_user_id": dm_partner,
        "member_count": summary.get("m.joined_member_count"),
        "unread_count": unread.get("notification_count", 0),
        "highlight_count": unread.get("highlight_count", 0),
        "last_message": last_message,
    }


def _last_ts(room: dict[str, Any]) -> int:
    return (room.get("last_message") or {}).get("origin_server_ts") or 0


async def _snapshot(client: MatrixClient) -> list[dict[str, Any]]:
    """All joined rooms, summarized, most recently active first."""
    sync = await client.sync_overview()
    partners = _dm_partners(sync)
    joined = (sync.get("rooms") or {}).get("join") or {}
    rooms = [
        _summarize_room(room_id, room, partners.get(room_id))
        for room_id, room in joined.items()
    ]
    rooms.sort(key=_last_ts, reverse=True)
    return rooms


async def list_direct_messages(
    client: MatrixClient, *, limit: int = 50, cursor: int = 0, unread_only: bool = False
) -> dict[str, Any]:
    """List 1:1 conversations (per the account's `m.direct` map, as Element
    does), with partner, unread counters and a last-message preview.

    Most recently active first. Cursor is an offset into that ordering.
    """
    validate_limit(limit, lo=1, hi=200)
    if not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    dms = [r for r in await _snapshot(client) if r["is_direct"]]
    if unread_only:
        dms = [r for r in dms if r["unread_count"] or r["highlight_count"]]
    end = cursor + limit
    return {
        "direct_messages": dms[cursor:end],
        "total": len(dms),
        "next_cursor": end if end < len(dms) else None,
    }


async def get_unread(client: MatrixClient, *, limit: int = 50) -> dict[str, Any]:
    """Rooms and DMs with unread notifications, most recently active first.

    Counters are the homeserver's push-rule notification counts — the same
    numbers Element badges with — so rooms muted or set to mentions-only only
    count what would notify. Reading does not mark anything as read.
    """
    validate_limit(limit, lo=1, hi=200)
    unread = [
        r for r in await _snapshot(client) if r["unread_count"] or r["highlight_count"]
    ]
    return {
        "rooms": unread[:limit],
        "total_rooms": len(unread),
        "total_unread": sum(r["unread_count"] for r in unread),
        "total_highlights": sum(r["highlight_count"] for r in unread),
    }
