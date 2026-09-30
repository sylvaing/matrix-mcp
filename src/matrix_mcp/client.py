"""Async httpx wrapper around the Matrix Client-Server v3 API.

Multi-homeserver: TenantRegistry parses TENANT_<NAME>_* env vars at startup
and exposes one MatrixClient per tenant ("tenant" = one homeserver + one
account on it). Tools take an optional `tenant` argument to pick which one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(frozen=True, slots=True)
class TenantConfig:
    name: str            # lowercased tenant name (e.g. "personal")
    base_url: str        # https://matrix.example.org
    user_id: str         # @alice:example.org
    token: str           # Matrix access token for that user


class MatrixClient:
    """Single-tenant async Matrix C-S v3 client.

    `transport` is an optional httpx transport override, used by the test
    suite to substitute a mock homeserver. Production code never passes it.
    """

    def __init__(
        self, cfg: TenantConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _q(s: str) -> str:
        return quote(s, safe="")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = await self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, json: dict[str, Any]) -> Any:
        r = await self._http.post(path, json=json)
        r.raise_for_status()
        return r.json()

    async def put(self, path: str, json: dict[str, Any]) -> Any:
        r = await self._http.put(path, json=json)
        r.raise_for_status()
        return r.json()

    # ---- typed call helpers ----

    async def joined_rooms(self) -> list[str]:
        d = await self.get("/_matrix/client/v3/joined_rooms")
        return d.get("joined_rooms", [])

    async def room_state(self, room_id: str, event_type: str) -> dict[str, Any] | None:
        """Return the state event content, or None if the event isn't set."""
        try:
            return await self.get(
                f"/_matrix/client/v3/rooms/{self._q(room_id)}/state/{event_type}"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def joined_members(self, room_id: str) -> dict[str, Any]:
        return await self.get(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/joined_members"
        )

    async def messages(
        self,
        room_id: str,
        *,
        limit: int = 20,
        from_token: str | None = None,
        dir_: str = "b",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"dir": dir_, "limit": limit}
        if from_token:
            params["from"] = from_token
        return await self.get(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/messages",
            params=params,
        )

    async def send_message(
        self, room_id: str, body: str, *, txn_id: str
    ) -> dict[str, Any]:
        return await self.put(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/send/m.room.message/{self._q(txn_id)}",
            json={"msgtype": "m.text", "body": body},
        )

    async def mark_read(self, room_id: str, event_id: str) -> dict[str, Any]:
        return await self.post(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/receipt/m.read/{self._q(event_id)}",
            json={},
        )

    # ---- richer writes: reactions, attachments, room management ----

    async def send_reaction(
        self, room_id: str, target_event_id: str, key: str, *, txn_id: str
    ) -> dict[str, Any]:
        """Send an m.reaction event annotating another event."""
        return await self.put(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/send/m.reaction/{self._q(txn_id)}",
            json={"m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target_event_id,
                "key": key,
            }},
        )

    async def upload_media(self, content: bytes, content_type: str, filename: str | None = None) -> dict[str, Any]:
        """Upload a file to the Matrix media repo; returns {content_uri: mxc://...}."""
        params = {"filename": filename} if filename else None
        r = await self._http.post(
            "/_matrix/media/v3/upload",
            params=params,
            content=content,
            headers={"Content-Type": content_type},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        r.raise_for_status()
        return r.json()

    async def send_file(
        self, room_id: str, mxc_uri: str, filename: str, *,
        msgtype: str = "m.file", mime: str | None = None, size: int | None = None,
        txn_id: str,
    ) -> dict[str, Any]:
        """Send a file/image/video event referencing an already-uploaded media uri."""
        body: dict[str, Any] = {
            "msgtype": msgtype,
            "body": filename,
            "url": mxc_uri,
            "info": {},
        }
        if mime:
            body["info"]["mimetype"] = mime
        if size is not None:
            body["info"]["size"] = size
        return await self.put(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/send/m.room.message/{self._q(txn_id)}",
            json=body,
        )

    async def create_room(
        self, *, name: str | None = None, topic: str | None = None,
        invite: list[str] | None = None, is_direct: bool = False,
        preset: str = "private_chat", visibility: str = "private",
    ) -> dict[str, Any]:
        """Create a new Matrix room. Returns {room_id, ...}."""
        body: dict[str, Any] = {"preset": preset, "visibility": visibility}
        if name:
            body["name"] = name
        if topic:
            body["topic"] = topic
        if invite:
            body["invite"] = invite
        if is_direct:
            body["is_direct"] = True
        return await self.post("/_matrix/client/v3/createRoom", json=body)

    async def invite_user(self, room_id: str, user_id: str, reason: str | None = None) -> dict[str, Any]:
        """Invite a user to a room."""
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        return await self.post(
            f"/_matrix/client/v3/rooms/{self._q(room_id)}/invite",
            json=body,
        )

    async def sync_overview(self) -> dict[str, Any]:
        """One-shot, heavily filtered initial /sync: per joined room, the name,
        unread counters and the last message, plus the global `m.direct` map.

        - `timeout=0`: return immediately, never long-poll.
        - `set_presence=offline`: reading must not flip the account to "online".
        - No `since`: stateless — every call is a fresh snapshot.
        """
        sync_filter = {
            "presence": {"types": []},
            "account_data": {"types": ["m.direct"]},
            "room": {
                "timeline": {"limit": 1, "types": ["m.room.message", "m.room.encrypted"]},
                "state": {
                    "lazy_load_members": True,
                    "types": ["m.room.name", "m.room.canonical_alias", "m.room.member"],
                },
                "ephemeral": {"types": []},
                "account_data": {"types": []},
                "include_leave": False,
            },
        }
        r = await self._http.get(
            "/_matrix/client/v3/sync",
            params={
                "filter": json.dumps(sync_filter, separators=(",", ":")),
                "timeout": 0,
                "set_presence": "offline",
            },
            # An initial sync can be slow on large accounts, even filtered.
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
        r.raise_for_status()
        return r.json()

    async def search(
        self, search_term: str, *, room_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        criteria: dict[str, Any] = {
            "search_term": search_term,
            "order_by": "recent",
            "filter": {"limit": limit},
        }
        if room_id:
            criteria["filter"]["rooms"] = [room_id]
        return await self.post(
            "/_matrix/client/v3/search",
            json={"search_categories": {"room_events": criteria}},
        )


class TenantRegistry:
    """Parses TENANT_<NAME>_{BASE_URL,USER_ID,TOKEN} env triples.

    `env` defaults to os.environ; tests pass a plain dict.
    `transport` is forwarded to every MatrixClient (tests only).
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if env is None:
            env = os.environ
        self.clients: dict[str, MatrixClient] = {}
        seen: dict[str, dict[str, str]] = {}
        for key, value in env.items():
            if not key.startswith("TENANT_"):
                continue
            rest = key[len("TENANT_"):]
            for suffix in ("_BASE_URL", "_USER_ID", "_TOKEN"):
                if rest.endswith(suffix):
                    tenant = rest[: -len(suffix)].lower()
                    field = suffix[1:].lower()
                    seen.setdefault(tenant, {})[field] = value
                    break
        for tenant, fields in seen.items():
            missing = {"base_url", "user_id", "token"} - fields.keys()
            if missing:
                raise RuntimeError(
                    f"Tenant '{tenant}' missing required env: {sorted(missing)}"
                )
            cfg = TenantConfig(
                name=tenant,
                base_url=fields["base_url"].rstrip("/"),
                user_id=fields["user_id"],
                token=fields["token"],
            )
            self.clients[tenant] = MatrixClient(cfg, transport=transport)
        if not self.clients:
            raise RuntimeError(
                "No tenants configured. Set TENANT_<NAME>_{BASE_URL,USER_ID,TOKEN}."
            )

    def names(self) -> list[str]:
        return sorted(self.clients.keys())

    def get(self, tenant: str) -> MatrixClient:
        return self.clients[tenant]

    async def aclose_all(self) -> None:
        for c in self.clients.values():
            await c.aclose()
