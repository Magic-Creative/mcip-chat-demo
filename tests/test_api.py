"""The demo's own HTTP API: auth, CSRF, connecting, chat, conversations."""

from __future__ import annotations

import json
import re

import httpx
from fastapi import FastAPI

from app.main import STATIC_DIR
from tests.conftest import TEST_KEY, DemoSession, parse_sse_text
from tests.fake_mcip import FakeMcip, error_body, scripted_turn


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo.test")


async def chat_events(session: DemoSession, payload: dict) -> tuple[int, list[dict]]:
    response = await session.post("/api/chat", payload)
    if response.status_code != 200:
        return response.status_code, []
    return 200, parse_sse_text(response.text)


# -- auth and CSRF -----------------------------------------------------------


async def test_register_is_disabled_by_default(session: DemoSession):
    await session.boot()
    response = await session.register("bob")
    assert response.status_code == 403
    body = response.json()
    assert body["errorCode"] == "DEMO_REGISTRATION_DISABLED"
    assert body["ui"] == "auth"
    assert body["advice"]


async def test_register_when_enabled_and_duplicate(app_with):
    async with client_for(app_with(allow_register=True)) as client:
        session = DemoSession(client)
        await session.boot()
        first = await session.register("bob")
        assert first.status_code == 200
        assert first.json()["user"]["username"] == "bob"
        duplicate = await session.register("bob")
        assert duplicate.status_code == 409
        assert duplicate.json()["errorCode"] == "DEMO_USERNAME_TAKEN"


async def test_login_bad_credentials(session: DemoSession, store):
    store.create_user("alice", "password123")
    await session.boot()
    response = await session.login("alice", "wrong-password")
    assert response.status_code == 401
    assert response.json()["errorCode"] == "DEMO_BAD_CREDENTIALS"


async def test_state_changing_calls_require_csrf(session: DemoSession, store):
    store.create_user("alice", "password123")
    await session.boot()
    response = await session.post(
        "/api/login", {"username": "alice", "password": "password123"}, csrf=False
    )
    assert response.status_code == 403
    assert response.json()["errorCode"] == "DEMO_CSRF"


async def test_anonymous_endpoints_ask_for_sign_in(session: DemoSession):
    payload = await session.boot()
    assert payload["user"] is None
    response = await session.client.get("/api/connection")
    assert response.status_code == 401
    assert response.json()["errorCode"] == "DEMO_UNAUTHENTICATED"


async def test_logout_clears_the_session(ready: DemoSession):
    response = await ready.post("/api/logout")
    assert response.status_code == 200
    # the old CSRF token died with the session
    assert (await ready.client.get("/api/connection")).status_code == 401


# -- connecting --------------------------------------------------------------


async def test_connect_rejects_a_non_key(session: DemoSession, store):
    store.create_user("alice", "password123")
    await session.login("alice")
    response = await session.connect("not-a-key")
    assert response.status_code == 400
    assert response.json()["errorCode"] == "DEMO_KEY_FORMAT"


async def test_connect_stores_the_key_and_never_returns_it(session: DemoSession, store, fake):
    store.create_user("alice", "password123")
    await session.login("alice")
    response = await session.connect()
    assert response.status_code == 200
    assert TEST_KEY not in response.text

    connection = response.json()["connection"]
    assert connection["key_prefix"] == "ss_pat_demo000000"
    assert connection["email"] == "ada@example.com"
    assert [workspace["name"] for workspace in connection["workspaces"]] == [
        "Chat Demo",
        "Second Workspace",
    ]
    assert connection["workspace"] is None

    boot = await session.boot()
    assert TEST_KEY not in json.dumps(boot)
    assert boot["connection"]["key_prefix"] == "ss_pat_demo000000"

    # server-side, decrypted only for the outgoing call
    user_id = store.get_user("alice")["id"]
    assert store.get_api_key(user_id) == TEST_KEY
    assert fake.me_requests[-1] == f"Bearer {TEST_KEY}"


async def test_connect_surfaces_mcip_refusals(session: DemoSession, store, fake):
    store.create_user("alice", "password123")
    await session.login("alice")
    fake.me_error = (403, error_body("API_CLIENT_DISABLED"))
    response = await session.connect()
    assert response.status_code == 403
    body = response.json()
    assert body["errorCode"] == "API_CLIENT_DISABLED"
    assert body["advice"]
    assert store.get_connection(store.get_user("alice")["id"]) is None


async def test_disconnect_forgets_the_key(ready: DemoSession, store):
    response = await ready.delete("/api/connection")
    assert response.status_code == 200
    assert response.json()["connection"] is None
    assert store.get_connection(store.get_user("alice")["id"]) is None


