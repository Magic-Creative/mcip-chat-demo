"""Connect MCip via OAuth 2.1 (#1389, Guide §16).

The demo's OAuth connect: start -> MCip consent (the fake's authorize URL is
only asserted, the browser leg is the fake's code seeding) -> callback ->
exchange -> /ext/me -> stored tokens; plus the refresh paths (proactive,
retry-once on 401 OAUTH_TOKEN_EXPIRED, invalid_grant = reconnect), the
disconnect revoke, the PAT setting gate and the redaction of OAuth prefixes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from app.store import AUTH_MODE_OAUTH, AUTH_MODE_PAT, redact
from tests.conftest import DemoSession
from tests.fake_mcip import FakeMcip, error_body


async def _enable_oauth(session: DemoSession, store, fake: FakeMcip) -> None:
    """Flip the deployment to OAuth mode the way the admin UI does."""
    store.create_user("root", "password123", is_admin=True)
    assert (await session.login("root")).status_code == 200
    response = await session.put(
        "/api/admin/settings",
        {
            "auth_mode": AUTH_MODE_OAUTH,
            "oauth_client_id": fake.oauth_client_id,
            "client_key": fake.oauth_client_secret,
        },
    )
    assert response.status_code == 200, response.text
    assert "auth_mode" in response.json()["changed"]


def _state_and_challenge(start_response) -> tuple[str, str]:
    target = parse_qs(urlsplit(start_response.headers["location"]).query)
    return target["state"][0], target["code_challenge"][0]


async def _connect_oauth(session: DemoSession, store, fake: FakeMcip) -> tuple[str, int]:
    """The full happy path; returns (redirect_uri, user_id)."""
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    alice_id = int(store.get_user("alice")["id"])
    assert (await session.login("alice")).status_code == 200
    start = await session.client.get("/api/oauth/start")
    assert start.status_code == 303
    url = urlsplit(start.headers["location"])
    assert url.path == "/api/v1/ext/oauth/authorize"
    query = parse_qs(url.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [fake.oauth_client_id]
    assert query["scope"] == ["chat"]
    redirect_uri = query["redirect_uri"][0]
    code = fake.seed_authorization_code(
        redirect_uri=redirect_uri, challenge=query["code_challenge"][0]
    )
    callback = await session.client.get(
        "/api/oauth/callback",
        params={"code": code, "state": query["state"][0], "iss": "http://mcip.test"},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?connected=oauth"
    return redirect_uri, alice_id


async def test_start_requires_oauth_mode(session: DemoSession, store, fake: FakeMcip):
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    response = await session.client.get("/api/oauth/start")

    assert response.status_code == 409
    assert response.json()["errorCode"] == "OAUTH_DISABLED"


async def test_start_requires_a_client_id(session: DemoSession, store, fake: FakeMcip):
    store.create_user("root", "password123", is_admin=True)
    assert (await session.login("root")).status_code == 200
    await session.put("/api/admin/settings", {"auth_mode": AUTH_MODE_OAUTH})
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    response = await session.client.get("/api/oauth/start")

    assert response.status_code == 503
    assert response.json()["errorCode"] == "OAUTH_NOT_CONFIGURED"


async def test_start_returns_a_pkce_s256_redirect_and_keeps_the_verifier_server_side(
    session: DemoSession, store, fake: FakeMcip
):
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    response = await session.client.get("/api/oauth/start")

    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43, "base64url(sha256) without padding"
    assert query["state"][0]
    # The verifier itself never leaves the server: nothing in the response
    # (headers, cookies) carries it — only its S256 challenge does.
    body_and_headers = location + " " + " ".join(f"{k}:{v}" for k, v in response.headers.items())
    assert "code_verifier" not in body_and_headers


async def test_callback_exchanges_the_code_with_pkce_and_stores_the_connection(
    session: DemoSession, store, fake: FakeMcip
):
    redirect_uri, alice_id = await _connect_oauth(session, store, fake)

    # The exchange used Basic auth, the exact redirect URI and the verifier
    # the fake validated against the challenge it saw at /start (a mismatch
    # would have been invalid_grant).
    assert len(fake.token_requests) == 1
    exchange = fake.token_requests[0]
    assert exchange["form"]["grant_type"] == "authorization_code"
    assert exchange["form"]["redirect_uri"] == redirect_uri
    assert exchange["form"]["code_verifier"]
    assert exchange["authorization"] == fake._basic_expected()

    # A single /ext/me call with the fresh access token, which is what got
    # stored (Fernet-encrypted columns, never the plaintext in the row).
    assert len(fake.oauth_me_bearers) == 1
    assert fake.oauth_me_bearers[0].startswith("ss_oat_")
    tokens = store.get_oauth_tokens(alice_id)
    assert tokens is not None and tokens["refresh_token"].startswith("ss_ort_")
    with store._connect() as connection:
        row = connection.execute(
            "SELECT auth_mode, key_ciphertext, access_ciphertext FROM mcip_connections "
            "WHERE user_id = ?",
            (alice_id,),
        ).fetchone()
    assert row["auth_mode"] == AUTH_MODE_OAUTH
    assert row["key_ciphertext"] is None, "PAT column cleared in OAuth mode"
    assert b"ss_oat_" not in bytes(row["access_ciphertext"])

    boot = await session.boot()
    assert boot["connection"]["auth_mode"] == AUTH_MODE_OAUTH
    assert boot["connection"]["connected"] is True
    assert boot["connection"]["workspaces"], "the /me snapshot was stored"


async def test_callback_state_is_single_use(session: DemoSession, store, fake: FakeMcip):
    redirect_uri, _ = await _connect_oauth(session, store, fake)
    start = await session.client.get("/api/oauth/start")
    state, challenge = _state_and_challenge(start)
    code = fake.seed_authorization_code(redirect_uri=redirect_uri, challenge=challenge)

    first = await session.client.get(
        "/api/oauth/callback",
        params={"code": code, "state": state, "iss": "http://mcip.test"},
    )
    assert first.status_code == 303
    assert first.headers["location"] == "/?connected=oauth", "the first redemption succeeds"
    second = await session.client.get(
        "/api/oauth/callback",
        params={"code": code, "state": state, "iss": "http://mcip.test"},
    )

    assert "oauth_error_code=OAUTH_STATE_UNKNOWN" in second.headers["location"]


async def test_callback_rejects_a_foreign_issuer(session: DemoSession, store, fake: FakeMcip):
    redirect_uri, _ = await _connect_oauth(session, store, fake)
    start = await session.client.get("/api/oauth/start")
    state, challenge = _state_and_challenge(start)
    code = fake.seed_authorization_code(redirect_uri=redirect_uri, challenge=challenge)
    exchanges_before = len(fake.token_requests)

    response = await session.client.get(
        "/api/oauth/callback",
        params={"code": code, "state": state, "iss": "http://evil.example"},
    )

    assert "oauth_error_code=OAUTH_ISS_MISMATCH" in response.headers["location"]
    assert len(fake.token_requests) == exchanges_before, "no exchange happens after a mix-up signal"


async def test_callback_requires_the_iss_parameter(session: DemoSession, store, fake: FakeMcip):
    redirect_uri, _ = await _connect_oauth(session, store, fake)
    start = await session.client.get("/api/oauth/start")
    state, challenge = _state_and_challenge(start)
    code = fake.seed_authorization_code(redirect_uri=redirect_uri, challenge=challenge)
    exchanges_before = len(fake.token_requests)

    response = await session.client.get(
        "/api/oauth/callback", params={"code": code, "state": state}
    )

    assert "oauth_error_code=OAUTH_ISS_MISSING" in response.headers["location"]
    assert len(fake.token_requests) == exchanges_before


async def test_callback_matches_the_recorded_metadata_issuer_exactly(
    session: DemoSession, store, fake: FakeMcip
):
    """RFC 9207 §2.4: exact string match, not an origin compare.

    The issuer recorded at /start is the AS metadata's, so an issuer *path*
    that differs from the base URL is legitimate when the metadata says so —
    and the callback must then also reject the bare origin.
    """
    fake.oauth_issuer = "http://mcip.test/tenant"
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    start = await session.client.get("/api/oauth/start")
    query = parse_qs(urlsplit(start.headers["location"]).query)
    redirect_uri = query["redirect_uri"][0]
    code = fake.seed_authorization_code(
        redirect_uri=redirect_uri, challenge=query["code_challenge"][0]
    )

    wrong = await session.client.get(
        "/api/oauth/callback",
        params={"code": code, "state": query["state"][0], "iss": "http://mcip.test"},
    )
    assert "oauth_error_code=OAUTH_ISS_MISMATCH" in wrong.headers["location"], (
        "the bare origin differs from the recorded issuer path"
    )

    # The recorded issuer itself succeeds.
    start2 = await session.client.get("/api/oauth/start")
    query2 = parse_qs(urlsplit(start2.headers["location"]).query)
    code2 = fake.seed_authorization_code(
        redirect_uri=query2["redirect_uri"][0], challenge=query2["code_challenge"][0]
    )
    ok = await session.client.get(
        "/api/oauth/callback",
        params={
            "code": code2,
            "state": query2["state"][0],
            "iss": "http://mcip.test/tenant",
        },
    )
    assert ok.headers["location"] == "/?connected=oauth"


async def test_start_fails_closed_when_the_metadata_is_missing(
    session: DemoSession, store, fake: FakeMcip
):
    fake.oauth_issuer = None
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    response = await session.client.get("/api/oauth/start")

    assert response.status_code == 503
    assert response.json()["errorCode"] == "OAUTH_METADATA_UNREACHABLE"


async def test_start_requires_demo_public_url_in_oauth_mode(
    session: DemoSession, store, fake: FakeMcip, monkeypatch
):
    """#1439 review item 2: no Host-derived redirect URI in oauth mode."""
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200
    monkeypatch.delenv("DEMO_PUBLIC_URL", raising=False)

    response = await session.client.get("/api/oauth/start")

    assert response.status_code == 503
    assert response.json()["errorCode"] == "OAUTH_PUBLIC_URL_UNSET"


