"""The SSE relay: MCip's ``/ext/chat`` stream → the browser (Guide §5, §7, §8).

The relay is where the demo *owns* the turn:

* it retries a request that failed before any output (429 / 409 busy / 5xx /
  network error) up to :data:`MAX_ATTEMPTS` times, always with the same
  ``client_request_id``, and tells the browser each time through a ``retry``
  event so it can show the countdown (Guide §8);
* every event the demo itself originates (``start``, ``retry``, ``error``)
  carries that ``client_request_id`` back to the browser, so a Retry re-sends
  the exact id the first attempt used — even when the browser never saw an
  earlier event (the id survives an HTTP error body too);
* it normalizes MCip's events into the demo's browser protocol, adding the
  ``ui`` state and ``advice`` for every error (``errors.py``), and passes
  MCip's ``: keep-alive`` heartbeat on as a ``keepalive`` marker so the
  browser connection stays under proxy idle limits while the agent works;
* it treats a stream that ends without ``done`` as a failed turn
  (``STREAM_TRUNCATED``, retryable) — the browser's Retry re-sends the same
  ``client_request_id``, and a turn that finished server-side comes back as
  stored JSON ("recovered, not charged again", Guide §8).

Retry notices are yielded live, so the browser can count the wait down; a
notice already commits the SSE response. From then on — and after any output
at all — a later failure becomes an ``error`` + ``done`` pair instead of an
HTTP error. Only a failure with nothing whatsoever sent (a non-retryable
first refusal) raises, letting the route answer with an HTTP error.

The demo does **not** auto-retry a turn that already produced text — that
would duplicate the answer on screen. From there on the Retry button owns it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable

import httpx

from app.errors import DemoError, advice, is_retryable, ui_state
from app.mcip import DEFAULT_RETRY_S, MAX_RETRY_S, ExtApiError, McipClient

#: First try + retries (the example client's budget, Guide §8).
MAX_ATTEMPTS = 4


def _delay(seconds: float, rand: Callable[[], float]) -> float:
    """Cap the wait, then add a little jitter so many callers don't sync up."""
    capped = max(0.0, min(seconds, MAX_RETRY_S))
    return capped + capped * 0.25 * rand()


def _retry_event(
    attempt: int, error_code: str, message: str, delay: float, client_request_id: str
) -> dict:
    return {
        "event": "retry",
        "attempt": attempt + 1,
        "max_attempts": MAX_ATTEMPTS,
        "delay_s": round(delay, 1),
        "errorCode": error_code,
        "message": message,
        "client_request_id": client_request_id,
    }


def _turn_error_event(
    error_code: str, message: str, retryable: bool, client_request_id: str
) -> dict:
    return {
        "event": "error",
        "errorCode": error_code,
        "message": message,
        "retryable": retryable,
        "ui": ui_state(error_code),
        "advice": advice(error_code),
        "client_request_id": client_request_id,
    }


def _done_event(status: str, usage: dict | None = None) -> dict:
    return {"event": "done", "status": status, "usage": usage or {}}


def translate_ext_error(exc: ExtApiError) -> DemoError:
    """A non-2xx ``/ext`` response as the demo's HTTP-shaped error."""
    return DemoError(
        error_code=exc.error_code,
        message=exc.message or "MCip refused the request.",
        http_status=exc.status_code if 400 <= exc.status_code < 600 else 502,
        retry_after_ms=exc.retry_after_ms() or None,
        continue_url=exc.body.get("continue_url"),
    )


def _translate(exc: BaseException) -> DemoError:
    """A pre-stream failure as an HTTP-shaped error for the browser."""
    if isinstance(exc, ExtApiError):
        return translate_ext_error(exc)
    return DemoError(
        error_code="NETWORK_ERROR",
        message=f"Could not reach MCip: {type(exc).__name__}.",
        http_status=502,
    )


