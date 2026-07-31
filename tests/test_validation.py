"""Identifier / parameter validators."""

import pytest

from matrix_mcp.validation import (
    validate_body,
    validate_event_id,
    validate_limit,
    validate_room_id,
    validate_user_id,
)


class TestRoomId:
    def test_valid(self):
        assert validate_room_id("!abc:example.org") == "!abc:example.org"

    @pytest.mark.parametrize("bad", [
        "", "abc", "#alias:example.org", "!nocolon", "!has space:example.org",
        "@user:example.org", "!" + "x" * 300 + ":example.org",
    ])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_room_id(bad)


class TestEventId:
    def test_valid_v3(self):
        assert validate_event_id("$abc123") == "$abc123"

    def test_valid_v1(self):
        assert validate_event_id("$abc:example.org") == "$abc:example.org"

    @pytest.mark.parametrize("bad", ["", "abc", "!room:x", "$has space"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_event_id(bad)


class TestUserId:
    def test_valid(self):
        assert validate_user_id("@alice:example.org") == "@alice:example.org"

    @pytest.mark.parametrize("bad", ["", "alice", "@nocolon", "@a b:example.org"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_user_id(bad)


class TestLimit:
    def test_bounds(self):
        assert validate_limit(1) == 1
        assert validate_limit(100) == 100

    @pytest.mark.parametrize("bad", [0, -1, 101, True, "5"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_limit(bad)


class TestBody:
    def test_valid(self):
        assert validate_body("hello") == "hello"

    @pytest.mark.parametrize("bad", ["", "   ", "x" * 60_001])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_body(bad)