async def test_callback_denial_is_reported(session: DemoSession, store, fake: FakeMcip):
    await _connect_oauth(session, store, fake)
    start = await session.client.get("/api/oauth/start")
    state, _ = _state_and_challenge(start)

    response = await session.client.get(
        "/api/oauth/callback",
        params={"error": "access_denied", "state": state, "iss": "http://mcip.test"},
    )

    assert "oauth_error_code=OAUTH_DENIED" in response.headers["location"]


async def test_a_short_lived_access_token_is_refreshed_before_use(
    session: DemoSession, store, fake: FakeMcip
):
    _, alice_id = await _connect_oauth(session, store, fake)
    # The next request is always "within the skew": the demo must refresh,
    # then call /me with the NEW access token.
    store.update_oauth_access(
        alice_id,
        access_token="ss_oat_test_stale",
        access_expires_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    )
    fake.oauth_access["ss_oat_test_stale"] = True
    before_me = len(fake.oauth_me_bearers)

    response = await session.post("/api/connection/refresh")

    assert response.status_code == 200
    refreshes = [r for r in fake.token_requests if r["form"]["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1
    fresh = fake.oauth_me_bearers[before_me:]
    assert fresh and fresh[0] != "ss_oat_test_stale", "the refreshed token was used"
    tokens = store.get_oauth_tokens(alice_id)
    assert tokens["access_token"] == fresh[0]
    assert tokens["refresh_token"].startswith("ss_ort_"), "no rotation"


async def test_a_401_oauth_token_expired_is_retried_once(
    session: DemoSession, store, fake: FakeMcip
):
    await _connect_oauth(session, store, fake)
    fake.oauth_me_error_once = (401, error_body("OAUTH_TOKEN_EXPIRED"))
    before_me = len(fake.oauth_me_bearers)

    response = await session.post("/api/connection/refresh")

    assert response.status_code == 200
    assert len(fake.oauth_me_bearers) == before_me + 2, "first attempt + the retry"
    refreshes = [r for r in fake.token_requests if r["form"]["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1, "exactly one silent refresh"


async def test_invalid_grant_on_refresh_drops_the_connection(
    session: DemoSession, store, fake: FakeMcip
):
    _, alice_id = await _connect_oauth(session, store, fake)
    store.update_oauth_access(
        alice_id,
        access_token="ss_oat_test_stale",
        access_expires_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    )
    fake.oauth_access["ss_oat_test_stale"] = True
    fake.refresh_invalid_grant = True

    response = await session.post("/api/connection/refresh")

    assert response.status_code == 401
    assert response.json()["errorCode"] == "NOT_CONNECTED"
    assert store.get_connection(alice_id) is None, "the dead connection is gone"


async def test_disconnect_revokes_at_mcip_then_deletes_locally(
    session: DemoSession, store, fake: FakeMcip
):
    _, alice_id = await _connect_oauth(session, store, fake)
    tokens = store.get_oauth_tokens(alice_id)

    response = await session.delete("/api/connection")

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert len(fake.revoke_requests) == 1
    assert fake.revoke_requests[0]["token"] == tokens["refresh_token"], (
        "the refresh token is revoked, not just the access one"
    )
    assert fake.revoke_requests[0]["authorization"] == fake._basic_expected()
    assert store.get_connection(alice_id) is None


async def test_disconnect_reports_a_failed_revoke_but_still_deletes(
    session: DemoSession, store, fake: FakeMcip
):
    _, alice_id = await _connect_oauth(session, store, fake)
    fake.oauth_client_secret = "ss_cli_rotated_elsewhere_0123456789"  # pragma: allowlist secret

    response = await session.delete("/api/connection")

    assert response.status_code == 200
    assert response.json()["revoked"] is False
    assert store.get_connection(alice_id) is None, "local delete is never blocked"


async def test_oauth_mode_disables_the_paste_path(session: DemoSession, store, fake: FakeMcip):
    await _enable_oauth(session, store, fake)
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200

    response = await session.connect()

    assert response.status_code == 409
    assert response.json()["errorCode"] == "PAT_DISABLED"


async def test_session_and_settings_expose_the_mode(session: DemoSession, store, fake: FakeMcip):
    await _enable_oauth(session, store, fake)

    boot = await session.boot()
    assert boot["auth_mode"] == AUTH_MODE_OAUTH
    settings = (await session.client.get("/api/admin/settings")).json()["settings"]
    assert settings["auth_mode"] == AUTH_MODE_OAUTH
    assert settings["oauth_client_id"] == fake.oauth_client_id
    assert settings["oauth_redirect_uri"] == "http://demo.test/api/oauth/callback"
    assert settings["public_url_configured"] is True

    # Back to PAT: the paste path works again without a redeploy.
    assert (
        await session.put("/api/admin/settings", {"auth_mode": AUTH_MODE_PAT})
    ).status_code == 200
    assert (await session.boot())["auth_mode"] == AUTH_MODE_PAT
    store.create_user("bob", "password123")
    assert (await session.login("bob")).status_code == 200
    assert (await session.connect()).status_code == 200


def test_redact_covers_the_oauth_prefixes():
    text = "access ss_oat_abc123 refresh ss_ort_def456 pat ss_pat_x cli ss_cli_y"
    redacted = redact(text)
    assert "ss_oat_abc123" not in redacted
    assert "ss_ort_def456" not in redacted
    assert "ss_pat_x" not in redacted
    assert "ss_cli_y" not in redacted


def test_the_migration_relaxes_the_pat_column_on_an_old_database(tmp_path):
    """A database created before #1389 had key_ciphertext NOT NULL."""
    import sqlite3

    from cryptography.fernet import Fernet

    from app.store import Store

    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE mcip_connections (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            key_ciphertext BLOB NOT NULL,
            key_prefix TEXT NOT NULL,
            mcip_user_id TEXT,
            display_name TEXT,
            email TEXT,
            api_client_name TEXT,
            expires_at TEXT,
            workspaces_json TEXT NOT NULL DEFAULT '[]',
            workspace_id INTEGER,
            workspace_name TEXT,
            connected_at TEXT NOT NULL,
            workspaces_updated_at TEXT
        );
        INSERT INTO users (username, password_hash, created_at, is_admin)
            VALUES ('alice', 'x', '2026-01-01T00:00:00+00:00', 0);
        INSERT INTO mcip_connections (user_id, key_ciphertext, key_prefix, connected_at)
            VALUES (1, X'00', 'ss_pat_test', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    store = Store(db_path, Fernet.generate_key().decode())
    store.save_oauth_connection(
        1,
        access_token="ss_oat_x",
        access_expires_at=None,
        refresh_token="ss_ort_y",
        prefix="ss_oat_x"[:16],
        mcip_user_id=None,
        display_name=None,
        email=None,
        api_client_name=None,
        workspaces=[],
    )

    assert store.get_oauth_tokens(1)["refresh_token"] == "ss_ort_y"
