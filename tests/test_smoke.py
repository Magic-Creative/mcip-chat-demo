"""Opt-in smoke test against a live MCip deployment (the last acceptance step).

    MCIP_SMOKE_BASE_URL=https://dev-mcip.example.com \\
    MCIP_SMOKE_KEY=ss_pat_... \\
    MCIP_SMOKE_WORKSPACE_ID=12 \\
    uv run pytest -m smoke -v

Skipped unless all three variables are set; never runs in the normal suite.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.mcip import McipClient
from app.relay import relay_turn

pytestmark = pytest.mark.smoke


def smoke_env() -> tuple[str, str, str]:
    base = os.environ.get("MCIP_SMOKE_BASE_URL", "").strip()
    key = os.environ.get("MCIP_SMOKE_KEY", "").strip()
    workspace = os.environ.get("MCIP_SMOKE_WORKSPACE_ID", "").strip()
    if not (base and key and workspace):
        pytest.skip("set MCIP_SMOKE_BASE_URL, MCIP_SMOKE_KEY and MCIP_SMOKE_WORKSPACE_ID")
    return base, key, workspace


async def test_me_and_one_streamed_turn():
    base, key, workspace = smoke_env()
    async with McipClient(base, key) as mcip:
        me = await mcip.me()
        assert me["user"]["email"]
        assert any(int(w["id"]) == int(workspace) for w in me["workspaces"])

        events = [
            event
            async for event in relay_turn(
                mcip,
                workspace_id=int(workspace),
                message="Reply with the single word: pong",
                conversation_id=None,
                stream=True,
                client_request_id=f"smoke-{uuid.uuid4().hex}",
            )
        ]

    assert events[0]["event"] == "start"
    assert events[0]["conversation_id"]
    assert events[-1]["event"] == "done"
    assert events[-1]["status"] == "completed"
    answer = "".join(event.get("text") or "" for event in events if event["event"] == "delta")
    assert answer.strip()
