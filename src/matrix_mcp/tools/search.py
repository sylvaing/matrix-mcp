"""Server-side full-text search across joined rooms."""

from __future__ import annotations

from typing import Any

from matrix_mcp.client import MatrixClient
from matrix_mcp.tools.messages import _normalize_event
from matrix_mcp.validation import validate_limit, validate_room_id


async def search_messages(
    client: MatrixClient,
    search_term: str,
    *,
    room_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Full-text search across joined rooms (or a single room if `room_id` is set).

    Returns the top `limit` matching events, most-recent first. Encrypted rooms
    are not searchable server-side.
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term must be non-empty")
    validate_limit(limit, lo=1, hi=100)
    if room_id is not None:
        validate_room_id(room_id)
    d = await client.search(search_term, room_id=room_id, limit=limit)
    cat = (d.get("search_categories") or {}).get("room_events") or {}
    results = cat.get("results") or []
    hits = []
    for r in results:
        ev = r.get("result") or {}
        hits.append({
            "rank": r.get("rank"),
            **_normalize_event(ev),
            "room_id": ev.get("room_id"),
        })
    return {
        "hits": hits,
        "count": cat.get("count", len(hits)),
        "search_term": search_term,
    }
