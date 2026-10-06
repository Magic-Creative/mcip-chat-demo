"""Headless-browser checks for the workspace switcher (issue #7) — opt-in.

Requires Playwright and its Chromium build, which are not part of the default
dev environment:

    uv sync --group browser
    uv run playwright install chromium
    uv run pytest -m browser -v

Only the demo app itself listens (on a loopback port the OS picks), so a real
browser can load the page; MCip stays the in-process fake on an ASGI
transport, and the demo session cookie is seeded over httpx — this suite
never touches the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import uvicorn

from app.store import Store
from tests.conftest import DemoSession
from tests.fake_mcip import FakeMcip, scripted_turn

pytest.importorskip("playwright.async_api", reason="run: uv sync --group browser")

from playwright.async_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    expect,
)

pytestmark = pytest.mark.browser

DEFAULT_WORKSPACES = [
    {"id": 11, "name": "Chat Demo"},
    {"id": 12, "name": "Second Workspace"},
]


@pytest.fixture
def live_url(demo_app) -> Iterator[str]:
    """The demo app on a real loopback port: the browser needs an origin."""
    config = uvicorn.Config(demo_app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the demo server did not start")
        time.sleep(0.02)
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        try:
            launched = await playwright.chromium.launch()
        except Exception as exc:  # the Chromium build is not downloaded
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        try:
            yield launched
        finally:
            await launched.close()


async def signed_in_context(
    browser: Browser, live_url: str, username: str, *, workspace_id: int = 11
) -> BrowserContext:
    """A browser context carrying the demo session of a connected user."""
    async with httpx.AsyncClient(base_url=live_url) as client:
        session = DemoSession(client)
        await session.boot()
        assert (await session.login(username)).status_code == 200
        assert (await session.connect()).status_code == 200
        assert (await session.choose_workspace(workspace_id)).status_code == 200
        cookies = [
            {"name": cookie.name, "value": cookie.value, "url": live_url}
            for cookie in client.cookies.jar
        ]
    context = await browser.new_context()
    await context.add_cookies(cookies)
    return context


async def open_chat(context: BrowserContext, live_url: str) -> Page:
    page = await context.new_page()
    await page.goto(live_url + "/")
    await expect(page.locator("#view-chat")).to_be_visible()
    return page


async def test_switching_workspaces_swaps_the_sidebar_and_the_chat(
    live_url: str, browser: Browser, store: Store, fake: FakeMcip
) -> None:
    store.create_user("alice", "password123")
    alice_id = store.get_user("alice")["id"]
    store.create_conversation(alice_id, "In 11", workspace_id=11)
    store.create_conversation(alice_id, "In 12", workspace_id=12)

    context = await signed_in_context(browser, live_url, "alice")
    try:
        page = await open_chat(context, live_url)
        select = page.locator("#workspace-select")
        titles = page.locator(".conversation-title")
        await expect(select).to_have_value("11")
        await expect(titles).to_have_text(["In 11"])

        # open a conversation: switching away closes it
        await page.locator(".conversation-open").click()
        await expect(page.locator("#empty-state")).to_be_hidden()

        await select.select_option("12")
        await expect(titles).to_have_text(["In 12"])
        await expect(page.locator("#empty-state")).to_be_visible()

        # the composer now starts a new chat in workspace 12
        fake.enqueue_sse(scripted_turn())
        await page.fill("#composer-input", "Hello from 12")
        await page.press("#composer-input", "Enter")
        await expect(page.locator("#status-line")).to_have_text("Done.")
        assert fake.chat_requests[-1]["workspace_id"] == 12

        # switching back: workspace 11 still has its own list
        await select.select_option("11")
        await expect(titles).to_have_text(["In 11"])
    finally:
        await context.close()


async def test_refresh_picks_up_a_new_workspace(
    live_url: str, browser: Browser, store: Store, fake: FakeMcip
) -> None:
    store.create_user("alice", "password123")
    context = await signed_in_context(browser, live_url, "alice")
    try:
        page = await open_chat(context, live_url)
        select = page.locator("#workspace-select")
        await expect(select.locator("option")).to_have_count(2)

        fake.me_payload = {
            **fake.me_payload,
            "workspaces": [*DEFAULT_WORKSPACES, {"id": 13, "name": "Third Workspace"}],
        }
        await page.click("#workspace-refresh")
        await expect(page.locator("#workspace-status")).to_have_text("Updated just now")
        await expect(select.locator("option")).to_have_count(3)
        await expect(select).to_have_value("11")  # the active one survives
    finally:
        await context.close()


async def test_the_switcher_is_locked_while_an_answer_streams(
    live_url: str, browser: Browser, store: Store, fake: FakeMcip
) -> None:
    store.create_user("alice", "password123")
    context = await signed_in_context(browser, live_url, "alice")
    try:
        page = await open_chat(context, live_url)
        fake.pacing = 0.6  # a slow turn: the streaming state stays observable
        fake.enqueue_sse(scripted_turn())
        await page.fill("#composer-input", "a slow answer")
        await page.press("#composer-input", "Enter")

        select = page.locator("#workspace-select")
        refresh = page.locator("#workspace-refresh")
        row = page.locator("#workspace-row")
        await expect(select).to_be_disabled()
        await expect(refresh).to_be_disabled()
        await expect(row).to_have_attribute("title", "Wait for the answer, or press Stop")

        await expect(page.locator("#status-line")).to_have_text("Done.", timeout=20_000)
        await expect(select).to_be_enabled()
        await expect(refresh).to_be_enabled()
        assert (await row.get_attribute("title")) in (None, "")
    finally:
        await context.close()


async def test_a_single_workspace_is_plain_text_with_refresh(
    live_url: str, browser: Browser, store: Store, fake: FakeMcip
) -> None:
    store.create_user("bob", "password123")
    fake.me_payload = {**fake.me_payload, "workspaces": [{"id": 11, "name": "Only Workspace"}]}
    context = await signed_in_context(browser, live_url, "bob")
    try:
        page = await open_chat(context, live_url)
        await expect(page.locator("#workspace-select")).to_be_hidden()
        await expect(page.locator("#workspace-name")).to_have_text("Only Workspace")
        await expect(page.locator("#workspace-refresh")).to_be_visible()
        await expect(page.locator("#workspace-empty")).to_be_hidden()
    finally:
        await context.close()


async def test_refresh_with_no_workspace_left_shows_the_advice(
    live_url: str, browser: Browser, store: Store, fake: FakeMcip
) -> None:
    store.create_user("alice", "password123")
    context = await signed_in_context(browser, live_url, "alice")
    try:
        page = await open_chat(context, live_url)
        fake.me_payload = {**fake.me_payload, "workspaces": []}
        await page.click("#workspace-refresh")
        await expect(page.locator("#view-workspace")).to_be_visible()
        await expect(page.locator("#workspace-list")).to_contain_text(
            "chat access to one of its workspaces"
        )
        await expect(page.locator("#workspace-error")).to_contain_text("no longer available")
    finally:
        await context.close()


async def test_the_control_is_labelled_and_keyboard_reachable(
    live_url: str, browser: Browser, store: Store
) -> None:
    store.create_user("alice", "password123")
    alice_id = store.get_user("alice")["id"]
    store.create_conversation(alice_id, "In 11", workspace_id=11)
    store.create_conversation(alice_id, "In 12", workspace_id=12)

    context = await signed_in_context(browser, live_url, "alice")
    try:
        page = await open_chat(context, live_url)
        select = page.locator("#workspace-select")
        labelled = page.get_by_label("Workspace", exact=True)  # not "Refresh workspaces"
        await expect(labelled).to_have_count(1)
        assert (await labelled.get_attribute("id")) == "workspace-select"

        # tab order: dropdown -> Refresh -> New chat
        await select.focus()
        await page.keyboard.press("Tab")
        assert (await page.evaluate("document.activeElement.id")) == "workspace-refresh"
        await page.keyboard.press("Tab")
        assert (await page.evaluate("document.activeElement.id")) == "new-chat"

        # a keyboard change applies immediately (change event, no submit)
        await select.focus()
        await page.keyboard.press("ArrowDown")
        await expect(select).to_have_value("12")
        await expect(page.locator(".conversation-title")).to_have_text(["In 12"])
    finally:
        await context.close()
