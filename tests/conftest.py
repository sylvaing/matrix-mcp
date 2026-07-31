"""Shared fixtures: a mock Matrix homeserver behind httpx.MockTransport.

No real network I/O happens anywhere in the suite.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from matrix_mcp.client import MatrixClient, TenantRegistry

BASE_URL = "https://matrix.example.org"
USER_ID = "@alice:example.org"
TOKEN = "sekrit-matrix-token"

ENV_ONE_TENANT = {
    "TENANT_MAIN_BASE_URL": BASE_URL,
    "TENANT_MAIN_USER_ID": USER_ID,
    "TENANT_MAIN_TOKEN": TOKEN,
}

ENV_TWO_TENANTS = {
    **ENV_ONE_TENANT,
    "TENANT_WORK_BASE_URL": "https://matrix.work.example",
    "TENANT_WORK_USER_ID": "@alice:work.example",
    "TENANT_WORK_TOKEN": "work-token",
}

ROOM_IDS = [f"!room{i}:example.org" for i in range(5)]
ROOM_STATE = {
    "!room0:example.org": {
        "m.room.name": {"name": "General"},
        "m.room.topic": {"topic": "Chit-chat"},
    },
    "!room1:example.org": {
        "m.room.name": {"name": "Sealed"},
        "m.room.encryption": {"algorithm": "m.megolm.v1.aes-sha2"},
    },
}
MEMBERS = {
    "@alice:example.org": {"display_name": "Alice"},
    "@bob:example.org": {"display_name": "Bob"},
    "@carol:example.org": {"display_name": None},
}


def _event(i: int, room_id: str = "!room0:example.org") -> dict:
    return {
        "event_id": f"$evt{i}",
        "sender": "@bob:example.org",
        "type": "m.room.message",
        "content": {"msgtype": "m.text", "body": f"message {i}"},
        "origin_server_ts": 1_700_000_000_000 + i,
        "room_id": room_id,
    }


class MockHomeserver:
    """Implements just enough of the Matrix C-S v3 API for the suite."""

    def __init__(self) -> None:
        self.fail_mode: str | None = None  # None|"auth"|"ratelimit"|"network"|"forbidden"
        self.sent: list[dict] = []          # recorded message/reaction sends
        self.uploads: list[dict] = []       # recorded media uploads
        self.receipts: list[tuple[str, str]] = []
        self.created_rooms: list[dict] = []
        self.invites: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_mode == "network":
            raise httpx.ConnectError("boom", request=request)
        if self.fail_mode == "auth":
            return httpx.Response(
                401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid access token"}
            )
        if self.fail_mode == "forbidden":
            return httpx.Response(
                403, json={"errcode": "M_FORBIDDEN", "error": "Not a member"}
            )
        if self.fail_mode == "ratelimit":
            return httpx.Response(
                429,
                json={
                    "errcode": "M_LIMIT_EXCEEDED",
                    "error": "Too Many Requests",
                    "retry_after_ms": 2000,
                },
            )
        return self._route(request)

    # --- routing ---

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/_matrix/client/v3/joined_rooms":
            return httpx.Response(200, json={"joined_rooms": list(ROOM_IDS)})

        m = re.fullmatch(r"/_matrix/client/v3/rooms/([^/]+)/state/([^/]+)", path)
        if m:
            from urllib.parse import unquote

            room_id, ev_type = unquote(m.group(1)), unquote(m.group(2))
            content = ROOM_STATE.get(room_id, {}).get(ev_type)
            if content is None:
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "Not found"})
            return httpx.Response(200, json=content)

        if re.fullmatch(r"/_matrix/client/v3/rooms/[^/]+/joined_members", path):
            return httpx.Response(200, json={"joined": MEMBERS})

        if re.fullmatch(r"/_matrix/client/v3/rooms/[^/]+/messages", path):
            frm = request.url.params.get("from")
            limit = int(request.url.params.get("limit", "10"))
            start = int(frm[1:]) if frm else 0  # tokens look like "t<offset>"
            events = [_event(i) for i in range(start, min(start + limit, 30))]
            end = f"t{start + limit}" if start + limit < 30 else None
            body = {"chunk": events, "start": f"t{start}"}
            if end:
                body["end"] = end
            return httpx.Response(200, json=body)

        m = re.fullmatch(
            r"/_matrix/client/v3/rooms/[^/]+/send/(m\.room\.message|m\.reaction)/([^/]+)", path
        )
        if m and method == "PUT":
            payload = json.loads(request.content)
            self.sent.append({"path": path, "type": m.group(1), "txn_id": m.group(2), **payload})
            return httpx.Response(200, json={"event_id": f"$sent_{m.group(2)}"})

        m = re.fullmatch(r"/_matrix/client/v3/rooms/[^/]+/receipt/m\.read/([^/]+)", path)
        if m and method == "POST":
            self.receipts.append((path, m.group(1)))
            return httpx.Response(200, json={})

        if path == "/_matrix/media/v3/upload" and method == "POST":
            self.uploads.append({
                "content_type": request.headers.get("content-type"),
                "filename": request.url.params.get("filename"),
                "size": len(request.content),
            })
            return httpx.Response(200, json={"content_uri": "mxc://example.org/mediaid123"})

        if path == "/_matrix/client/v3/createRoom" and method == "POST":
            payload = json.loads(request.content)
            self.created_rooms.append(payload)
            return httpx.Response(200, json={"room_id": "!newroom:example.org"})

        if re.fullmatch(r"/_matrix/client/v3/rooms/[^/]+/invite", path) and method == "POST":
            self.invites.append(json.loads(request.content))
            return httpx.Response(200, json={})

        if path == "/_matrix/client/v3/search" and method == "POST":
            payload = json.loads(request.content)
            criteria = payload["search_categories"]["room_events"]
            term = criteria["search_term"]
            room_filter = criteria.get("filter", {}).get("rooms")
            results = [
                {"rank": 1.0 - 0.1 * i, "result": _event(i)}
                for i in range(3)
                if term in f"message {i}" or term == "message"
            ]
            if room_filter and "!room0:example.org" not in room_filter:
                results = []
            return httpx.Response(200, json={
                "search_categories": {"room_events": {"results": results, "count": len(results)}}
            })

        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED", "error": f"no route {path}"})


@pytest.fixture()
def hs() -> MockHomeserver:
    return MockHomeserver()


@pytest.fixture()
def transport(hs: MockHomeserver) -> httpx.MockTransport:
    return httpx.MockTransport(hs.handler)


@pytest.fixture()
def registry(transport: httpx.MockTransport) -> TenantRegistry:
    return TenantRegistry(ENV_ONE_TENANT, transport=transport)


@pytest.fixture()
def client(registry: TenantRegistry) -> MatrixClient:
    return registry.get("main")