async def test_workspace_must_come_from_the_keys_list(ready: DemoSession):
    response = await ready.choose_workspace(999)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "WORKSPACE_FORBIDDEN"


# -- chat --------------------------------------------------------------------


async def test_chat_needs_a_connection(session: DemoSession, store):
    store.create_user("alice", "password123")
    await session.login("alice")
    status, _ = await chat_events(session, {"message": "hi"})
    assert status == 401


async def test_chat_needs_a_workspace(session: DemoSession, store):
    store.create_user("alice", "password123")
    await session.login("alice")
    await session.connect()
    response = await session.post("/api/chat", {"message": "hi"})
    assert response.status_code == 409
    assert response.json()["errorCode"] == "DEMO_NO_WORKSPACE"


async def test_chat_streams_and_records_the_conversation(ready: DemoSession, fake):
    fake.enqueue_sse(scripted_turn("We refund within 30 days."))
    response = await ready.post("/api/chat", {"message": "Refund policy?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse_text(response.text)
    start = events[0]
    assert start["event"] == "start"
    assert start["conversation_id"] == 900  # MCip's id
    assert start["local_conversation_id"]  # the demo's id
    assert start["replayed"] is False
    assert events[-1]["event"] == "done"
    assert events[-1]["status"] == "completed"

    # the outgoing body follows the contract
    sent = fake.chat_requests[-1]
    assert sent["workspace_id"] == 11
    assert sent["message"] == "Refund policy?"
    assert sent["stream"] is True
    assert sent["client_request_id"].startswith("demo-")
    assert sent["external_user_ref"].startswith("demo-user-")
    assert sent["conversation_id"] is None

    # and the conversation is listed locally
    listing = (await ready.client.get("/api/conversations")).json()["conversations"]
    assert len(listing) == 1
    assert listing[0]["mcip_conversation_id"] == 900
    assert listing[0]["title"] == "Refund policy?"


async def test_continuing_the_conversation_sends_mcip_its_id(ready: DemoSession, fake):
    fake.enqueue_sse(scripted_turn("First answer."))
    _, events = await chat_events(ready, {"message": "First question"})
    local_id = events[0]["local_conversation_id"]

    fake.enqueue_sse(scripted_turn("Second answer."))
    status, _ = await chat_events(ready, {"conversation_id": local_id, "message": "Again"})
    assert status == 200
    assert fake.chat_requests[-1]["conversation_id"] == 900


async def test_transcript_comes_from_mcip(ready: DemoSession, fake):
    fake.enqueue_sse(scripted_turn("First answer."))
    _, events = await chat_events(ready, {"message": "First question"})
    local_id = events[0]["local_conversation_id"]

    fake.transcripts[900] = [
        {
            "id": 1,
            "role": "user",
            "text": "First question",
            "citations": [],
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": 2,
            "role": "assistant",
            "text": "First answer.",
            "citations": [
                {"index": 1, "title": "Policy", "snippet": "…", "document_id": 5, "url": None}
            ],
            "created_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    response = await ready.client.get(f"/api/conversations/{local_id}/messages")
    assert response.status_code == 200
    page = response.json()
    assert [message["role"] for message in page["messages"]] == ["user", "assistant"]
    assert page["messages"][1]["citations"][0]["title"] == "Policy"


async def test_deleting_a_conversation_reaches_mcip(ready: DemoSession, fake):
    fake.enqueue_sse(scripted_turn("Answer."))
    _, events = await chat_events(ready, {"message": "Question"})
    local_id = events[0]["local_conversation_id"]

    response = await ready.delete(f"/api/conversations/{local_id}")
    assert response.status_code == 200
    assert fake.deleted == [900]
    assert (await ready.client.get("/api/conversations")).json()["conversations"] == []
    assert (await ready.client.get(f"/api/conversations/{local_id}/messages")).status_code == 404


async def test_prestream_mcip_failure_is_an_http_error(ready: DemoSession, fake):
    fake.enqueue_error(401, "API_KEY_INVALID")
    response = await ready.post("/api/chat", {"message": "hi"})
    assert response.status_code == 401
    body = response.json()
    assert body["errorCode"] == "API_KEY_INVALID"
    assert body["ui"] == "reconnect"
    # the browser still learns the turn's id, so Retry can reuse it
    assert body["client_request_id"].startswith("demo-")


async def test_a_browser_supplied_client_request_id_is_used_and_echoed(
    ready: DemoSession, fake: FakeMcip
):
    fake.enqueue_sse(scripted_turn("Ok."))
    response = await ready.post(
        "/api/chat", {"message": "hi", "client_request_id": "web-0000abcd"}
    )
    assert response.status_code == 200
    assert parse_sse_text(response.text)[0]["client_request_id"] == "web-0000abcd"
    assert fake.chat_requests[-1]["client_request_id"] == "web-0000abcd"


async def test_retry_after_a_cut_stream_replays_with_the_echoed_id(
    ready: DemoSession, fake: FakeMcip
):
    """The browser lost the stream before ``done`` and hits Retry: the same
    ``client_request_id`` *and* the same conversation param go out (``None`` —
    the turn opened a new chat), so MCip replays the finished turn instead of
    running and charging it again."""
    fake.enqueue_sse(scripted_turn("We refund within 30 days."))
    start: dict | None = None
    async with ready.client.stream(
        "POST",
        "/api/chat",
        json={"message": "Refund policy?"},
        headers={"X-CSRF-Token": ready.csrf},
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                event = json.loads(line[5:])
                if event["event"] == "start":
                    start = event
                    break  # the cut: nothing after `start` reaches the browser
    assert start is not None
    assert start["replayed"] is False
    assert start["client_request_id"].startswith("demo-")

    retry = await ready.post(
        "/api/chat",
        {
            "conversation_id": start["local_conversation_id"],
            "message": "Refund policy?",
            "client_request_id": start["client_request_id"],
        },
    )
    assert retry.status_code == 200
    events = parse_sse_text(retry.text)
    assert events[0]["event"] == "start"
    assert events[0]["replayed"] is True
    assert events[0]["conversation_id"] == 900
    answer = "".join(event.get("text") or "" for event in events if event["event"] == "delta")
    assert answer == "We refund within 30 days."
    assert events[-1]["event"] == "done"
    assert events[-1]["status"] == "completed"

    # one upstream turn: the retry was a replay, with the first attempt's
    # conversation param and the same id
    assert fake.executed_turns == 1
    assert len(fake.chat_requests) == 2
    assert fake.chat_requests[1]["conversation_id"] is None
    assert fake.chat_requests[1]["client_request_id"] == start["client_request_id"]


async def test_keepalives_reach_the_browser_as_comments(ready: DemoSession, fake: FakeMcip):
    """The demo forwards MCip's heartbeat as an SSE comment, so the browser
    connection never sits idle through a slow turn (Cloudflare cuts at 100 s)."""
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "turn-1"},
            {"comment": "keep-alive"},
            {"event": "delta", "text": "Ok."},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    response = await ready.post("/api/chat", {"message": "hi"})
    assert response.status_code == 200
    assert response.text.count(": keep-alive") == 1
    # the comment frame doesn't disturb the event stream itself
    assert [event["event"] for event in parse_sse_text(response.text)] == [
        "start",
        "delta",
        "done",
    ]


async def test_a_retried_first_turn_follows_the_new_mcip_conversation(
    ready: DemoSession, fake: FakeMcip
):
    """A first turn that stored no finished answer is re-run by a retry: MCip
    opens a new conversation, so the sidebar row (and its transcript) must
    follow it — the failed attempt's conversation is dropped from MCip."""
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "turn-1"},
            {"event": "delta", "text": "Partial answ"},
        ],
        complete=False,  # stopped or cut: no finished turn to replay (Guide §8)
    )
    first = await ready.post("/api/chat", {"message": "Refund policy?"})
    assert first.status_code == 200
    started = parse_sse_text(first.text)[0]
    assert started["conversation_id"] == 900
    assert fake.chat_requests[-1]["conversation_id"] is None
    # MCip kept the attempt's rows: the user message plus the pre-written
    # assistant shell the stream left partial. The guard must count users only,
    # or this two-row conversation looks "in use" and the retry re-opens bug B.
    assert [message["role"] for message in fake.transcripts[900]] == ["user", "assistant"]
    assert fake.transcripts[900][1]["content"] == "Partial answ"

    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 901, "turn_id": "turn-1"},
            {"event": "delta", "text": "We refund within 30 days."},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    retried = await ready.post(
        "/api/chat",
        {
            "conversation_id": started["local_conversation_id"],
            "message": "Refund policy?",
            "client_request_id": started["client_request_id"],
        },
    )
    assert retried.status_code == 200
    events = parse_sse_text(retried.text)
    assert fake.chat_requests[-1]["conversation_id"] is None
    assert events[0]["conversation_id"] == 901
    assert events[0]["replayed"] is False

    # the orphaned first conversation is gone from MCip, and the local row now
    # reads the retry's answer from the new one
    assert fake.deleted == [900]
    page = await ready.client.get(
        f"/api/conversations/{started['local_conversation_id']}/messages"
    )
    assert page.status_code == 200
    assert page.json()["messages"] == [
        {"id": 1, "role": "user", "content": "Refund policy?"},
        {"id": 2, "role": "assistant", "content": "We refund within 30 days."},
    ]


