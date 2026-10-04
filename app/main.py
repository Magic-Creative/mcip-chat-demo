"""The demo backend: FastAPI app, routes, session, CSRF and limits.

The browser only ever talks to this app (never to MCip): the routes below are
the demo's own API, and every MCip call happens server-side with the signed-in
user's own key (Guide §1, §11).

Security posture (Guide §11, and "Public-exposure safeguards" in the issue):

* keys are write-only — they go in through ``POST /api/connection`` and are
  never part of any response, URL or log line;
* the session is an HttpOnly, SameSite=Lax signed cookie; state-changing
  routes require a double-submit CSRF token (header + cookie + session);
* responses carry a strict CSP (``default-src 'self'``, no inline code), so
  the UI is plain files under ``static/`` with no build step;
* demo-side rate limits: login 5/min per IP, chat 20/min per user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, load_settings
from app.errors import DemoError
from app.mcip import ExtApiError, McipClient
from app.relay import relay_turn, translate_ext_error
from app.store import Store, key_prefix, redact, verify_password

logger = logging.getLogger("demo")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9_.\-]{2,31}$"
MIN_PASSWORD_LENGTH = 8
COOKIE_MAX_AGE = 14 * 24 * 3600

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)


# ---------------------------------------------------------------------------
# Request payloads
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)

    @field_validator("username")
    @classmethod
    def username_is_valid(cls, value: str) -> str:
        import re

        if not re.fullmatch(USERNAME_PATTERN, value):
            raise ValueError("Use 3-32 characters: a-z, 0-9, dot, dash, underscore.")
        return value


class ConnectRequest(BaseModel):
    #: Only a length guard here: the route's ``ss_pat_`` check owns the
    #: format error, so a wrong paste gets DEMO_KEY_FORMAT, not a 422.
    key: str = Field(min_length=1, max_length=256)


class WorkspaceRequest(BaseModel):
    workspace_id: int


class ChatRequest(BaseModel):
    """One turn, from the browser (Guide §4.2)."""

    conversation_id: int | None = None
    message: str = Field(min_length=1, max_length=32_000)
    stream: bool = True
    #: Only sent back by the browser's Retry so the retry reuses the id.
    client_request_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Write a message first.")
        return value


# ---------------------------------------------------------------------------
# Session helpers and the signed-in-user dependency
# ---------------------------------------------------------------------------
# These live at module level rather than inside ``create_app``: under ``from
# __future__ import annotations`` route annotations are strings, and FastAPI
# resolves them against this module's globals — a closure-local ``UserId``
# would silently degrade to a plain query parameter.


def user_id_of(request: Request) -> int | None:
    value = request.session.get("user_id")
    return int(value) if isinstance(value, int) else None


async def require_user(request: Request) -> int:
    user_id = user_id_of(request)
    if user_id is None:
        raise DemoError(
            error_code="DEMO_UNAUTHENTICATED", message="Sign in to continue.", http_status=401
        )
    return user_id


UserId = Annotated[int, Depends(require_user)]


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding window; a demo, not a production limiter)
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > window_s:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after_ms = max(1000, int((window_s - (now - bucket[0])) * 1000))
            return False, retry_after_ms
        bucket.append(now)
        if len(self._hits) > 10_000:  # bound memory; drop empty buckets
            for stale in [k for k, v in self._hits.items() if not v][:1000]:
                del self._hits[stale]
        return True, 0


# ---------------------------------------------------------------------------
# Logging: never let a key reach the log (Guide §11)
# ---------------------------------------------------------------------------


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        return True


def install_redaction() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(RedactingFilter())


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    mcip_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """``mcip_transport`` is where tests plug in the fake MCip (httpx knows
    how to speak to an ASGI app without a socket)."""
    settings = settings or load_settings()
    store = store or Store(settings.db_path, settings.encryption_key)

    app = FastAPI(title="MCip chat demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.limiter = RateLimiter()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="demo_session",
        max_age=COOKIE_MAX_AGE,
        same_site="lax",
        https_only=settings.https_only,
    )

    install_redaction()
    if not settings.https_only:
        logger.warning(
            "DEMO_HTTPS_ONLY is false: session cookies are not marked Secure. "
            "Set DEMO_HTTPS_ONLY=true when serving the demo over HTTPS."
        )

    # -- helpers ------------------------------------------------------------

    def api_error(http_status: int, error_code: str, message: str, **extra: Any) -> DemoError:
        return DemoError(error_code=error_code, message=message, http_status=http_status, **extra)

    def require_csrf(request: Request) -> None:
        """Double-submit: header == cookie == session value (issue spec)."""
        token = request.headers.get("X-CSRF-Token", "")
        cookie = request.cookies.get("demo_csrf", "")
        session_token = request.session.get("csrf", "")
        if not token or not cookie or not session_token:
            raise api_error(403, "DEMO_CSRF", "Missing CSRF token. Reload the page.")
        if not (compare_digest(token, cookie) and compare_digest(token, session_token)):
            raise api_error(403, "DEMO_CSRF", "Invalid CSRF token. Reload the page.")

    def issue_csrf(request: Request, *, rotate: bool = False) -> str:
        """The token for this session; also (re)sets the readable cookie."""
        token = request.session.get("csrf", "")
        if rotate or not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def attach_csrf_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            "demo_csrf",
            token,
            httponly=False,  # the frontend echoes it in X-CSRF-Token
            samesite="lax",
            secure=settings.https_only,
            max_age=COOKIE_MAX_AGE,
        )

    def client_ip(request: Request) -> str:
        # Behind the Cloudflare tunnel the real client address is in
        # CF-Connecting-IP. Only Cloudflare may set it, which is true while the
        # port stays loopback-bound (docker-compose.yml does that); if the port
        # is ever published directly, the header is spoofable and
        # DEMO_TRUST_CF_HEADER must be set to false.
        if settings.trust_cf_header:
            forwarded = request.headers.get("cf-connecting-ip", "").strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    def enforce_limit(key: str, limit: int, message: str) -> None:
        allowed, retry_after_ms = app.state.limiter.allow(key, limit)
        if not allowed:
            raise api_error(429, "DEMO_RATE_LIMITED", message, retry_after_ms=retry_after_ms)

    def connection_view(connection: dict[str, Any] | None) -> dict[str, Any] | None:
        """The connection as the browser may see it — never the key."""
        if connection is None:
            return None
        expires_at = connection.get("expires_at")
        expires_days = None
        if expires_at:
            try:
                moment = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                expires_days = (moment - datetime.now(UTC)).days
            except ValueError:
                expires_at = None
        return {
            "connected": True,
            "display_name": connection.get("display_name"),
            "email": connection.get("email"),
            "api_client_name": connection.get("api_client_name"),
            "key_prefix": connection.get("key_prefix"),
            "expires_at": expires_at,
            "expires_days": expires_days,
            "workspaces": connection.get("workspaces") or [],
            "workspace": (
                {"id": connection["workspace_id"], "name": connection["workspace_name"]}
                if connection.get("workspace_id")
                else None
            ),
            "connected_at": connection.get("connected_at"),
        }

    def mcip_client(key: str) -> McipClient:
        return McipClient(settings.mcip_base_url, key, transport=mcip_transport)

    async def open_mcip(user_id: int) -> McipClient:
        key = await asyncio.to_thread(store.get_api_key, user_id)
        if not key:
            raise api_error(401, "NOT_CONNECTED", "Connect your MCip key first.")
        return mcip_client(key)

    # -- error handling -----------------------------------------------------

    @app.exception_handler(DemoError)
    async def demo_error_handler(_request: Request, exc: DemoError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after_ms:
            headers["Retry-After"] = str((exc.retry_after_ms + 999) // 1000)
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload(), headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            payload = detail
        else:
            payload = {
                "errorCode": f"HTTP_{exc.status_code}",
                "message": str(detail),
                "ui": "fatal",
                "advice": "",
            }
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = f"{field}: {first.get('msg', 'invalid')}" if field else str(first.get("msg"))
        return JSONResponse(
            status_code=422,
            content={
                "errorCode": "VALIDATION_ERROR",
                "message": redact(message),
                "ui": "fatal",
                "advice": "",
            },
        )

    # -- security headers ---------------------------------------------------

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings.https_only:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    # -- pages and health ---------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- session and auth ---------------------------------------------------

    @app.get("/api/session")
    async def get_session(request: Request) -> JSONResponse:
        """Boot payload: who is signed in, the CSRF token, and where MCip is."""
        user_id = user_id_of(request)
        body: dict[str, Any] = {
            "allow_register": settings.allow_register,
            "mcip_host": urlsplit(settings.mcip_base_url).netloc or settings.mcip_base_url,
            "user": None,
            "connection": None,
        }
        if user_id is not None:
            user = await asyncio.to_thread(store.get_user_by_id, user_id)
            if user is None:
                request.session.clear()
            else:
                body["user"] = {"username": user["username"]}
                body["connection"] = connection_view(
                    await asyncio.to_thread(store.get_connection, user_id)
                )
        token = issue_csrf(request)
        body["csrf_token"] = token
        response = JSONResponse(body)
        attach_csrf_cookie(response, token)
        return response

    @app.post("/api/register", dependencies=[Depends(require_csrf)])
    async def register(request: Request, payload: RegisterRequest) -> JSONResponse:
        if not settings.allow_register:
            raise api_error(
                403,
                "DEMO_REGISTRATION_DISABLED",
                "Self-registration is off. Ask the demo admin for an account.",
            )
        enforce_limit(
            f"login:{client_ip(request)}", settings.login_rate_per_minute, "Too many attempts."
        )
        if await asyncio.to_thread(store.get_user, payload.username):
            raise api_error(409, "DEMO_USERNAME_TAKEN", "That username is taken.")
        user_id = await asyncio.to_thread(store.create_user, payload.username, payload.password)
        request.session.clear()
        request.session["user_id"] = user_id
        token = issue_csrf(request, rotate=True)
        response = JSONResponse({"user": {"username": payload.username}, "csrf_token": token})
        attach_csrf_cookie(response, token)
        return response

    @app.post("/api/login", dependencies=[Depends(require_csrf)])
    async def login(request: Request, payload: LoginRequest) -> JSONResponse:
        enforce_limit(
            f"login:{client_ip(request)}", settings.login_rate_per_minute, "Too many attempts."
        )
        user = await asyncio.to_thread(store.get_user, payload.username)
        password_ok = bool(user) and await asyncio.to_thread(
            verify_password, user["password_hash"], payload.password
        )
        if not password_ok or user is None:
            raise api_error(401, "DEMO_BAD_CREDENTIALS", "Wrong username or password.")
        request.session.clear()
        request.session["user_id"] = int(user["id"])
        token = issue_csrf(request, rotate=True)
        response = JSONResponse({"user": {"username": user["username"]}, "csrf_token": token})
        attach_csrf_cookie(response, token)
        return response

    @app.post("/api/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request) -> JSONResponse:
        request.session.clear()
        token = issue_csrf(request, rotate=True)
        response = JSONResponse({"ok": True, "csrf_token": token})
        attach_csrf_cookie(response, token)
        return response

    # -- connection (Guide §2, §4.1, §11) -----------------------------------

    @app.get("/api/connection")
    async def get_connection(user_id: UserId) -> dict[str, Any]:
        connection = await asyncio.to_thread(store.get_connection, user_id)
        return {"connection": connection_view(connection)}

    @app.post("/api/connection", dependencies=[Depends(require_csrf)])
    async def connect(request: Request, payload: ConnectRequest, user_id: UserId) -> dict[str, Any]:
        """Connect MCip: validate the key with ``GET /me``, then store it."""
        enforce_limit(f"connect:{user_id}", settings.login_rate_per_minute, "Too many attempts.")
        key = payload.key.strip()
        if not key.startswith("ss_pat_"):
            raise api_error(
                400, "DEMO_KEY_FORMAT", "That does not look like an MCip API key (ss_pat_...)."
            )
        async with mcip_client(key) as mcip:
            try:
                me = await mcip.me()
            except ExtApiError as exc:
                raise translate_ext_error(exc) from exc
        user = me.get("user") or {}
        key_info = me.get("key") or {}
        client = me.get("api_client") or {}
        await asyncio.to_thread(
            store.save_connection,
            user_id,
            key=key,
            prefix=str(key_info.get("prefix") or key_prefix(key)),
            mcip_user_id=user.get("id"),
            display_name=user.get("display_name"),
            email=user.get("email"),
            api_client_name=client.get("name"),
            expires_at=key_info.get("expires_at"),
            workspaces=list(me.get("workspaces") or []),
        )
        logger.info("connected user=%s as %s (key %s)", user_id, user.get("email"), key_prefix(key))
        connection = await asyncio.to_thread(store.get_connection, user_id)
        return {"connection": connection_view(connection)}

    @app.delete("/api/connection", dependencies=[Depends(require_csrf)])
    async def disconnect(user_id: UserId) -> dict[str, Any]:
        """Forget the key here. The key itself stays valid in MCip until the
        user deletes it at /user-settings/api-key (Guide §11)."""
        await asyncio.to_thread(store.delete_connection, user_id)
        return {"connection": None}

    @app.put("/api/connection/workspace", dependencies=[Depends(require_csrf)])
    async def choose_workspace(payload: WorkspaceRequest, user_id: UserId) -> dict[str, Any]:
        connection = await asyncio.to_thread(store.get_connection, user_id)
        if connection is None:
            raise api_error(401, "NOT_CONNECTED", "Connect your MCip key first.")
        allowed = {int(w.get("id")): str(w.get("name") or "") for w in connection["workspaces"]}
        if payload.workspace_id not in allowed:
            raise api_error(
                403, "WORKSPACE_FORBIDDEN", "That workspace is not one your key can use."
            )
        await asyncio.to_thread(
            store.set_workspace, user_id, payload.workspace_id, allowed[payload.workspace_id]
        )
        connection = await asyncio.to_thread(store.get_connection, user_id)
        return {"connection": connection_view(connection)}

    # -- conversations (Guide §4.3, §4.4) -----------------------------------

    @app.get("/api/conversations")
    async def list_conversations(user_id: UserId) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.list_conversations, user_id)
        return {"conversations": rows}

    @app.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(
        conversation_id: int,
        user_id: UserId,
        before: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(store.get_conversation, user_id, conversation_id)
        if row is None:
            raise api_error(404, "CONVERSATION_NOT_FOUND", "That conversation does not exist.")
        mcip_id = row.get("mcip_conversation_id")
        if not mcip_id:
            return {"conversation_id": conversation_id, "messages": [], "has_more": False}
        mcip = await open_mcip(user_id)
        async with mcip:
            try:
                page = await mcip.messages(int(mcip_id), before=before)
            except ExtApiError as exc:
                raise translate_ext_error(exc) from exc
        return {
            "conversation_id": conversation_id,
            "messages": page.get("messages") or [],
            "has_more": bool(page.get("has_more")),
            "next_before": page.get("next_before"),
        }

    @app.delete("/api/conversations/{conversation_id}", dependencies=[Depends(require_csrf)])
    async def delete_conversation(conversation_id: int, user_id: UserId) -> dict[str, Any]:
        row = await asyncio.to_thread(store.get_conversation, user_id, conversation_id)
        if row is None:
            raise api_error(404, "CONVERSATION_NOT_FOUND", "That conversation does not exist.")
        mcip_id = row.get("mcip_conversation_id")
        if mcip_id:
            mcip = await open_mcip(user_id)
            async with mcip:
                try:
                    await mcip.delete_conversation(int(mcip_id))
                except ExtApiError as exc:
                    if exc.error_code != "CONVERSATION_NOT_FOUND":
                        raise translate_ext_error(exc) from exc
        await asyncio.to_thread(store.delete_conversation, user_id, conversation_id)
        return {"deleted": True, "conversation_id": conversation_id}

    # -- chat (Guide §4.2, §5, §7, §8) --------------------------------------

    @app.post("/api/chat", dependencies=[Depends(require_csrf)])
    async def chat(payload: ChatRequest, user_id: UserId) -> StreamingResponse:
        enforce_limit(
            f"chat:{user_id}",
            settings.chat_rate_per_minute,
            "Slow down a little — too many messages.",
        )
        connection = await asyncio.to_thread(store.get_connection, user_id)
        if connection is None:
            raise api_error(401, "NOT_CONNECTED", "Connect your MCip key first.")
        if not connection.get("workspace_id"):
            raise api_error(409, "DEMO_NO_WORKSPACE", "Choose a workspace first.")

        created_now = False
        if payload.conversation_id is None:
            title = payload.message.strip().splitlines()[0][:80]
            conversation_id = await asyncio.to_thread(store.create_conversation, user_id, title)
            stored_mcip_id: int | None = None
            created_now = True
        else:
            row = await asyncio.to_thread(store.get_conversation, user_id, payload.conversation_id)
            if row is None:
                raise api_error(404, "CONVERSATION_NOT_FOUND", "That conversation does not exist.")
            conversation_id = int(row["id"])
            stored_mcip_id = (
                int(row["mcip_conversation_id"]) if row.get("mcip_conversation_id") else None
            )

        # The demo owns the idempotency key (issue spec): a fresh one per turn,
        # or the one the browser sends back when it retries a failed turn. It
        # is echoed on every event the relay emits, and on a refusal that never
        # produced an event (below), so the browser can always reuse it.
        client_request_id = payload.client_request_id or f"demo-{secrets.token_hex(16)}"

        # MCip scopes the idempotency key by conversation (`new:ws…` for the
        # first turn of a chat). A retry must repeat the exact conversation
        # param of the first attempt, not the conversation's stored MCip id.
        turn_request = await asyncio.to_thread(store.get_turn_request, user_id, client_request_id)
        if turn_request is None:
            mcip_conversation_id = stored_mcip_id
            await asyncio.to_thread(
                store.record_turn_request, user_id, client_request_id, stored_mcip_id
            )
        else:
            mcip_conversation_id = turn_request["mcip_conversation_id"]
        mcip = await open_mcip(user_id)

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for event in relay_turn(
                    mcip,
                    workspace_id=int(connection["workspace_id"]),
                    message=payload.message,
                    conversation_id=mcip_conversation_id,
                    stream=payload.stream,
                    client_request_id=client_request_id,
                    external_user_ref=f"demo-user-{user_id}",
                ):
                    if event.get("event") == "keepalive":
                        # MCip's heartbeat, as a comment for the browser (and
                        # the proxies between): any slow turn keeps trickling
                        # bytes, so no 100 s idle cut (deploy runbook §4).
                        yield ": keep-alive\n\n"
                        continue
                    if event.get("event") == "start":
                        new_id = event.get("conversation_id")
                        remapped = (
                            new_id
                            and mcip_conversation_id is None
                            and int(new_id) != stored_mcip_id
                        )
                        if remapped:
                            if stored_mcip_id and not await _orphan_holds_nothing(
                                mcip, stored_mcip_id
                            ):
                                # The old conversation is still in use — the
                                # user kept chatting while this Retry card sat
                                # there. Keep the row on it and leave the
                                # retried answer in the new conversation,
                                # rather than hiding (and deleting) real
                                # turns.
                                logger.info(
                                    "conversation %s still holds other turns; keeping it",
                                    stored_mcip_id,
                                )
                            else:
                                # The turn ran with conversation_id: null —
                                # MCip opened a conversation. On a retry of a
                                # first turn that stored nothing (Guide §8)
                                # that is a *new* conversation: follow it, and
                                # drop the old one.
                                await asyncio.to_thread(
                                    store.set_mcip_conversation_id,
                                    user_id,
                                    conversation_id,
                                    int(new_id),
                                )
                                if stored_mcip_id:
                                    await _delete_orphan(mcip, stored_mcip_id)
                        await asyncio.to_thread(store.touch_conversation, user_id, conversation_id)
                        event = {**event, "local_conversation_id": conversation_id}
                    yield _sse(event)
            finally:
                await mcip.aclose()

        stream = event_stream()
        try:
            first = await anext(stream)
        except BaseException as exc:
            if created_now:
                await asyncio.to_thread(store.delete_conversation, user_id, conversation_id)
            if isinstance(exc, DemoError):
                # the browser still learns the id, so its Retry replays
                exc.extra = {**exc.extra, "client_request_id": client_request_id}
            raise

        return StreamingResponse(
            _prepend(first, stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'), ensure_ascii=False)}\n\n"


async def _prepend(first: str, rest: AsyncIterator[str]) -> AsyncIterator[str]:
    yield first
    async for item in rest:
        yield item


async def _orphan_holds_nothing(mcip: McipClient, conversation_id: int) -> bool:
    """Whether the conversation a failed first attempt left behind is safe to
    delete. MCip keeps the attempt's own rows — its user message plus a
    pre-written assistant shell (empty or partial, Guide §8), so count *user*
    messages only: a second one means later turns ran in the conversation, and
    it must not be destroyed. On an error the safe answer is no."""
    try:
        page = await mcip.messages(conversation_id)
    except ExtApiError as exc:
        return exc.error_code == "CONVERSATION_NOT_FOUND"  # already gone
    except httpx.TransportError:
        return False
    messages = page.get("messages") or []
    user_messages = [message for message in messages if message.get("role") == "user"]
    return not page.get("has_more") and len(user_messages) <= 1


async def _delete_orphan(mcip: McipClient, conversation_id: int) -> None:
    """Best-effort cleanup of the conversation a failed first attempt left in
    MCip (it stored nothing — Guide §8). A failed delete must not break the
    running stream, so it is only logged."""
    try:
        await mcip.delete_conversation(conversation_id)
    except ExtApiError as exc:
        if exc.error_code != "CONVERSATION_NOT_FOUND":
            logger.warning("orphan conversation %s not deleted: %s", conversation_id, exc)
    except httpx.TransportError as exc:
        logger.warning("orphan conversation %s not deleted: %s", conversation_id, exc)


app = create_app()
