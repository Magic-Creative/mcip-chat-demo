"""Storage: passwords, the encrypted key, and the conversation rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.store import KEY_PREFIX_LENGTH, Store, key_prefix, redact, verify_password
from tests.conftest import TEST_KEY


def test_create_and_verify_user(store: Store):
    user_id = store.create_user("alice", "s3cret-password")
    user = store.get_user("alice")
    assert user["id"] == user_id
    assert "s3cret-password" not in user["password_hash"]  # hashed, not stored
    assert verify_password(user["password_hash"], "s3cret-password")
    assert not verify_password(user["password_hash"], "wrong")


def test_set_and_check_password(store: Store):
    store.create_user("alice", "first-password")
    assert store.set_password("alice", "second-password")
    user = store.get_user("alice")
    assert verify_password(user["password_hash"], "second-password")
    assert not store.set_password("nobody", "x")


def test_delete_user_cascades(store: Store):
    user_id = store.create_user("alice", "password123")
    store.save_connection(
        user_id,
        key=TEST_KEY,
        prefix=key_prefix(TEST_KEY),
        mcip_user_id="u-1",
        display_name="Ada",
        email="ada@example.com",
        api_client_name="Acme Assist",
        expires_at=None,
        workspaces=[{"id": 11, "name": "Chat Demo"}],
    )
    store.create_conversation(user_id, "Hello", workspace_id=11)
    assert store.delete_user("alice")
    assert store.get_user("alice") is None
    assert store.get_connection(user_id) is None
    assert store.list_conversations(user_id, 11) == []
    assert not store.delete_user("alice")


def test_connection_roundtrip_hides_the_ciphertext(store: Store):
    user_id = store.create_user("alice", "password123")
    store.save_connection(
        user_id,
        key=TEST_KEY,
        prefix="ss_pat_test_only",
        mcip_user_id="u-1",
        display_name="Ada",
        email="ada@example.com",
        api_client_name="Acme Assist",
        expires_at="2027-01-01T00:00:00+00:00",
        workspaces=[{"id": 11, "name": "Chat Demo"}, {"id": 12, "name": "Other"}],
    )
    connection = store.get_connection(user_id)
    assert "key_ciphertext" not in connection
    assert connection["key_prefix"] == "ss_pat_test_only"
    assert connection["workspaces"][1]["name"] == "Other"
    assert store.get_api_key(user_id) == TEST_KEY


def test_key_replacement_clears_the_chosen_workspace(store: Store):
    user_id = store.create_user("alice", "password123")
    common = dict(
        prefix="ss_pat_test_only",
        mcip_user_id="u-1",
        display_name="Ada",
        email="ada@example.com",
        api_client_name="Acme Assist",
        expires_at=None,
        workspaces=[{"id": 11, "name": "Chat Demo"}],
    )
    store.save_connection(user_id, key=TEST_KEY, **common)
    assert store.set_workspace(user_id, 11, "Chat Demo")
    store.save_connection(user_id, key=TEST_KEY + "2", **common)
    assert store.get_connection(user_id)["workspace_id"] is None


def test_key_never_in_plain_text_on_disk(store: Store, settings):
    user_id = store.create_user("alice", "password123")
    store.save_connection(
        user_id,
        key=TEST_KEY,
        prefix=key_prefix(TEST_KEY),
        mcip_user_id=None,
        display_name=None,
        email=None,
        api_client_name=None,
        expires_at=None,
        workspaces=[],
    )
    on_disk = b""
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(settings.db_path) + suffix)
        if path.exists():
            on_disk += path.read_bytes()
    assert TEST_KEY.encode() not in on_disk
    assert b"ss_pat_" in on_disk  # only the prefix is stored in clear text


def test_wrong_encryption_key_drops_the_connection(settings, tmp_path):
    store = Store(settings.db_path, settings.encryption_key)
    user_id = store.create_user("alice", "password123")
    store.save_connection(
        user_id,
        key=TEST_KEY,
        prefix=key_prefix(TEST_KEY),
        mcip_user_id=None,
        display_name=None,
        email=None,
        api_client_name=None,
        expires_at=None,
        workspaces=[],
    )
    other = Store(settings.db_path, Fernet.generate_key().decode())
    assert other.get_api_key(user_id) is None
    # the unreadable row is dropped, not kept around
    assert other.get_connection(user_id) is None


def test_conversation_crud(store: Store):
    user_id = store.create_user("alice", "password123")
    first = store.create_conversation(user_id, "First", workspace_id=11)
    second = store.create_conversation(user_id, "Second", workspace_id=11)
    store.set_mcip_conversation_id(user_id, first, 900)
    store.touch_conversation(user_id, first)
    rows = store.list_conversations(user_id, 11)
    assert [row["id"] for row in rows] == [first, second]
    assert rows[0]["mcip_conversation_id"] == 900
    assert store.get_conversation(user_id, second)["title"] == "Second"
    assert store.delete_conversation(user_id, second)
    assert store.get_conversation(user_id, second) is None


def test_conversations_are_scoped_to_a_workspace(store: Store):
    user_id = store.create_user("alice", "password123")
    chat = store.create_conversation(user_id, "In 11", workspace_id=11)
    store.create_conversation(user_id, "In 12", workspace_id=12)
    assert [row["id"] for row in store.list_conversations(user_id, 11)] == [chat]
    assert [row["title"] for row in store.list_conversations(user_id, 12)] == ["In 12"]
    # no workspace chosen: nothing is listed — rows are never mixed
    assert store.list_conversations(user_id, None) == []


def test_legacy_conversations_are_adopted_by_a_workspace(store: Store):
    user_id = store.create_user("alice", "password123")
    legacy = store.create_conversation(user_id, "Old", workspace_id=11)
    with sqlite3.connect(store.db_path) as connection:  # as they were before #7
        connection.execute("UPDATE conversations SET workspace_id = NULL WHERE id = ?", (legacy,))
    assert store.list_conversations(user_id, 11) == []  # hidden until a workspace is known
    assert store.adopt_orphan_conversations(user_id, 11) == 1
    assert store.adopt_orphan_conversations(user_id, 11) == 0  # no-op afterwards
    rows = store.list_conversations(user_id, 11)
    assert [row["id"] for row in rows] == [legacy]


def test_redact_and_prefix():
    text = "auth failed for ss_pat_abcDEF123_456 in request"
    assert redact(text) == "auth failed for ss_pat_[redacted] in request"
    assert key_prefix("ss_pat_0123456789abcdefEXTRA") == "ss_pat_012345678"
    assert len(key_prefix(TEST_KEY)) == KEY_PREFIX_LENGTH


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$argon2id$broken"])
def test_verify_password_survives_a_malformed_hash(bad_hash: str):
    assert not verify_password(bad_hash, "whatever")
