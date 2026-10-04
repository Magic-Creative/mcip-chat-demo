"""The relay: pass-through, retries, replay, truncation (guide §5–§8)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.errors import DemoError
from app.mcip import DEFAULT_RETRY_S, MAX_RETRY_S, McipClient, retry_delay
from app.relay import MAX_ATTEMPTS, relay_turn
from tests.conftest import TEST_KEY
from tests.fake_mcip import DEFAULT_USAGE, FakeMcip, scripted_turn

TURN = dict(workspace_id=11, message="What is the refund policy?")


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def client_for(fake: FakeMcip, *, transport: httpx.AsyncBaseTransport | None = None) -> McipClient:
    return McipClient(
        "http://mcip.test",
        TEST_KEY,
        transport=transport or httpx.ASGITransport(app=fake.app),
    )


async def collect(stream: AsyncIterator[dict]) -> list[dict]:
    return [event async for event in stream]


async def run(
    fake: FakeMcip,
    *,
    client_request_id: str = "c-1",
    stream: bool = True,
    sleep=None,
    transport=None,
    **overrides,
) -> list[dict]:
    options = {**TURN, **overrides}
    return await collect(
        relay_turn(
            client_for(fake, transport=transport),
            conversation_id=None,
            stream=stream,
            client_request_id=client_request_id,
            sleep=sleep or SleepRecorder(),
            **options,
        )
    )


def kinds(events: list[dict]) -> list[str]:
    return [event["event"] for event in events]


def answer_of(events: list[dict]) -> str:
    return "".join(event.get("text") or "" for event in events if event["event"] == "delta")


async def test_successful_stream_passthrough(fake: FakeMcip):
    fake.enqueue_sse(scripted_turn("We refund within 30 days."))
    events = await run(fake)
    assert kinds(events)[0] == "start"
    assert kinds(events)[-1] == "done"
    assert answer_of(events) == "We refund within 30 days."
    assert events[0]["replayed"] is False
    assert events[0]["conversation_id"] == 900
    citations = [event for event in events if event["event"] == "citation"]
    assert citations and citations[0]["title"] == "Refund policy"
    assert events[-1]["usage"]["total_tokens"] == DEFAULT_USAGE["total_tokens"]
    # the demo's idempotency key travels unchanged
    assert fake.chat_requests[0]["client_request_id"] == "c-1"


async def test_unknown_events_are_ignored(fake: FakeMcip):
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "t"},
            {"event": "mystery", "payload": 1},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    events = await run(fake)
    assert kinds(events) == ["start", "done"]


async def test_error_inside_the_stream(fake: FakeMcip):
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "t"},
            {"event": "delta", "text": "Partial answer"},
            {
                "event": "error",
                "errorCode": "PREMIUM_QUOTA_EXHAUSTED",
                "message": "No credits left.",
                "retryable": False,
            },
            {"event": "done", "status": "error", "usage": {}},
        ]
    )
    events = await run(fake)
    error = next(event for event in events if event["event"] == "error")
    assert error["ui"] == "fatal"
    assert error["advice"]
    assert error["retryable"] is False
    assert events[-1] == {"event": "done", "status": "error", "usage": {}}


async def test_stream_cut_before_done_becomes_truncated(fake: FakeMcip):
    fake.enqueue_sse(scripted_turn()[:-1], complete=False)  # no done
    events = await run(fake)
    error = next(event for event in events if event["event"] == "error")
    assert error["errorCode"] == "STREAM_TRUNCATED"
    assert error["retryable"] is True
    assert error["ui"] == "retryable"
    assert events[-1]["event"] == "done"
    assert events[-1]["status"] == "error"


async def test_rate_limited_is_retried_with_the_same_id(fake: FakeMcip):
    fake.enqueue_error(429, "RATE_LIMITED", retry_after_s=0)
    fake.enqueue_sse(scripted_turn("Done."))
    sleep = SleepRecorder()
    events = await run(fake, client_request_id="c-retry", sleep=sleep)
    retry = next(event for event in events if event["event"] == "retry")
    assert retry["errorCode"] == "RATE_LIMITED"
    assert retry["attempt"] == 2
    assert retry["max_attempts"] == MAX_ATTEMPTS
    ids = [request["client_request_id"] for request in fake.chat_requests]
    assert ids == ["c-retry", "c-retry"]
    assert events[-1]["event"] == "done"
    assert sleep.calls == [0.0]


async def test_busy_waits_for_retry_after_ms(fake: FakeMcip):
    fake.enqueue_error(409, "CONVERSATION_BUSY", retry_after_ms=1500)
    fake.enqueue_sse(scripted_turn("Done."))
    sleep = SleepRecorder()
    events = await run(fake, client_request_id="c-busy", sleep=sleep)
    retry = next(event for event in events if event["event"] == "retry")
    assert retry["errorCode"] == "CONVERSATION_BUSY"
    assert retry["delay_s"] == pytest.approx(1.5, abs=0.4)  # jitter is 25% max
    assert 1.5 <= sleep.calls[0] <= 1.9


async def test_server_error_is_retried_but_gives_up_in_stream(fake: FakeMcip):
    for _ in range(MAX_ATTEMPTS):
        fake.enqueue_error(500, "INTERNAL_ERROR")
    sleep = SleepRecorder()
    events = await run(fake, client_request_id="c-500", sleep=sleep)
    # the notices don't use up the retry budget: all attempts run
    notices = [event for event in events if event["event"] == "retry"]
    assert [event["attempt"] for event in notices] == [2, 3, 4]
    assert {event["errorCode"] for event in notices} == {"INTERNAL_ERROR"}
    # giving up after a notice is an in-stream error + done, not a raise
    error = next(event for event in events if event["event"] == "error")
    assert error["errorCode"] == "INTERNAL_ERROR"
    assert error["retryable"] is True
    assert error["ui"] == "retryable"
    assert events[-1] == {"event": "done", "status": "error", "usage": {}}
    assert len(fake.chat_requests) == MAX_ATTEMPTS
    assert len(sleep.calls) == MAX_ATTEMPTS - 1


async def test_invalid_key_fails_before_any_event(fake: FakeMcip):
    fake.enqueue_error(401, "API_KEY_INVALID")
    with pytest.raises(DemoError) as excinfo:
        await run(fake)
    assert excinfo.value.error_code == "API_KEY_INVALID"
    assert excinfo.value.ui == "reconnect"
    assert excinfo.value.http_status == 401
    assert len(fake.chat_requests) == 1  # a 401 is not retried


async def test_awaiting_approval_carries_continue_url(fake: FakeMcip):
    fake.enqueue_error(409, "CONVERSATION_AWAITING_APPROVAL")
    fake.queue[-1]["body"]["continue_url"] = "https://mcip.example.com/approvals/7"
    with pytest.raises(DemoError) as excinfo:
        await run(fake)
    assert excinfo.value.continue_url == "https://mcip.example.com/approvals/7"
    assert excinfo.value.ui == "approval"


async def test_retry_replays_the_finished_turn(fake: FakeMcip):
    fake.enqueue_sse(scripted_turn("We refund within 30 days."))
    first = await run(fake, client_request_id="c-replay")
    assert first[0]["replayed"] is False

    second = await run(fake, client_request_id="c-replay")
    assert second[0]["event"] == "start"
    assert second[0]["replayed"] is True
    assert answer_of(second) == "We refund within 30 days."
    assert second[-1]["status"] == "completed"
    # the replay cost no new upstream turn: only the two scripted calls ran
    assert len(fake.chat_requests) == 2


async def test_a_json_turn_is_not_a_replay(fake: FakeMcip):
    fake.enqueue_json(
        {
            "conversation_id": 900,
            "turn_id": "t",
            "status": "completed",
            "answer": "Stored answer.",
            "citations": [],
            "rejected_actions": [],
            "usage": DEFAULT_USAGE,
            "error": None,
        }
    )
    events = await run(fake, stream=False, client_request_id="c-json")
    assert events[0]["replayed"] is False
    assert answer_of(events) == "Stored answer."
    assert events[-1]["event"] == "done"


class _FailOnce(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.failed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.failed:
            self.failed = True
            raise httpx.ConnectError("scripted connection failure", request=request)
        return await self.inner.handle_async_request(request)


class _AlwaysFails(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("scripted connection failure", request=request)


async def test_network_error_is_retried(fake: FakeMcip):
    fake.enqueue_sse(scripted_turn("Back online."))
    sleep = SleepRecorder()
    events = await run(fake, transport=_FailOnce(httpx.ASGITransport(app=fake.app)), sleep=sleep)
    retry = next(event for event in events if event["event"] == "retry")
    assert retry["errorCode"] == "NETWORK_ERROR"
    assert events[-1]["event"] == "done"
    assert len(sleep.calls) == 1


async def test_unreachable_mcip_gives_up_with_a_network_error(fake: FakeMcip):
    sleep = SleepRecorder()
    events = await run(fake, transport=_AlwaysFails(), sleep=sleep)
    assert kinds(events) == ["retry"] * (MAX_ATTEMPTS - 1) + ["error", "done"]
    assert {event["errorCode"] for event in events if event["event"] == "retry"} == {
        "NETWORK_ERROR"
    }
    assert events[-2]["errorCode"] == "NETWORK_ERROR"
    assert events[-2]["retryable"] is True
    assert len(sleep.calls) == MAX_ATTEMPTS - 1


async def test_upstream_keepalives_are_forwarded(fake: FakeMcip):
    """MCip's ``: keep-alive`` comments become ``keepalive`` events, so the
    demo can pass the heartbeat on while the agent works silently (Guide §7)."""
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "turn-1"},
            {"comment": "keep-alive"},  # 15 s of upstream silence
            {"comment": "keep-alive"},
            {"event": "delta", "text": "Slow but alive."},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    events = await run(fake)
    assert kinds(events) == ["start", "keepalive", "keepalive", "delta", "done"]
    # a heartbeat is not turn output: it must not affect retry semantics
    assert answer_of(events) == "Slow but alive."


def test_retry_after_ms_wins_over_the_retry_after_header():
    # Both are present and disagree: the millisecond value is the precise one.
    response = httpx.Response(429, headers={"Retry-After": "30"})
    assert retry_delay(response, {"retry_after_ms": 1500}) == pytest.approx(1.5)


def test_retry_delay_fallback_order():
    header = httpx.Response(429, headers={"Retry-After": "30"})
    assert retry_delay(header, {}) == 30.0
    # A non-numeric header (older proxies send "1 minute") without ms waits the cap.
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "later"}), {}) == MAX_RETRY_S
    assert retry_delay(httpx.Response(429), {}) == DEFAULT_RETRY_S
    # JSON true is not a duration, and the total is capped at MAX_RETRY_S.
    assert retry_delay(httpx.Response(429), {"retry_after_ms": True}) == DEFAULT_RETRY_S
    assert retry_delay(httpx.Response(429), {"retry_after_ms": 120_000}) == MAX_RETRY_S
