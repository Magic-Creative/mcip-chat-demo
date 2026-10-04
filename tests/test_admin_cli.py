"""``python -m app.admin_cli`` — user management without a server."""

from __future__ import annotations

import io

from app.admin_cli import GENERATED_PASSWORD_LENGTH, main
from app.store import Store, verify_password


def run(argv: list[str], store: Store) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, store=store)
    return code, out.getvalue()


def test_add_user_generates_a_password(store: Store):
    code, output = run(["add-user", "carol"], store)
    assert code == 0
    assert "created user 'carol'" in output
    password = output.split("password: ")[1].splitlines()[0]
    assert len(password) == GENERATED_PASSWORD_LENGTH
    user = store.get_user("carol")
    assert verify_password(user["password_hash"], password)
    assert password not in user["password_hash"]


def test_add_user_with_explicit_password_and_duplicate(store: Store):
    code, _ = run(["add-user", "carol", "--password", "chosen-password"], store)
    assert code == 0
    assert verify_password(store.get_user("carol")["password_hash"], "chosen-password")
    code, output = run(["add-user", "carol", "--password", "again"], store)
    assert code == 1
    assert "already exists" in output


def test_list_users_shows_connection_state(store: Store):
    code, output = run(["list-users"], store)
    assert code == 0
    assert "no users yet" in output

    user_id = store.create_user("carol", "password123")
    store.save_connection(
        user_id,
        key="ss_pat_test_only_0123456789abcdef",  # pragma: allowlist secret
        prefix="ss_pat_test_only",
        mcip_user_id=None,
        display_name=None,
        email=None,
        api_client_name=None,
        expires_at=None,
        workspaces=[],
    )
    store.set_workspace(user_id, 11, "Chat Demo")
    code, output = run(["list-users"], store)
    assert code == 0
    assert "carol" in output
    assert "yes" in output
    assert "Chat Demo" in output


def test_reset_password(store: Store):
    store.create_user("carol", "old-password")
    code, output = run(["reset-password", "carol"], store)
    assert code == 0
    password = output.split(": ")[1].splitlines()[0]
    assert verify_password(store.get_user("carol")["password_hash"], password)

    code, output = run(["reset-password", "nobody"], store)
    assert code == 1
    assert "no such user" in output


def test_remove_user(store: Store):
    store.create_user("carol", "password123")
    code, output = run(["remove-user", "carol"], store)
    assert code == 0
    assert "removed user 'carol'" in output
    assert store.get_user("carol") is None

    code, output = run(["remove-user", "carol"], store)
    assert code == 1
    assert "no such user" in output