async def test_a_stale_retry_keeps_a_conversation_that_is_still_in_use(
    ready: DemoSession, fake: FakeMcip
):
    """A Retry card outlives later turns: if the old conversation kept living
    (the user sent more messages from the same chat), the stale retry must not
    delete it or hide it — the row stays there, and the re-run's answer stays
    in its own new conversation."""
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "turn-1"},
            {"event": "delta", "text": "Partial answ"},
        ],
        complete=False,  # stopped or cut: no finished turn to replay (Guide §8)
    )
    first = await ready.post("/api/chat", {"message": "Refund policy?"})
    started = parse_sse_text(first.text)[0]
    local_id = started["local_conversation_id"]

    # the user ignores the Retry card and keeps chatting: a later turn lands
    # in the same MCip conversation and completes
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 900, "turn_id": "turn-2"},
            {"event": "delta", "text": "Later answer."},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    later = await ready.post(
        "/api/chat", {"conversation_id": local_id, "message": "Another question"}
    )
    assert later.status_code == 200
    assert fake.chat_requests[-1]["conversation_id"] == 900
    # what MCip holds for that conversation: the failed attempt's user row and
    # partial assistant shell, then the completed later turn
    assert [message["role"] for message in fake.transcripts[900]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    # now the stale Retry card fires: the re-run opens conversation 901
    fake.enqueue_sse(
        [
            {"event": "start", "conversation_id": 901, "turn_id": "turn-1"},
            {"event": "delta", "text": "We refund within 30 days."},
            {"event": "done", "status": "completed", "usage": {}},
        ]
    )
    retried = await ready.post(
        "/api/chat",
        {
            "conversation_id": local_id,
            "message": "Refund policy?",
            "client_request_id": started["client_request_id"],
        },
    )
    assert retried.status_code == 200
    assert parse_sse_text(retried.text)[0]["conversation_id"] == 901
    # conversation 900 was *not* deleted, and the row still reads from it
    assert fake.deleted == []
    page = await ready.client.get(f"/api/conversations/{local_id}/messages")
    assert page.status_code == 200
    assert [message["id"] for message in page.json()["messages"]] == [1, 2, 3, 4]


async def test_busy_then_success_inside_one_response(ready: DemoSession, fake):
    fake.enqueue_error(409, "CONVERSATION_BUSY", retry_after_ms=1)
    fake.enqueue_sse(scripted_turn("Finally."))
    response = await ready.post("/api/chat", {"message": "hi"})
    assert response.status_code == 200
    events = parse_sse_text(response.text)
    assert "retry" in [event["event"] for event in events]
    assert events[-1]["event"] == "done"


async def test_chat_rate_limit(app_with, store, fake):
    app = app_with(chat_rate_per_minute=1)
    async with client_for(app) as client:
        session = DemoSession(client)
        store.create_user("alice", "password123")
        await session.login("alice")
        await session.connect()
        await session.choose_workspace()
        fake.enqueue_sse(scripted_turn("One."))
        first = await session.post("/api/chat", {"message": "first"})
        assert first.status_code == 200
        second = await session.post("/api/chat", {"message": "second"})
        assert second.status_code == 429
        body = second.json()
        assert body["errorCode"] == "DEMO_RATE_LIMITED"
        assert body["ui"] == "busy"
        assert int(second.headers["Retry-After"]) >= 1


async def test_blank_message_is_rejected(ready: DemoSession):
    response = await ready.post("/api/chat", {"message": "   "})
    assert response.status_code == 422
    assert response.json()["errorCode"] == "VALIDATION_ERROR"


# -- pages, headers, logs ----------------------------------------------------


async def test_security_headers_and_no_inline_scripts(session: DemoSession):
    response = await session.client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag, f"inline script would be blocked by the CSP: {tag}"


async def test_healthz(session: DemoSession):
    assert (await session.client.get("/healthz")).json() == {"status": "ok"}


async def test_theme_toggle_and_views_exist_in_the_page(session: DemoSession):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element_id in (
        "view-auth",
        "view-connect",
        "view-workspace",
        "view-chat",
        "composer-input",
        "status-line",
        "theme-toggle",
    ):
        assert f'id="{element_id}"' in html


async def test_the_key_never_reaches_the_logs(ready: DemoSession, fake, caplog):
    with caplog.at_level("DEBUG"):
        fake.enqueue_sse(scripted_turn("Answer."))
        await ready.post("/api/chat", {"message": "hello"})
    assert TEST_KEY not in caplog.text
