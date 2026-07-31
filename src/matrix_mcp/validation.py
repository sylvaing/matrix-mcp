"""Input validation for Matrix identifiers and tool parameters.

Validation failures raise ValueError with a message safe to surface to the
MCP client (no credentials, no upstream internals).
"""

from __future__ import annotations

# Matrix common-identifier grammar caps identifiers at 255 bytes.
_MAX_ID_LEN = 255

# m.room.message events are capped at 65536 bytes for the whole event PDU;
# leave headroom for the envelope.
MAX_BODY_CHARS = 60_000


def validate_room_id(room_id: str) -> str:
    """Room IDs look like '!opaque:server.tld'. Aliases ('#…') are not accepted
    by the endpoints this server calls — resolve the alias first."""
    if not isinstance(room_id, str) or not room_id:
        raise ValueError("room_id is required")
    if not room_id.startswith("!") or ":" not in room_id:
        raise ValueError(
            f"invalid room_id {room_id!r}: expected the form '!opaque:example.org' "
            "(room aliases starting with '#' are not accepted here)"
        )
    if len(room_id) > _MAX_ID_LEN or any(c.isspace() for c in room_id):
        raise ValueError("invalid room_id: too long or contains whitespace")
    return room_id


def validate_event_id(event_id: str) -> str:
    """Event IDs look like '$opaque' (room v3+) or '$opaque:server.tld' (v1/v2)."""
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id is required")
    if not event_id.startswith("$"):
        raise ValueError(f"invalid event_id {event_id!r}: expected it to start with '$'")
    if len(event_id) > _MAX_ID_LEN or any(c.isspace() for c in event_id):
        raise ValueError("invalid event_id: too long or contains whitespace")
    return event_id


def validate_user_id(user_id: str) -> str:
    """User IDs (MXIDs) look like '@localpart:server.tld'."""
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    if not user_id.startswith("@") or ":" not in user_id:
        raise ValueError(
            f"invalid user_id {user_id!r}: expected the form '@alice:example.org'"
        )
    if len(user_id) > _MAX_ID_LEN or any(c.isspace() for c in user_id):
        raise ValueError("invalid user_id: too long or contains whitespace")
    return user_id


def validate_limit(limit: int, *, lo: int = 1, hi: int = 100, name: str = "limit") -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not lo <= limit <= hi:
        raise ValueError(f"{name} must be an integer in {lo}..{hi}")
    return limit


def validate_body(body: str) -> str:
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body must be a non-empty string")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body too long ({len(body)} chars; max {MAX_BODY_CHARS})")
    return body
