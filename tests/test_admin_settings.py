"""Admin role and settings: MCip base URL + API client key in the GUI.

Two roles: a demo admin sets the MCip address and the API client key; a
common user only connects their own MCip key. Changing either admin setting
disconnects every user.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from app.store import SETTING_BASE_URL, Store
from tests.conftest import TEST_KEY, DemoSession

CLIENT_KEY = "ss_cli_test_only_client_key_0123456789"  # pragma: allowlist secret
OTHER_CLIENT_KEY = "ss_cli_test_only_other_key_9876543210"  # pragma: allowlist secret


@pytest.fixture
async def admin(session: DemoSession, store: Store) -> DemoSession:
    store.create_user("root", "password123", is_admin=True)
    assert (await session.login("root")).status_code == 200
    return session


async def _second_session(demo_app) -> tuple[httpx.AsyncClient, DemoSession]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=demo_app), base_url="http://demo.test"
    )
    return client, DemoSession(client)


# -- roles ------------------------------------------------------------------


async def test_session_reports_the_role(admin: DemoSession) -> None:
    payload = await admin.boot()
    assert payload["user"] == {"username": "root", "is_admin": True}
    assert payload["configured"] is True  # MCIP_BASE_URL default in tests
    assert payload["mcip_host"] == "mcip.test"


async def test_common_user_cannot_read_or_change_settings(
    session: DemoSession, store: Store
) -> None:
    store.create_user("alice", "password123")
    await session.login("alice")
    assert (await session.client.get("/api/admin/settings")).status_code == 403
    response = await session.put("/api/admin/settings", {"mcip_base_url": "https://x.example"})
    assert response.status_code == 403
    assert response.json()["errorCode"] == "DEMO_FORBIDDEN"
    assert store.get_setting(SETTING_BASE_URL) is None
    assert (await session.post("/api/admin/settings/test")).status_code == 403


async def test_signed_out_gets_401(session: DemoSession) -> None:
    assert (await session.client.get("/api/admin/settings")).status_code == 401


async def test_settings_need_csrf(admin: DemoSession, store: Store) -> None:
    response = await admin.put(
        "/api/admin/settings", {"mcip_base_url": "https://x.example"}, csrf=False
    )
    assert response.status_code == 403
    assert response.json()["errorCode"] == "DEMO_CSRF"
    assert store.get_setting(SETTING_BASE_URL) is None


# -- base URL ----------------------------------------------------------------


async def test_env_default_is_reported_until_saved(admin: DemoSession) -> None:
    settings = (await admin.client.get("/api/admin/settings")).json()["settings"]
    assert settings["mcip_base_url"] == "http://mcip.test"
    assert settings["mcip_base_url_source"] == "env"
    assert settings["client_key_prefix"] is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mcip.example",
        "mcip.example",
        "https://user:pw@mcip.example",
        "https://mcip.example/?x=1",
        "http://mcip.example",  # http only for localhost unless opted in
    ],
)
async def test_bad_urls_are_rejected(admin: DemoSession, store: Store, url: str) -> None:
    response = await admin.put("/api/admin/settings", {"mcip_base_url": url})
    assert response.status_code == 400
    assert response.json()["errorCode"] == "DEMO_BAD_URL"
    assert store.get_setting(SETTING_BASE_URL) is None


async def test_localhost_http_is_allowed(admin: DemoSession, store: Store) -> None:
    response = await admin.put("/api/admin/settings", {"mcip_base_url": "http://localhost:8929/"})
    assert response.status_code == 200
    assert store.get_setting(SETTING_BASE_URL) == "http://localhost:8929"
    body = response.json()
    assert body["settings"]["mcip_base_url_source"] == "settings"
    assert body["settings"]["mcip_base_url_updated_by"] == "root"


async def test_changing_the_url_disconnects_every_user(
    admin: DemoSession, store: Store, demo_app
) -> None:
    # alice connects in a second browser
    store.create_user("alice", "password123")
    client, alice = await _second_session(demo_app)
    async with client:
        await alice.login("alice")
        assert (await alice.connect()).status_code == 200
    alice_id = store.get_user("alice")["id"]
    assert store.get_connection(alice_id) is not None

    response = await admin.put("/api/admin/settings", {"mcip_base_url": "https://new.example"})
    body = response.json()
    assert body["changed"] == ["mcip_base_url"]
    assert body["disconnected"] == 1
    assert store.get_connection(alice_id) is None
    assert store.get_api_key(alice_id) is None
    assert (await admin.boot())["mcip_host"] == "new.example"


async def test_unchanged_https_url_keeps_connections(app_with, store: Store) -> None:
    app = app_with(mcip_base_url="https://mcip.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://demo.test"
    ) as client:
        s = DemoSession(client)
        store.create_user("root", "password123", is_admin=True)
        await s.login("root")
        response = await s.put("/api/admin/settings", {"mcip_base_url": "https://mcip.test/"})
        assert response.json()["changed"] == []
        assert response.json()["disconnected"] == 0


# -- client key ----------------------------------------------------------------


async def test_client_key_is_stored_encrypted_and_never_returned(
    admin: DemoSession, store: Store
) -> None:
    response = await admin.put("/api/admin/settings", {"client_key": CLIENT_KEY})
    assert response.status_code == 200
    text = response.text
    assert CLIENT_KEY not in text
    assert response.json()["settings"]["client_key_prefix"] == CLIENT_KEY[:16]
    assert response.json()["changed"] == ["client_key"]
    raw = store.get_settings()
    assert all(CLIENT_KEY not in row["value"] for row in raw.values())
    assert store.get_client_key() == CLIENT_KEY
    listing = (await admin.client.get("/api/admin/settings")).text
    assert CLIENT_KEY not in listing


async def test_client_key_format_is_checked(admin: DemoSession, store: Store) -> None:
    response = await admin.put("/api/admin/settings", {"client_key": TEST_KEY})
    assert response.status_code == 400
    assert response.json()["errorCode"] == "DEMO_CLIENT_KEY_FORMAT"
    assert store.get_client_key() is None


async def test_client_key_header_is_sent_to_mcip(admin: DemoSession, fake) -> None:
    await admin.put("/api/admin/settings", {"client_key": CLIENT_KEY})
    fake.required_client_key = CLIENT_KEY
    assert (await admin.connect()).status_code == 200
    assert fake.client_keys_seen[-1] == CLIENT_KEY


async def test_no_client_key_means_no_header(admin: DemoSession, fake) -> None:
    assert (await admin.connect()).status_code == 200
    assert fake.client_keys_seen[-1] is None


async def test_missing_client_key_is_reported(admin: DemoSession, fake) -> None:
    fake.required_client_key = CLIENT_KEY
    response = await admin.connect()
    assert response.status_code == 401
    body = response.json()
    assert body["errorCode"] == "CLIENT_KEY_MISSING"
    assert body["ui"] == "fatal"
    assert "Settings" in body["advice"]


async def test_wrong_client_key_is_reported(admin: DemoSession, fake) -> None:
    await admin.put("/api/admin/settings", {"client_key": OTHER_CLIENT_KEY})
    fake.required_client_key = CLIENT_KEY
    response = await admin.connect()
    assert response.json()["errorCode"] == "CLIENT_KEY_INVALID"


async def test_changing_the_client_key_disconnects_and_clearing_works(
    admin: DemoSession, store: Store
) -> None:
    await admin.put("/api/admin/settings", {"client_key": CLIENT_KEY})
    await admin.connect()
    root_id = store.get_user("root")["id"]
    assert store.get_connection(root_id) is not None

    response = await admin.put("/api/admin/settings", {"client_key": OTHER_CLIENT_KEY})
    assert response.json()["disconnected"] == 1
    assert store.get_connection(root_id) is None

    # same key again: no change
    response = await admin.put("/api/admin/settings", {"client_key": OTHER_CLIENT_KEY})
    assert response.json()["changed"] == []

    response = await admin.put("/api/admin/settings", {"clear_client_key": True})
    assert response.json()["changed"] == ["client_key"]
    assert store.get_client_key() is None
    assert response.json()["settings"]["client_key_prefix"] is None


async def test_set_and_clear_together_is_refused(admin: DemoSession) -> None:
    response = await admin.put(
        "/api/admin/settings", {"client_key": CLIENT_KEY, "clear_client_key": True}
    )
    assert response.status_code == 400


# -- test connection -------------------------------------------------------


async def test_connection_test_reports_reachability_and_client(admin: DemoSession, fake) -> None:
    result = (await admin.post("/api/admin/settings/test")).json()
    assert result["reachable"] is True
    assert result["api_version"] == "1.0.0"
    assert result["client_check"] == "skipped"  # admin not connected yet

    await admin.put("/api/admin/settings", {"client_key": CLIENT_KEY})
    fake.required_client_key = CLIENT_KEY
    await admin.connect()
    result = (await admin.post("/api/admin/settings/test")).json()
    assert result["client_check"] == "ok"

    fake.required_client_key = OTHER_CLIENT_KEY
    result = (await admin.post("/api/admin/settings/test")).json()
    assert result["client_check"] == "failed"
    assert result["client_error"] == "CLIENT_KEY_INVALID"


# -- first run (no base URL anywhere) ------------------------------------------


async def test_first_run_without_a_base_url(app_with, store: Store, fake) -> None:
    app = app_with(mcip_base_url="")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://demo.test"
    ) as client:
        s = DemoSession(client)
        payload = await s.boot()
        assert payload["configured"] is False
        assert payload["mcip_host"] is None

        store.create_user("alice", "password123")
        await s.login("alice")
        response = await s.connect()
        assert response.status_code == 503
        assert response.json()["errorCode"] == "DEMO_NOT_CONFIGURED"

        store.create_user("root", "password123", is_admin=True)
        await s.login("root")
        response = await s.put("/api/admin/settings", {"mcip_base_url": "http://localhost:1"})
        assert response.status_code == 200
        assert (await s.boot())["configured"] is True


def test_app_starts_without_mcip_base_url(settings, store, fake) -> None:
    import dataclasses

    app = create_app(
        dataclasses.replace(settings, mcip_base_url=""),
        store=store,
        mcip_transport=httpx.ASGITransport(app=fake.app),
    )
    assert app is not None


# -- store migration -------------------------------------------------------------


def test_old_database_gains_the_admin_column(tmp_path, settings) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 'now')"
        )
    store = Store(db, settings.encryption_key)
    user = store.get_user("old")
    assert user["is_admin"] == 0
    assert store.set_admin("old", True)
    assert store.is_admin(user["id"]) is True


async def test_static_javascript_is_served_as_javascript(session: DemoSession) -> None:
    """ES modules load only with a JavaScript MIME type (Windows registry quirk)."""
    for path in ("/static/app.js", "/static/vendor/purify.es.mjs"):
        response = await session.client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/javascript"), path


async def test_static_files_must_be_revalidated(session: DemoSession) -> None:
    """After a deploy the browser must not keep an old app.js (Cloudflare 4 h TTL)."""
    response = await session.client.get("/static/app.js")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")
    page = await session.client.get("/")
    assert page.headers["cache-control"] == "no-store"
