"""The workspace switcher (issue #7): refresh, switching and conversation scope.

The sidebar's dropdown refreshes through ``POST /api/connection/refresh``
(a fresh ``GET /ext/me``) and switches through the existing
``PUT /api/connection/workspace``; every conversation route is scoped to the
active workspace, so a stale tab can never send a conversation of another
workspace to MCip.
"""

from __future__ import annotations

import sqlite3

from app.store import Store
from tests.conftest import TEST_KEY, DemoSession, parse_sse_text
from tests.fake_mcip import FakeMcip, scripted_turn

CLIENT_KEY = "ss_cli_test_only_client_key_0123456789"  # pragma: allowlist secret
OTHER_CLIENT_KEY = "ss_cli_test_only_other_key_9876543210"  # pragma: allowlist secret


# -- refresh -------------------------------------------------------------------


async def test_refresh_re_reads_the_snapshot(ready: DemoSession, fake: FakeMcip, store: Store):
    alice_id = store.get_user("alice")["id"]
    before = store.get_connection(alice_id)
    fake.me_payload = {
        **fake.me_payload,
        "user": {"id": "u-1", "display_name": "Ada (renamed)", "email": "ada2@example.com"},
        "workspaces": [
            {"id": 11, "name": "Chat Demo (renamed)"},
            {"id": 12, "name": "Second Workspace"},
            {"id": 13, "name": "New Workspace"},
        ],
    }
    response = await ready.post("/api/connection/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_removed"] is False
    connection = body["connection"]
    assert [w["id"] for w in connection["workspaces"]] == [11, 12, 13]
    assert connection["display_name"] == "Ada (renamed)"
    # the active workspace survives when it is still in the list (renamed)
    assert connection["workspace"] == {"id": 11, "name": "Chat Demo (renamed)"}
    assert connection["workspaces_updated_at"] >= before["workspaces_updated_at"]
    # the key itself is untouched
    assert connection["key_prefix"] == before["key_prefix"]


async def test_refresh_clears_a_vanished_workspace(ready: DemoSession, fake: FakeMcip, store):
    fake.me_payload = {**fake.me_payload, "workspaces": [{"id": 12, "name": "Second Workspace"}]}
    response = await ready.post("/api/connection/refresh")
    body = response.json()
    assert body["workspace_removed"] is True
    assert body["connection"]["workspace"] is None
    assert [w["id"] for w in body["connection"]["workspaces"]] == [12]
    alice_id = store.get_user("alice")["id"]
    assert store.get_connection(alice_id)["workspace_id"] is None


async def test_after_a_refresh_only_listed_workspaces_can_be_chosen(ready: DemoSession, fake):
    fake.me_payload = {**fake.me_payload, "workspaces": [{"id": 12, "name": "Second Workspace"}]}
    assert (await ready.post("/api/connection/refresh")).json()["workspace_removed"] is True
    response = await ready.choose_workspace(11)  # the old one, gone from the list
    assert response.status_code == 403
    assert response.json()["errorCode"] == "WORKSPACE_FORBIDDEN"


async def test_refresh_reports_a_dead_key_as_reconnect(ready: DemoSession, fake, store):
    fake.me_error = (401, {"errorCode": "API_KEY_INVALID", "message": "unknown key"})
    response = await ready.post("/api/connection/refresh")
    assert response.status_code == 401
    body = response.json()
    assert body["errorCode"] == "API_KEY_INVALID"
    assert body["ui"] == "reconnect"
    # the browser's Reconnect button owns the disconnect; until then the row stays
    alice_id = store.get_user("alice")["id"]
    assert store.get_connection(alice_id) is not None


async def test_refresh_reports_a_wrong_client_key_as_fatal(
    session: DemoSession, store: Store, fake: FakeMcip
):
    store.create_user("root", "password123", is_admin=True)
    assert (await session.login("root")).status_code == 200
    assert (await session.put("/api/admin/settings", {"client_key": CLIENT_KEY})).status_code == 200
    assert (await session.connect()).status_code == 200
    fake.required_client_key = OTHER_CLIENT_KEY  # MCip now rejects the stored key
    response = await session.post("/api/connection/refresh")
    assert response.status_code == 401
    body = response.json()
    assert body["errorCode"] == "CLIENT_KEY_INVALID"
    assert body["ui"] == "fatal"  # only the demo admin can fix that one
    assert "demo admin" in body["advice"]


async def test_refresh_needs_csrf(ready: DemoSession):
    response = await ready.post("/api/connection/refresh", csrf=False)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "DEMO_CSRF"


async def test_refresh_needs_sign_in(session: DemoSession):
    assert (await session.post("/api/connection/refresh")).status_code == 401


async def test_refresh_needs_a_connection(session: DemoSession, store: Store):
    store.create_user("alice", "password123")
    await session.login("alice")
    response = await session.post("/api/connection/refresh")
    assert response.status_code == 401
    assert response.json()["errorCode"] == "NOT_CONNECTED"


