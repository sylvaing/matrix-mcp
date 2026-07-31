"""Search tool against the mock homeserver."""

import pytest

from matrix_mcp.tools import search


async def test_search_happy(client):
    out = await search.search_messages(client, "message")
    assert out["count"] == 3
    assert out["search_term"] == "message"
    top = out["hits"][0]
    assert top["rank"] == 1.0
    assert top["body"] == "message 0"
    assert top["room_id"] == "!room0:example.org"


async def test_search_scoped_to_room(client, hs):
    out = await search.search_messages(client, "message", room_id="!room0:example.org")
    assert out["count"] == 3
    out = await search.search_messages(client, "message", room_id="!other:example.org")
    assert out["count"] == 0


async def test_search_empty_term_rejected(client):
    with pytest.raises(ValueError, match="search_term"):
        await search.search_messages(client, "  ")


async def test_search_bad_limit(client):
    with pytest.raises(ValueError, match="limit"):
        await search.search_messages(client, "hi", limit=0)


async def test_search_bad_room_id(client):
    with pytest.raises(ValueError, match="room_id"):
        await search.search_messages(client, "hi", room_id="general")
