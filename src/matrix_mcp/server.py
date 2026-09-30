"""Matrix MCP — FastMCP server with multi-homeserver Matrix tools.

The tool surface uses a `tenant` parameter (not a per-tenant name prefix) for
multi-homeserver disambiguation. When only one tenant is configured, `tenant`
defaults to it and callers can omit it entirely. When you add a second
homeserver, the tool surface DOES NOT CHANGE — callers just start passing
`tenant="work"` (or whatever you named it).

Design note: an earlier iteration prefixed tool names per tenant
(`matrix_<tenant>_list_joined_rooms`). Long, dynamic tool names caused some
MCP clients' models to hallucinate short forms that then failed. Stable names
plus an optional `tenant` argument proved far more reliable.

Two transports:
  - HTTP (default): Starlette app with a static-bearer middleware. Refuses to
    start without MCP_BEARER_TOKEN — there is no unauthenticated HTTP mode.
  - stdio: for local single-user clients (Claude Desktop, Claude Code). No
    bearer needed; the transport is the process's own stdin/stdout.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from matrix_mcp.client import MatrixClient, TenantRegistry

logger = logging.getLogger("matrix-mcp")


class BearerAuthMiddleware:
    """Static-bearer auth for the HTTP transport. `/health` is unauthenticated.

    - Comparison is constant-time (hmac.compare_digest).
    - Deliberately does NOT emit `WWW-Authenticate: Bearer` on 401 — some MCP
      clients misclassify that header as an OAuth challenge and break.
    - Pure ASGI (not BaseHTTPMiddleware) so streaming responses pass through
      untouched.
    - Every ASGI scope type is auth-checked, not just "http": websocket
      handshakes carry the same headers and must present the same bearer
      (rejected with a 1008 policy-violation close otherwise), and any scope
      type this middleware doesn't understand is refused outright. Only
      "lifespan" — server startup/shutdown, no client attached — passes
      through unauthenticated. No route added later can accidentally sit in
      front of this check.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise RuntimeError(
                "MCP_BEARER_TOKEN must be set (non-empty) for the HTTP transport. "
                "Generate one with: openssl rand -hex 32"
            )
        self.app = app
        self._token = token.encode()

    def _authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        return auth.startswith(b"Bearer ") and hmac.compare_digest(
            auth[7:], self._token
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type == "http":
            if scope["path"] == "/health" or self._authorized(scope):
                await self.app(scope, receive, send)
                return
            response = Response("unauthorized", status_code=401)
            await response(scope, receive, send)
            return
        if scope_type == "websocket":
            if self._authorized(scope):
                await self.app(scope, receive, send)
                return
            # Reject the handshake: consume websocket.connect, close 1008.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        raise RuntimeError(
            f"matrix-mcp: refusing unauthenticated ASGI scope type {scope_type!r}"
        )


def _sanitized_upstream_error(e: httpx.HTTPStatusError) -> ValueError:
    """Convert an upstream Matrix API error into a message that is safe and
    useful for the MCP client: HTTP status + Matrix errcode/error, never
    headers, never tokens, never the raw response dump."""
    status = e.response.status_code
    errcode, errmsg = None, None
    try:
        body = e.response.json()
        if isinstance(body, dict):
            errcode = body.get("errcode")
            errmsg = body.get("error")
    except Exception:
        pass
    detail = " ".join(str(p) for p in (errcode, errmsg) if p)
    if status in (401, 403):
        hint = (
            "Matrix homeserver rejected the request "
            f"({status} {detail or 'auth error'}). "
            "Check TENANT_<NAME>_TOKEN — the access token may be expired, "
            "revoked, or lacking permission for this room."
        )
    elif status == 429:
        retry_ms = None
        try:
            retry_ms = e.response.json().get("retry_after_ms")
        except Exception:
            pass
        hint = (
            f"Matrix homeserver rate-limited the request (429 {detail or 'M_LIMIT_EXCEEDED'})."
            + (f" Retry after {retry_ms} ms." if retry_ms else " Retry later.")
        )
    elif status == 404:
        hint = (
            f"Matrix homeserver returned 404 ({detail or 'not found'}). "
            "The room/event may not exist or the account may not be a member."
        )
    else:
        hint = f"Matrix homeserver returned {status}" + (f" ({detail})" if detail else "")
    return ValueError(hint)