async def relay_turn(
    mcip: McipClient,
    *,
    workspace_id: int,
    message: str,
    conversation_id: int | None,
    stream: bool,
    client_request_id: str,
    external_user_ref: str | None = None,
    sleep: Callable[[float], object] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> AsyncIterator[dict]:
    """One turn, as the demo's browser events (``start`` … ``done``).

    Retries stay available while the caller has seen no *output* yet; the
    ``retry`` notices don't count as output (they only drive the countdown).
    Once anything — a notice or output — has been sent, giving up is reported
    as ``error`` + ``done`` events. Only a failure before anything was sent
    raises :class:`DemoError`, so the route can answer with an HTTP error.
    """
    started = False  # any event reached the caller (retry notices included)
    content = False  # real turn output reached the caller (start/delta/…)
    attempt = 0

    def mark(event: dict, *, output: bool = True) -> dict:
        nonlocal started, content
        started = True
        if output:
            content = True
        return event

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            async with mcip.open_chat(
                workspace_id=workspace_id,
                message=message,
                conversation_id=conversation_id,
                stream=stream,
                client_request_id=client_request_id,
                external_user_ref=external_user_ref,
            ) as chat:
                saw_done = False
                async for event in chat.events:
                    name = event.get("event")
                    if name == "start":
                        payload = {
                            "event": "start",
                            "conversation_id": event.get("conversation_id"),
                            "turn_id": event.get("turn_id"),
                            # JSON back for a stream:true request is the
                            # stored replay of a finished turn (§8), never a
                            # plain stream:false answer.
                            "replayed": bool(chat.replayed and stream),
                            "client_request_id": client_request_id,
                        }
                        yield mark(payload)
                    elif name == "delta":
                        yield mark({"event": "delta", "text": str(event.get("text") or "")})
                    elif name == "status":
                        yield mark({"event": "status", "text": str(event.get("text") or "")})
                    elif name == "citation":
                        yield mark({"event": "citation", **_without_name(event)})
                    elif name == "action_rejected":
                        yield mark({"event": "action_rejected", **_without_name(event)})
                    elif name == "error":
                        code = str(event.get("errorCode") or "INTERNAL_ERROR")
                        yield mark(
                            _turn_error_event(
                                code,
                                str(event.get("message") or ""),
                                bool(event.get("retryable")),
                                client_request_id,
                            )
                        )
                    elif name == "done":
                        saw_done = True
                        yield mark(
                            _done_event(str(event.get("status") or "completed"), event.get("usage"))
                        )
                    elif name == "keepalive":
                        # MCip's `: keep-alive` heartbeat (Guide §7): pass it
                        # on so the browser leg never sits idle either.
                        yield mark({"event": "keepalive"}, output=False)
                    # Unknown event types are ignored (Guide §5).
                if not saw_done:
                    # The stream was cut before ``done``: the turn is failed,
                    # not lost — a Retry with the same id recovers it (§8).
                    yield mark(
                        _turn_error_event(
                            "STREAM_TRUNCATED",
                            "The stream ended before the answer finished.",
                            True,
                            client_request_id,
                        )
                    )
                    yield mark(_done_event("error"))
                return
        except httpx.TransportError as exc:
            # The connection failed or dropped before/while opening the stream.
            if not content and attempt < MAX_ATTEMPTS:
                delay = _delay(DEFAULT_RETRY_S, rand)
                yield mark(
                    _retry_event(
                        attempt, "NETWORK_ERROR", type(exc).__name__, delay, client_request_id
                    ),
                    output=False,
                )
                await sleep(delay)
                continue
            if started:
                yield mark(
                    _turn_error_event(
                        "NETWORK_ERROR",
                        f"Connection to MCip failed: {type(exc).__name__}.",
                        True,
                        client_request_id,
                    )
                )
                yield mark(_done_event("error"))
                return
            raise _translate(exc) from exc
        except ExtApiError as exc:
            if exc.retryable and not content and attempt < MAX_ATTEMPTS:
                delay = _delay(exc.retry_after_s, rand)
                yield mark(
                    _retry_event(attempt, exc.error_code, exc.message, delay, client_request_id),
                    output=False,
                )
                await sleep(delay)
                continue
            if started:
                # A notice or output already went out: report it in-stream.
                yield mark(
                    _turn_error_event(
                        exc.error_code, exc.message, is_retryable(exc.error_code), client_request_id
                    )
                )
                yield mark(_done_event("error"))
                return
            raise _translate(exc) from exc


def _without_name(event: dict) -> dict:
    return {key: value for key, value in event.items() if key != "event"}
