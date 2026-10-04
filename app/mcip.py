"""The MCip External Chat API client (Guide §3–§8).

An async adaptation of the guide's example client: the same request shapes,
timeouts and retry-*advice*, but built on ``httpx.AsyncClient`` so the relay
can stream many turns concurrently.

The retry *loop* lives in ``relay.py`` (it has to tell the browser about each
retry), so this module only classifies failures (``ExtApiError``,
``httpx.TransportError``) and exposes :func:`retry_delay`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

API_PREFIX = "/api/v1/ext"
#: Matches the server's 600 s budget for one turn (Guide §7).
DEFAULT_TIMEOUT_S = 600.0
#: Wait when a retryable response names no usable delay (Guide §8).
DEFAULT_RETRY_S = 2.0
#: Never wait longer than this between attempts (Guide §8).
MAX_RETRY_S = 60.0

#: Timeouts for a streamed turn (Guide §7): MCip sends ``: keep-alive`` after
#: 15 s of silence, so a 120 s read gap means the stream is dead — no point in
#: holding a silent connection for the whole-turn budget. Non-streaming
#: requests (including a stored replay, which is one JSON body) keep the
#: 600 s read timeout.
_HTTP_TIMEOUT = httpx.Timeout(DEFAULT_TIMEOUT_S, connect=10.0, read=120.0, write=30.0)


class ExtApiError(Exception):
    """A non-2xx ``/ext`` response: ``{errorCode, message, request_id, ...}``."""

    def __init__(self, status_code: int, body: dict[str, Any], retry_after_s: float):
        self.status_code = status_code
        self.body = body
        self.error_code = str(body.get("errorCode") or f"HTTP_{status_code}")
        self.message = str(body.get("message") or "")
        self.request_id = body.get("request_id")
        self.retry_after_s = retry_after_s
        super().__init__(f"{status_code} {self.error_code}: {self.message}")

    @property
    def retryable(self) -> bool:
        return self.error_code in {"RATE_LIMITED", "CLIENT_RATE_LIMITED", "CONVERSATION_BUSY"} or (
            self.status_code >= 500
        )

    def retry_after_ms(self) -> int:
        return int(self.retry_after_s * 1000)


def retry_delay(response: httpx.Response, body: dict[str, Any]) -> float:
    """Seconds to wait: ``retry_after_ms`` (precise) wins over the whole-second
    ``Retry-After`` header.

    Every ``/ext`` 429 carries a numeric ``Retry-After`` (Guide §8). A
    non-numeric value (e.g. ``1 minute`` from an older proxy) without
    ``retry_after_ms`` waits ``MAX_RETRY_S``.
    """
    header = (response.headers.get("Retry-After") or "").strip()
    precise_ms = body.get("retry_after_ms")
    if not isinstance(precise_ms, int) or isinstance(precise_ms, bool):
        precise_ms = 0
    if precise_ms > 0:
        delay = precise_ms / 1000
    elif header.isdigit():
        delay = float(header)
    elif header:
        delay = MAX_RETRY_S
    else:
        delay = DEFAULT_RETRY_S
    return max(0.0, min(delay, MAX_RETRY_S))


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"message": response.text[:200]}
    return body if isinstance(body, dict) else {"message": str(body)[:200]}


def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Public events from ``text/event-stream`` lines.

    Each event is one ``data:`` line holding one JSON object (Guide §5);
    ``: keep-alive`` comments and blank separators are skipped, as are
    unknown or malformed frames (Guide §5: ignore what you don't know).
    """
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and "event" in event:
            yield event