async def test_refresh_shares_the_connect_rate_limit(ready: DemoSession):
    for _ in range(4):  # connecting consumed one slot of the five
        response = await ready.post("/api/connection/refresh")
        assert response.status_code == 200
    response = await ready.post("/api/connection/refresh")
    assert response.status_code == 429
    assert response.json()["errorCode"] == "DEMO_RATE_LIMITED"


# -- conversation scope --------------------------------------------------------


async def test_the_sidebar_lists_only_the_active_workspaces_conversations(
    ready: DemoSession, store: Store
):
    alice_id = store.get_user("alice")["id"]
    store.create_conversation(alice_id, "In 11", workspace_id=11)
    store.create_conversation(alice_id, "In 12", workspace_id=12)

    titles = (await ready.client.get("/api/conversations")).json()["conversations"]
    assert [row["title"] for row in titles] == ["In 11"]

    assert (await ready.choose_workspace(12)).status_code == 200
    titles = (await ready.client.get("/api/conversations")).json()["conversations"]
    assert [row["title"] for row in titles] == ["In 12"]

    assert (await ready.choose_workspace(11)).status_code == 200  # switching back
    titles = (await ready.client.get("/api/conversations")).json()["conversations"]
    assert [row["title"] for row in titles] == ["In 11"]


async def test_another_workspaces_conversation_is_a_404(
    ready: DemoSession, store: Store, fake: FakeMcip
):
    alice_id = store.get_user("alice")["id"]
    other = store.create_conversation(alice_id, "In 12", workspace_id=12)
    store.set_mcip_conversation_id(alice_id, other, 900)

    response = await ready.client.get(f"/api/conversations/{other}/messages")
    assert response.status_code == 404
    assert response.json()["errorCode"] == "CONVERSATION_NOT_FOUND"

    response = await ready.delete(f"/api/conversations/{other}")
    assert response.status_code == 404
    assert fake.deleted == []  # MCip was never asked to delete it

    response = await ready.post("/api/chat", {"conversation_id": other, "message": "hi"})
    assert response.status_code == 404
    assert store.get_conversation(alice_id, other) is not None  # still workspace 12's


async def test_a_new_conversation_records_the_active_workspace(
    ready: DemoSession, store: Store, fake: FakeMcip
):
    fake.enqueue_sse(scripted_turn())
    response = await ready.post("/api/chat", {"message": "Refund?"})
    assert response.status_code == 200
    events = parse_sse_text(response.text)
    assert events[-1]["event"] == "done"
    alice_id = store.get_user("alice")["id"]
    rows = store.list_conversations(alice_id, 11)
    assert len(rows) == 1
    assert store.get_conversation(alice_id, rows[0]["id"])["workspace_id"] == 11


async def test_legacy_conversations_join_the_workspace_on_first_read(
    ready: DemoSession, store: Store
):
    alice_id = store.get_user("alice")["id"]
    legacy = store.create_conversation(alice_id, "Old", workspace_id=11)
    with sqlite3.connect(store.db_path) as connection:  # as they were before #7
        connection.execute("UPDATE conversations SET workspace_id = NULL WHERE id = ?", (legacy,))
    rows = (await ready.client.get("/api/conversations")).json()["conversations"]
    assert [row["id"] for row in rows] == [legacy]
    assert store.get_conversation(alice_id, legacy)["workspace_id"] == 11
    # adopted once: a second read is not needed and changes nothing
    assert store.adopt_orphan_conversations(alice_id, 11) == 0


# -- store migration -----------------------------------------------------------


def test_old_database_gains_the_workspace_columns(tmp_path, settings):
    """A database from a release before #7: users has no is_admin, the
    connection has no workspaces_updated_at, conversations has no
    workspace_id — all three are added in place."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE mcip_connections (user_id INTEGER PRIMARY KEY, "
            "key_ciphertext BLOB NOT NULL, key_prefix TEXT NOT NULL, mcip_user_id TEXT, "
            "display_name TEXT, email TEXT, api_client_name TEXT, expires_at TEXT, "
            "workspaces_json TEXT NOT NULL DEFAULT '[]', workspace_id INTEGER, "
            "workspace_name TEXT, connected_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, mcip_conversation_id INTEGER, title TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    store = Store(db, settings.encryption_key)
    user_id = store.create_user("alice", "password123")
    store.save_connection(
        user_id,
        key=TEST_KEY,
        prefix="ss_pat_test_only",
        mcip_user_id=None,
        display_name=None,
        email=None,
        api_client_name=None,
        expires_at=None,
        workspaces=[{"id": 11, "name": "Chat Demo"}],
    )
    assert store.set_workspace(user_id, 11, "Chat Demo")
    assert store.refresh_connection_snapshot(
        user_id,
        display_name="Ada",
        email="ada@example.com",
        api_client_name="Acme Assist",
        expires_at=None,
        workspaces=[{"id": 11, "name": "Chat Demo"}],
    )
    assert store.get_connection(user_id)["workspaces_updated_at"]
    conversation_id = store.create_conversation(user_id, "New", workspace_id=11)
    assert store.list_conversations(user_id, 11)[0]["id"] == conversation_id
