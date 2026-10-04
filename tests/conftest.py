"""Test setup: environment, the demo app with a fake MCip, and helpers.

The suite never touches the network: MCip is the ASGI app in ``fake_mcip.py``
and the demo's own HTTP calls go through ``httpx.ASGITransport``.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="mcip-demo-tests-"))

# Set before anything imports app.main (which loads settings at import time).
os.environ["DEMO_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["DEMO_SESSION_SECRET"] = "test-session-secret-" + "x" * 24
os.environ["DEMO_DB_PATH"] = str(_TMP / "module-import.db")
os.environ["MCIP_BASE_URL"] = "http://mcip.test"
os.environ["DEMO_HTTPS_ONLY"] = "false"

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from tests.fake_mcip import FakeMcip  # noqa: E402

TEST_KEY = "ss_pat_test_only_0123456789abcdef"  # pragma: allowlist secret


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        mcip_base_url="http://mcip.test",
        encryption_key=Fernet.generate_key().decode(),
        session_secret="test-session-secret",
        port=8090,
        db_path=tmp_path / "demo.db",
        allow_register=False,
        https_only=False,
        trust_cf_header=True,
        login_rate_per_minute=5,
        chat_rate_per_minute=20,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.db_path, settings.encryption_key)


@pytest.fixture
def fake() -> FakeMcip:
    return FakeMcip()


@pytest.fixture
def demo_app(settings: Settings, store: Store, fake: FakeMcip):
    transport = httpx.ASGITransport(app=fake.app)
    return create_app(settings, store=store, mcip_transport=transport)


class DemoSession:
    """One browser-like session: cookies plus the CSRF dance."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.csrf = ""

    async def boot(self) -> dict:
        response = await self.client.get("/api/session")
        assert response.status_code == 200
        payload = response.json()
        self.csrf = payload["csrf_token"]
        return payload

    async def _headers(self, csrf: bool) -> dict[str, str]:
        if csrf and not self.csrf:
            await self.boot()  # a real browser visits the page first
        return {"X-CSRF-Token": self.csrf} if csrf else {}

    async def post(self, path: str, json: dict | None = None, *, csrf: bool = True):
        return await self.client.post(path, json=json, headers=await self._headers(csrf))

    async def put(self, path: str, json: dict | None = None, *, csrf: bool = True):
        return await self.client.put(path, json=json, headers=await self._headers(csrf))

    async def delete(self, path: str, *, csrf: bool = True):
        return await self.client.delete(path, headers=await self._headers(csrf))

    async def register(self, username: str, password: str = "password123"):
        response = await self.post("/api/register", {"username": username, "password": password})
        if response.status_code == 200:
            self.csrf = response.json()["csrf_token"]
        return response

    async def login(self, username: str, password: str = "password123"):
        response = await self.post("/api/login", {"username": username, "password": password})
        if response.status_code == 200:
            self.csrf = response.json()["csrf_token"]
        return response

    async def connect(self, key: str = TEST_KEY):
        response = await self.post("/api/connection", {"key": key})
        return response

    async def choose_workspace(self, workspace_id: int = 11):
        return await self.put("/api/connection/workspace", {"workspace_id": workspace_id})


@pytest.fixture
async def session(demo_app) -> DemoSession:
    transport = httpx.ASGITransport(app=demo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo.test") as client:
        yield DemoSession(client)


@pytest.fixture
async def ready(session: DemoSession, store: Store) -> DemoSession:
    """A signed-in, connected user with a chosen workspace."""
    store.create_user("alice", "password123")
    assert (await session.login("alice")).status_code == 200
    assert (await session.connect()).status_code == 200
    assert (await session.choose_workspace()).status_code == 200
    return session


def parse_sse_text(text: str) -> list[dict]:
    """The demo's SSE response body as a list of event objects."""
    import json

    events = []
    for frame in text.split("\n\n"):
        lines = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        events.append(json.loads("".join(lines)))
    return events


@pytest.fixture
def app_with(settings: Settings, store: Store, fake: FakeMcip):
    """Build a demo app with settings overridden (e.g. allow_register=True)."""

    def build(**overrides):
        transport = httpx.ASGITransport(app=fake.app)
        return create_app(
            dataclasses.replace(settings, **overrides), store=store, mcip_transport=transport
        )

    return build
