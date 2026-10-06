"""A fake MCip External Chat API for the tests (no network, no sockets).

It mirrors the real contract closely enough to exercise every path the demo
handles: the ``/me`` handshake, streamed turns, pre-stream failures, turns
that fail halfway, streams cut before ``done``, and the idempotent replay
(``client_request_id`` reused within 10 minutes → stored JSON, even for a
``stream: true`` request; see the guide's §8).

Like the real MCip, the replay key is scoped by conversation: the first turn
of a new chat is stored under ``new:ws{workspace_id}``, later turns under
their conversation id — so the same id sent with a different conversation
param is a *new* turn, not a replay (and the demo's tests catch it).

Script each response with :meth:`FakeMcip.enqueue` before making the request:

    fake.enqueue_sse([{"event": "start", ...}, {"event": "delta", "text": "Hi"},
                      {"comment": "keep-alive"},  # an SSE comment frame
                      {"event": "done", "status": "completed", "usage": {}}])

Responses are consumed in order; using more than were enqueued is a test bug
and answers 500 ``INTERNAL_ERROR``.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

API_PREFIX = "/api/v1/ext"

DEFAULT_ME = {
    "user": {"id": "u-1", "display_name": "Ada Demo", "email": "ada@example.com"},
    "api_client": {"id": 7, "name": "Acme Assist (demo)"},
    "key": {"prefix": "ss_pat_demo000000", "expires_at": "2027-01-01T00:00:00+00:00"},
    "workspaces": [
        {"id": 11, "name": "Chat Demo"},
        {"id": 12, "name": "Second Workspace"},
    ],
}

DEFAULT_USAGE = {
    "credits_micros": 1500,
    "model": "demo-model",
    "prompt_tokens": 120,
    "completion_tokens": 45,
    "total_tokens": 165,
}


def error_body(code: str, message: str = "scripted failure") -> dict[str, Any]:
    return {"errorCode": code, "message": message, "request_id": "req_test"}


class FakeMcip:
    def __init__(self) -> None:
        self.queue: deque[dict[str, Any]] = deque()
        self.chat_requests: list[dict[str, Any]] = []
        self.me_requests: list[str] = []  # Authorization headers seen
        self.client_keys_seen: list[str | None] = []  # X-MCip-Client-Key on /me
        #: When set, /me requires this X-MCip-Client-Key (MCip #1207 behaviour).
        self.required_client_key: str | None = None
        #: The key GET /ext/client accepts (MCip #1207); None = route absent (old MCip).
        self.valid_client_key: str | None = None
        self.client_key_in_grace = False
        self.client_payload: dict[str, Any] = {
            "id": 7,
            "name": "Acme Assist (demo)",
            "organization": {"id": 4, "name": "KAI"},
            "enabled": True,
            "require_client_key": True,
        }
        self.completed: dict[str, dict[str, Any]] = {}  # idempotency key → turn body
        self.executed_turns = 0  # scripted turns run (replays don't count)
        self.transcripts: dict[int, list[dict[str, Any]]] = {}
        self.deleted: list[int] = []
        self.me_error: tuple[int, dict[str, Any]] | None = None
        self.me_payload: dict[str, Any] = DEFAULT_ME
        #: Seconds to sleep before each SSE event — a slow turn for the
        #: browser tests (the streaming state stays observable).
        self.pacing = 0.0
        self.app = self._build()

    # -- scripting -----------------------------------------------------------

    def enqueue_error(
        self,
        status: int,
        code: str,
        *,
        retry_after_s: int | None = None,
        retry_after_ms: int | None = None,
        message: str = "scripted failure",
    ) -> None:
        headers = {}
        if retry_after_s is not None:
            headers["Retry-After"] = str(retry_after_s)
        body = error_body(code, message)
        if retry_after_ms is not None:
            body["retry_after_ms"] = retry_after_ms
        self.queue.append({"kind": "error", "status": status, "body": body, "headers": headers})

    def enqueue_sse(self, events: list[dict[str, Any]], *, complete: bool = True) -> None:
        """A streamed turn. ``complete=False`` cuts the stream early (no done)."""
        self.queue.append({"kind": "sse", "events": events, "complete": complete})

    def enqueue_json(self, turn: dict[str, Any]) -> None:
        self.queue.append({"kind": "json", "turn": turn})

    def reset(self) -> None:
        self.queue.clear()
        self.chat_requests.clear()
        self.me_requests.clear()
        self.completed.clear()
        self.executed_turns = 0
        self.transcripts.clear()
        self.deleted.clear()
        self.me_error = None
        self.me_payload = DEFAULT_ME
        self.client_keys_seen.clear()
        self.required_client_key = None
        self.valid_client_key = None
        self.client_key_in_grace = False
        self.pacing = 0.0

    # -- the ASGI app --------------------------------------------------------

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get(f"{API_PREFIX}/openapi.json")
        async def openapi() -> Response:
            return JSONResponse(
                {
                    "openapi": "3.1.0",
                    "info": {"title": "MCip External Chat API", "version": "1.0.0"},
                }
            )

        @app.get(f"{API_PREFIX}/client")
        async def client_check(request: Request) -> Response:
            if self.valid_client_key is None:  # an MCip release before #1207
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            key = request.headers.get("x-mcip-client-key")
            if not key:
                return JSONResponse(error_body("CLIENT_KEY_MISSING"), status_code=401)
            if key != self.valid_client_key:
                return JSONResponse(error_body("CLIENT_KEY_INVALID"), status_code=401)
            body = {
                "api_client": self.client_payload,
                "key": {
                    "prefix": key[:16],
                    "expires_at": "2026-10-07T00:00:00Z" if self.client_key_in_grace else None,
                },
            }
            return JSONResponse(body)

        @app.get(f"{API_PREFIX}/me")
        async def me(request: Request) -> Response:
            self.me_requests.append(request.headers.get("authorization", ""))
            client_key = request.headers.get("x-mcip-client-key")
            self.client_keys_seen.append(client_key)
            if self.required_client_key is not None:
                if not client_key:
                    return JSONResponse(error_body("CLIENT_KEY_MISSING"), status_code=401)
                if client_key != self.required_client_key:
                    return JSONResponse(error_body("CLIENT_KEY_INVALID"), status_code=401)
            if self.me_error is not None:
                status, body = self.me_error
                return JSONResponse(body, status_code=status)
            return JSONResponse(self.me_payload)

        @app.post(f"{API_PREFIX}/chat")
        async def chat(request: Request) -> Response:
            body = await request.json()
            self.chat_requests.append(body)
            idempotency_key = self._idempotency_key(body)

            # Idempotency: a repeat of a finished turn replays as stored JSON,
            # even though the caller asked for a stream (guide §8).
            if idempotency_key and idempotency_key in self.completed:
                return JSONResponse(self.completed[idempotency_key])

            if not self.queue:
                return JSONResponse(
                    error_body("INTERNAL_ERROR", "no scripted response"), status_code=500
                )
            item = self.queue.popleft()
            self.executed_turns += 1

            if item["kind"] == "error":
                return JSONResponse(
                    item["body"], status_code=item["status"], headers=item["headers"]
                )

            if item["kind"] == "json":
                self._remember(idempotency_key, item["turn"])
                return JSONResponse(item["turn"])

            events = item["events"]
            self._store_turn_messages(body, events)
            if item.get("complete", True):
                self._remember_sse(idempotency_key, events)

            async def stream():
                for event in events:
                    if self.pacing:
                        await asyncio.sleep(self.pacing)
                    if "event" not in event:
                        # A comment frame, e.g. {"comment": "keep-alive"} — what
                        # MCip sends after 15 s of silence (Guide §7).
                        yield f": {event.get('comment', '')}\n\n"
                        continue
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.get(f"{API_PREFIX}/conversations/{{conversation_id}}/messages")
        async def messages(conversation_id: int, limit: int = 50) -> Response:
            page = self.transcripts.get(conversation_id, [])[-limit:]
            return JSONResponse(
                {
                    "conversation_id": conversation_id,
                    "messages": page,
                    "has_more": False,
                    "next_before": None,
                }
            )

        @app.delete(f"{API_PREFIX}/conversations/{{conversation_id}}")
        async def delete(conversation_id: int) -> Response:
            self.deleted.append(conversation_id)
            self.transcripts.pop(conversation_id, None)
            return JSONResponse({"conversation_id": conversation_id, "deleted": True})

        return app

    # -- bookkeeping ---------------------------------------------------------

    @staticmethod
    def _idempotency_key(body: dict[str, Any]) -> str | None:
        """MCip's key: scoped by workspace for a new conversation, else by the
        conversation id (integration guide §8)."""
        client_request_id = body.get("client_request_id")
        if not client_request_id:
            return None
        if body.get("conversation_id") is None:
            return f"{client_request_id}:new:ws{body.get('workspace_id')}"
        return f"{client_request_id}:conv{body['conversation_id']}"

    def _store_turn_messages(self, body: dict[str, Any], events: list[dict[str, Any]]) -> None:
        """MCip's persistence (``persist_assistant_shell``): every streamed turn
        writes the user row and pre-writes an assistant row that the stream
        fills — so a cut or failed turn leaves it empty or partial, and the
        conversation holds *two* rows, not one."""
        conversation_id = body.get("conversation_id")
        if conversation_id is None:
            conversation_id = next(
                (event.get("conversation_id") for event in events if event.get("event") == "start"),
                None,
            )
        if conversation_id is None:
            return
        rows = self.transcripts.setdefault(conversation_id, [])
        answer = "".join(
            str(event.get("text") or "") for event in events if event.get("event") == "delta"
        )
        rows.append({"id": len(rows) + 1, "role": "user", "content": body.get("message") or ""})
        rows.append({"id": len(rows) + 1, "role": "assistant", "content": answer})

    def _remember(self, idempotency_key: str | None, turn: dict[str, Any]) -> None:
        if idempotency_key:
            self.completed[idempotency_key] = turn

    def _remember_sse(self, idempotency_key: str | None, events: list[dict[str, Any]]) -> None:
        """Store what a finished stream would answer on replay."""
        if not idempotency_key or not events or events[-1].get("event") != "done":
            return
        answer = "".join(
            str(event.get("text") or "") for event in events if event.get("event") == "delta"
        )
        citations = [
            {key: value for key, value in event.items() if key != "event"}
            for event in events
            if event.get("event") == "citation"
        ]
        rejected = [
            {key: value for key, value in event.items() if key != "event"}
            for event in events
            if event.get("event") == "action_rejected"
        ]
        error = next(
            (
                {key: value for key, value in event.items() if key != "event"}
                for event in events
                if event.get("event") == "error"
            ),
            None,
        )
        done = events[-1]
        self.completed[idempotency_key] = {
            "conversation_id": next(
                (event.get("conversation_id") for event in events if event.get("event") == "start"),
                None,
            ),
            "turn_id": "turn-1",
            "status": done.get("status") or "completed",
            "answer": answer,
            "citations": citations,
            "rejected_actions": rejected,
            "usage": done.get("usage") or {},
            "error": error,
        }


def scripted_turn(answer: str = "We refund within 30 days.") -> list[dict[str, Any]]:
    """The common case: a complete, successful stream."""
    return [
        {"event": "start", "conversation_id": 900, "turn_id": "turn-1"},
        {"event": "delta", "text": answer[:10]},
        {"event": "status", "text": "Searching your documents…"},
        {"event": "delta", "text": answer[10:]},
        {
            "event": "citation",
            "index": 1,
            "title": "Refund policy",
            "snippet": "Enterprise customers may request a refund within 30 days.",
            "document_id": 55,
            "url": None,
        },
        {"event": "done", "status": "completed", "usage": DEFAULT_USAGE},
    ]
