"""Room tools against the mock homeserver."""

import pytest

from matrix_mcp.tools import rooms


async def test_list_joined_rooms_happy(client):
    out = await rooms.list_joined_rooms(client)
    assert out["total"] == 5
    assert out["next_cursor"] is None
    ids = [r["room_id"] for r in out["rooms"]]
    assert ids == sorted(ids)
    general = next(r for r in out["rooms"] if r["room_id"] == "!room0:example.org")
    assert general["name"] == "General"
    assert general["topic"] == "Chit-chat"
    assert general["encrypted"] is False
    sealed = next(r for r in out["rooms"] if r["room_id"] == "!room1:example.org")
    assert sealed["encrypted"] is True


async def test_list_joined_rooms_pagination(client):
    page1 = await rooms.list_joined_rooms(client, limit=2, cursor=0)
    assert len(page1["rooms"]) == 2
    assert page1["next_cursor"] == 2
    page2 = await rooms.list_joined_rooms(client, limit=2, cursor=page1["next_cursor"])
    assert len(page2["rooms"]) == 2
    assert page2["next_cursor"] == 4
    page3 = await rooms.list_joined_rooms(client, limit=2, cursor=page2["next_cursor"])
    assert len(page3["rooms"]) == 1
    assert page3["next_cursor"] is None
    all_ids = {r["room_id"] for p in (page1, page2, page3) for r in p["rooms"]}
    assert len(all_ids) == 5


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1000}, {"cursor": -1}])
async def test_list_joined_rooms_bad_params(client, kwargs):
    with pytest.raises(ValueError):
        await rooms.list_joined_rooms(client, **kwargs)


async def test_get_room_info_happy(client):
    out = await rooms.get_room_info(client, "!room1:example.org")
    assert out["name"] == "Sealed"
    assert out["encrypted"] is True
    assert out["encryption_algorithm"] == "m.megolm.v1.aes-sha2"
    assert out["member_count"] == 3


async def test_get_room_info_unset_state_is_none(client):
    out = await rooms.get_room_info(client, "!room2:example.org")
    assert out["name"] is None
    assert out["encrypted"] is False


async def test_get_room_info_rejects_alias(client):
    with pytest.raises(ValueError, match="room_id"):
        await rooms.get_room_info(client, "#general:example.org")


async def test_list_room_members_happy(client):
    out = await rooms.list_room_members(client, "!room0:example.org")
    assert out["total"] == 3
    assert out["members"][0] == {"user_id": "@alice:example.org", "display_name": "Alice"}


async def test_list_room_members_pagination(client):
    page1 = await rooms.list_room_members(client, "!room0:example.org", limit=2)
    assert len(page1["members"]) == 2 and page1["next_cursor"] == 2
    page2 = await rooms.list_room_members(client, "!room0:example.org", limit=2, cursor=2)
    assert len(page2["members"]) == 1 and page2["next_cursor"] is None


async def test_list_room_members_bad_room(client):
    with pytest.raises(ValueError):
        await rooms.list_room_members(client, "not-a-room")