def events_from_body(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """A ``stream: false`` body — or an idempotent replay (Guide §8) — as the
    same event sequence a stream would have produced."""
    yield {
        "event": "start",
        "conversation_id": body.get("conversation_id"),
        "turn_id": body.get("turn_id"),
    }
    if body.get("answer"):
        yield {"event": "delta", "text": body["answer"]}
    for rejected in body.get("rejected_actions") or []:
        yield {"event": "action_rejected", **rejected}
    for citation in body.get("citations") or []:
        yield {"event": "citation", **citation}
    if body.get("error"):
        yield {"event": "error", **body["error"]}
    yield {
        "event": "done",
        "status": body.get("status") or "completed",
        "usage": body.get("usage") or {},
    }


@dataclass
class ChatStream:
    """An open 200 response from ``POST /ext/chat``."""

    content_type: str
    events: AsyncIterator[dict[str, Any]]

    @property
    def replayed(self) -> bool:
        """A retry with a used ``client_request_id`` returns stored JSON even
        when it asked for a stream (Guide §8) — branch on the content type."""
        return "text/event-stream" not in self.content_type


class McipClient:
    """One instance per request; carries exactly one user's key."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._timeout = httpx.Timeout(timeout_s, connect=10.0)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=self._timeout,
            transport=transport,
        )

    async def __aenter__(self) -> McipClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _raise_for_error(response: httpx.Response, body: dict[str, Any]) -> None:
        if response.is_success:
            return
        raise ExtApiError(response.status_code, body, retry_delay(response, body))

    async def me(self) -> dict[str, Any]:
        """``GET /ext/me``: validate the key and list its workspaces (§4.1)."""
        response = await self._http.get("/me")
        body = _error_body(response)
        self._raise_for_error(response, body)
        return body

    @asynccontextmanager
    async def open_chat(
        self,
        *,
        workspace_id: int,
        message: str,
        conversation_id: int | None,
        stream: bool,
        client_request_id: str,
        external_user_ref: str | None = None,
    ) -> AsyncIterator[ChatStream]:
        """``POST /ext/chat`` as an open stream (§4.2, §5).

        Raises :class:`ExtApiError` for a non-2xx response (the body is read
        first) and lets ``httpx.TransportError`` through; both are the relay's
        cue to retry. Closing the context closes the HTTP response, which is
        how the Stop button cancels the turn (Guide §7).
        """
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "message": message,
            "conversation_id": conversation_id,
            "stream": stream,
            "client_request_id": client_request_id,
        }
        if external_user_ref:
            payload["external_user_ref"] = external_user_ref

        request = self._http.build_request(
            "POST", "/chat", json=payload, timeout=_HTTP_TIMEOUT if stream else self._timeout
        )
        response = await self._http.send(request, stream=True)
        try:
            if response.is_error or response.is_redirect:
                await response.aread()
                self._raise_for_error(response, _error_body(response))
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/event-stream"):
                events = _sse_events(response)
            else:
                # stream: false, or a stored replay of a finished turn (§8).
                await response.aread()
                events = _body_events(response)
            yield ChatStream(content_type=content_type, events=events)
        finally:
            await response.aclose()

    async def messages(
        self, conversation_id: int, *, limit: int = 50, before: int | None = None
    ) -> dict[str, Any]:
        """One transcript page; pass ``next_before`` as ``before`` for older
        messages (§4.3)."""
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = before
        response = await self._http.get(f"/conversations/{conversation_id}/messages", params=params)
        body = _error_body(response)
        self._raise_for_error(response, body)
        return body

    async def delete_conversation(self, conversation_id: int) -> dict[str, Any]:
        """``DELETE /ext/conversations/{id}`` (§4.4); 404 is the caller's to
        interpret (the transcript may already be gone)."""
        response = await self._http.delete(f"/conversations/{conversation_id}")
        body = _error_body(response)
        self._raise_for_error(response, body)
        return body

    async def list_workspaces(self) -> list[dict[str, Any]]:
        return list((await self.me()).get("workspaces") or [])


async def _sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse ``aiter_lines`` lazily so a Stop aborts mid-stream.

    ``: keep-alive`` comment frames become ``keepalive`` markers so the relay
    can pass the heartbeat on to the browser (Guide §7)."""
    async for line in response.aiter_lines():
        if line.startswith(":"):
            yield {"event": "keepalive"}
            continue
        for event in parse_sse([line]):
            yield event


async def _body_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    for event in events_from_body(response.json()):
        yield event