def build_mcp(registry: TenantRegistry, *, primary_tenant: str | None = None) -> FastMCP:
    """Register the 13 Matrix tools against a FastMCP instance.

    `primary_tenant` is used when a caller omits the `tenant` argument;
    defaults to the alphabetically first configured tenant.
    """
    # Import here so the tool modules stay import-safe without env config.
    from matrix_mcp.tools import messages as messages_tools
    from matrix_mcp.tools import overview as overview_tools
    from matrix_mcp.tools import rooms as rooms_tools
    from matrix_mcp.tools import search as search_tools
    from matrix_mcp.tools import writes as writes_tools

    # Fail fast on a malformed MATRIX_MCP_MAX_UPLOAD_BYTES — at boot, not at
    # the first upload attempt hours later.
    writes_tools.max_upload_bytes()

    primary = primary_tenant or registry.names()[0]
    if primary not in registry.clients:
        raise RuntimeError(
            f"MATRIX_PRIMARY_TENANT={primary!r} is not a configured tenant. "
            f"Available: {registry.names()!r}."
        )
    mcp = FastMCP("matrix-mcp")
    tenant_doc = (
        f"Homeserver to target. Omit to use the primary tenant ({primary!r}). "
        f"Available: {registry.names()!r}."
    )

    def _resolve_tenant(tenant: str | None) -> tuple[str, MatrixClient]:
        name = tenant or primary
        if name not in registry.clients:
            raise ValueError(
                f"Unknown tenant {name!r}. Available: {registry.names()!r}."
            )
        return name, registry.get(name)

    def _log_invocation(
        tool_name: str, tenant: str, status: str, latency_ms: int, **extra: Any
    ) -> None:
        # Structured, single-line, and deliberately free of message bodies,
        # room names, and tokens. Log content: tool, tenant, status, latency.
        fields = {
            "tool": tool_name,
            "tenant": tenant,
            "status": status,
            "latency_ms": latency_ms,
            **extra,
        }
        logger.info(" ".join(f"{k}={v}" for k, v in fields.items()))

    async def _call(
        tool_name: str,
        tenant_hint: str | None,
        fn: Callable[..., Awaitable[Any]],
        **kwargs: Any,
    ) -> Any:
        tenant_name, client = _resolve_tenant(tenant_hint)
        invocation_id = uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        try:
            result = await fn(client, **kwargs)
            _log_invocation(
                tool_name,
                tenant_name,
                "ok",
                int((time.monotonic() - t0) * 1000),
                invocation_id=invocation_id,
            )
            return result
        except httpx.HTTPStatusError as e:
            _log_invocation(
                tool_name,
                tenant_name,
                "error",
                int((time.monotonic() - t0) * 1000),
                invocation_id=invocation_id,
                upstream_status=e.response.status_code,
            )
            raise _sanitized_upstream_error(e) from None
        except httpx.RequestError as e:
            _log_invocation(
                tool_name,
                tenant_name,
                "error",
                int((time.monotonic() - t0) * 1000),
                invocation_id=invocation_id,
                error_type=type(e).__name__,
            )
            raise ValueError(
                f"Could not reach the Matrix homeserver for tenant {tenant_name!r} "
                f"({type(e).__name__}). Check TENANT_*_BASE_URL and network reachability."
            ) from None
        except Exception as e:
            _log_invocation(
                tool_name,
                tenant_name,
                "error",
                int((time.monotonic() - t0) * 1000),
                invocation_id=invocation_id,
                error_type=type(e).__name__,
            )
            raise

    # ---- rooms ----

    @mcp.tool(
        name="matrix_list_joined_rooms",
        description=(
            "List rooms the configured account has joined, with name, topic, and "
            "encryption flag. Cursor-paginated. " + tenant_doc
        ),
    )
    async def t_list_joined_rooms(
        limit: int = 50, cursor: int = 0, tenant: str | None = None
    ) -> dict[str, Any]:
        return await _call(
            "matrix_list_joined_rooms", tenant, rooms_tools.list_joined_rooms,
            limit=limit, cursor=cursor,
        )

    @mcp.tool(
        name="matrix_get_room_info",
        description="Room name, topic, canonical alias, encryption status, member count.",
    )
    async def t_get_room_info(
        room_id: str, tenant: str | None = None
    ) -> dict[str, Any]:
        return await _call(
            "matrix_get_room_info", tenant, rooms_tools.get_room_info,
            room_id=room_id,
        )

    @mcp.tool(
        name="matrix_list_room_members",
        description="List joined members of a room (user_id + display name), paginated.",
    )
    async def t_list_room_members(
        room_id: str, limit: int = 100, cursor: int = 0, tenant: str | None = None
    ) -> dict[str, Any]:
        return await _call(
            "matrix_list_room_members", tenant, rooms_tools.list_room_members,
            room_id=room_id, limit=limit, cursor=cursor,
        )

    # ---- messages ----

    @mcp.tool(
        name="matrix_get_recent_messages",
        description=(
            "Most recent messages in a room, newest first. Pass the returned "
            "next_cursor as before_cursor to page backward through history. "
            "End-to-end-encrypted rooms surface ciphertext (see README)."
        ),
    )
    async def t_get_recent_messages(
        room_id: str,
        limit: int = 20,
        before_cursor: str | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_get_recent_messages", tenant, messages_tools.get_recent_messages,
            room_id=room_id, limit=limit, before_cursor=before_cursor,
        )

    @mcp.tool(
        name="matrix_send_message",
        description=(
            "Send a plaintext message to a room. dry_run=true previews without "
            "sending. idempotency_key is forwarded as the Matrix transaction ID — "
            "pass a stable value on retry to avoid duplicate sends."
        ),
    )
    async def t_send_message(
        room_id: str,
        body: str,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_send_message", tenant, messages_tools.send_message,
            room_id=room_id, body=body, dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(
        name="matrix_mark_read",
        description="Mark a room as read up to (and including) the given event.",
    )
    async def t_mark_read(
        room_id: str, event_id: str, tenant: str | None = None
    ) -> dict[str, Any]:
        return await _call(
            "matrix_mark_read", tenant, messages_tools.mark_read,
            room_id=room_id, event_id=event_id,
        )

    # ---- writes: reactions, attachments, room management ----

    @mcp.tool(
        name="matrix_send_reaction",
        description=(
            "Add an emoji reaction (👍 ❤️ ✅ etc.) to an existing message. "
            "Stacks alongside other reactions, doesn't replace. dry_run-able."
        ),
    )
    async def t_send_reaction(
        room_id: str,
        target_event_id: str,
        emoji: str,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_send_reaction", tenant, writes_tools.send_reaction,
            room_id=room_id, target_event_id=target_event_id, emoji=emoji,
            dry_run=dry_run, idempotency_key=idempotency_key,
        )

    @mcp.tool(
        name="matrix_send_file",
        description=(
            "Send a file attachment to a room. file_b64 is the file contents "
            "base64-encoded. msgtype 'auto' infers from mime_type (m.image / "
            "m.video / m.audio / m.file). dry_run-able."
        ),
    )
    async def t_send_file(
        room_id: str,
        file_b64: str,
        filename: str,
        mime_type: str = "application/octet-stream",
        msgtype: str = "auto",
        dry_run: bool = False,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_send_file", tenant, writes_tools.send_file,
            room_id=room_id, file_b64=file_b64, filename=filename,
            mime_type=mime_type, msgtype=msgtype, dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(
        name="matrix_create_room",
        description=(
            "Create a new Matrix room. preset='private_chat' (default), "
            "'trusted_private_chat', or 'public_chat'. Pass invite_user_ids "
            "as a list of full @user:server.tld ids. dry_run-able."
        ),
    )
    async def t_create_room(
        name: str | None = None,
        topic: str | None = None,
        invite_user_ids: list[str] | None = None,
        is_direct: bool = False,
        preset: str = "private_chat",
        dry_run: bool = False,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_create_room", tenant, writes_tools.create_room,
            name=name, topic=topic, invite_user_ids=invite_user_ids,
            is_direct=is_direct, preset=preset, dry_run=dry_run,
        )

    @mcp.tool(
        name="matrix_invite_user",
        description=(
            "Invite a user to an existing room. user_id is a full Matrix ID "
            "like '@alice:example.org'. dry_run-able."
        ),
    )
    async def t_invite_user(
        room_id: str,
        user_id: str,
        reason: str | None = None,
        dry_run: bool = False,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_invite_user", tenant, writes_tools.invite_user,
            room_id=room_id, user_id=user_id, reason=reason, dry_run=dry_run,
        )

    # ---- search ----

    @mcp.tool(
        name="matrix_search_messages",
        description=(
            "Server-side full-text search across joined rooms (or one room if "
            "room_id is set). Encrypted rooms are not searchable server-side."
        ),
    )
    async def t_search_messages(
        search_term: str,
        room_id: str | None = None,
        limit: int = 20,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_search_messages", tenant, search_tools.search_messages,
            search_term=search_term, room_id=room_id, limit=limit,
        )

    # ---- overview (one filtered /sync) ----

    @mcp.tool(
        name="matrix_list_direct_messages",
        description=(
            "List 1:1 direct-message conversations (from the account's m.direct "
            "map, as Element shows them) with partner, unread counters and a "
            "last-message preview, most recently active first. Read-only. "
            + tenant_doc
        ),
    )
    async def t_list_direct_messages(
        limit: int = 50,
        cursor: int = 0,
        unread_only: bool = False,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        return await _call(
            "matrix_list_direct_messages", tenant, overview_tools.list_direct_messages,
            limit=limit, cursor=cursor, unread_only=unread_only,
        )

    @mcp.tool(
        name="matrix_get_unread",
        description=(
            "Rooms and DMs with unread notifications (same counters as Element's "
            "badges), each with a last-message preview, most recently active "
            "first. Read-only: does not mark anything as read. " + tenant_doc
        ),
    )
    async def t_get_unread(limit: int = 50, tenant: str | None = None) -> dict[str, Any]:
        return await _call(
            "matrix_get_unread", tenant, overview_tools.get_unread, limit=limit,
        )

    logger.info(
        "matrix-mcp ready tenants=%s primary=%r tools=13",
        registry.names(), primary,
    )
    return mcp


async def _health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def create_app(
    registry: TenantRegistry,
    *,
    bearer_token: str,
    primary_tenant: str | None = None,
) -> Starlette:
    """Build the HTTP (streamable-http) ASGI app.

    Stateless HTTP — no Mcp-Session-Id binding, so any request can land on any
    instance behind a load balancer.
    """
    if not bearer_token:
        # Fail closed and fail fast: there is no unauthenticated HTTP mode.
        raise RuntimeError(
            "MCP_BEARER_TOKEN must be set (non-empty) for the HTTP transport. "
            "Generate one with: openssl rand -hex 32 "
            "(or run with --stdio for local, token-less use)."
        )
    mcp = build_mcp(registry, primary_tenant=primary_tenant)
    # Serve the MCP endpoint at exactly /mcp (no trailing-slash redirect) by
    # mounting the FastMCP app at root with its own internal path.
    mcp_asgi = mcp.http_app(transport="streamable-http", stateless_http=True, path="/mcp")

    @contextlib.asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[Any]:
        # Delegate to the FastMCP lifespan, and close every tenant's httpx
        # client on shutdown so connection pools don't leak.
        try:
            async with mcp_asgi.lifespan(app) as state:
                yield state
        finally:
            await registry.aclose_all()

    return Starlette(
        routes=[
            Route("/health", _health),
            Mount("/", app=mcp_asgi),
        ],
        middleware=[Middleware(BearerAuthMiddleware, token=bearer_token)],
        lifespan=_lifespan,
    )
