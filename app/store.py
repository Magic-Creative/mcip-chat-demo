"""Storage: SQLite for users, connections and conversations; Fernet for keys.

Key-handling rules from the guide (§11) that this module implements:

* the MCip key is encrypted at rest (Fernet, ``DEMO_ENCRYPTION_KEY``) and
  never leaves the backend except as the outgoing ``Authorization`` header;
* only the key's 16-character prefix is ever stored in clear text or logged;
* a disconnect on 401 is a delete of the stored ciphertext.

Schema (five tables): ``users``, ``mcip_connections``, ``conversations``,
``turn_requests`` and ``app_settings`` (admin settings: MCip base URL and the
API client key, the latter Fernet-encrypted like user keys).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mcip_connections (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    key_ciphertext  BLOB NOT NULL,
    key_prefix      TEXT NOT NULL,
    mcip_user_id    TEXT,
    display_name    TEXT,
    email           TEXT,
    api_client_name TEXT,
    expires_at      TEXT,
    workspaces_json TEXT NOT NULL DEFAULT '[]',
    workspace_id    INTEGER,
    workspace_name  TEXT,
    connected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mcip_conversation_id INTEGER,
    title            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_conversations_user ON conversations(user_id, updated_at DESC);

-- What the first attempt of a turn sent as `conversation_id` (NULL = the turn
-- started a new MCip conversation). MCip scopes idempotency by that value, so
-- a retry must repeat it exactly — see Store.record_turn_request.
CREATE TABLE IF NOT EXISTS turn_requests (
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_request_id    TEXT NOT NULL,
    mcip_conversation_id INTEGER,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (user_id, client_request_id)
);

-- Admin settings (name -> value). Secret values are stored as Fernet tokens.
CREATE TABLE IF NOT EXISTS app_settings (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""

#: Setting names (``app_settings.name``).
SETTING_BASE_URL = "mcip_base_url"
SETTING_CLIENT_KEY = "mcip_client_key"  # Fernet token of the ``ss_cli_…`` key
SETTING_CLIENT_KEY_PREFIX = "mcip_client_key_prefix"

#: ``ss_pat_…`` keys anywhere in a string; used by :func:`redact` for logs.
KEY_PATTERN = re.compile(r"ss_(pat|cli)_[A-Za-z0-9_\-]+")
#: The prefix MCip shows for a key (``GET /me`` → ``key.prefix``).
KEY_PREFIX_LENGTH = 16
#: How long a turn's first-attempt conversation param stays on record. MCip
#: replays a finished turn for 10 minutes (Guide §8); keep a wider margin.
TURN_REQUEST_TTL_HOURS = 24

_hasher = PasswordHasher()


def now_iso() -> str:
    # Sub-second precision keeps "most recently touched" ordering meaningful
    # between rows written in the same second (list_conversations).
    return datetime.now(UTC).isoformat()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # malformed hash in the database
        return False


def key_prefix(key: str) -> str:
    """The part of a key that is safe to store and log (Guide §11)."""
    return key[:KEY_PREFIX_LENGTH]


def redact(text: str) -> str:
    """Replace full keys with ``ss_pat_[redacted]`` (or ``ss_cli_…``) — for
    logs and errors."""
    return KEY_PATTERN.sub(lambda m: f"ss_{m.group(1)}_[redacted]", text)


def iso_or_none(value: str | None) -> str | None:
    return value or None


class Store:
    """All database access. Methods are synchronous: calls made from request
    handlers go through ``asyncio.to_thread`` (see ``main.py``)."""

    def __init__(self, db_path: Path, encryption_key: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        raw_key = encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
        self._fernet = Fernet(raw_key)
        self._init_lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._init_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            # Databases created before the admin role: add the column in place.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
            if "is_admin" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
                )

    # -- users ---------------------------------------------------------------

    def create_user(self, username: str, password: str, *, is_admin: bool = False) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users (username, password_hash, created_at, is_admin) "
                "VALUES (?, ?, ?, ?)",
                (username, hash_password(password), now_iso(), int(is_admin)),
            )
            return int(cursor.lastrowid)

    def set_admin(self, username: str, is_admin: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET is_admin = ? WHERE username = ?", (int(is_admin), username)
            )
            return cursor.rowcount > 0

    def is_admin(self, user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT is_admin FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return bool(row and row["is_admin"])

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, username, created_at, is_admin FROM users ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_password(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
            return cursor.rowcount > 0

    def delete_user(self, username: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE username = ?", (username,))
            return cursor.rowcount > 0

    # -- MCip connections ----------------------------------------------------

    def save_connection(
        self,
        user_id: int,
        *,
        key: str,
        prefix: str,
        mcip_user_id: str | None,
        display_name: str | None,
        email: str | None,
        api_client_name: str | None,
        expires_at: str | None,
        workspaces: list[dict[str, Any]],
    ) -> None:
        """Store the key encrypted; replace any previous connection."""
        ciphertext = self._fernet.encrypt(key.encode())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mcip_connections (
                    user_id, key_ciphertext, key_prefix, mcip_user_id, display_name,
                    email, api_client_name, expires_at, workspaces_json,
                    workspace_id, workspace_name, connected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    key_ciphertext = excluded.key_ciphertext,
                    key_prefix = excluded.key_prefix,
                    mcip_user_id = excluded.mcip_user_id,
                    display_name = excluded.display_name,
                    email = excluded.email,
                    api_client_name = excluded.api_client_name,
                    expires_at = excluded.expires_at,
                    workspaces_json = excluded.workspaces_json,
                    workspace_id = NULL,
                    workspace_name = NULL,
                    connected_at = excluded.connected_at
                """,
                (
                    user_id,
                    ciphertext,
                    prefix,
                    mcip_user_id,
                    display_name,
                    email,
                    api_client_name,
                    expires_at,
                    _json_dumps(workspaces),
                    now_iso(),
                ),
            )

    def get_connection(self, user_id: int) -> dict[str, Any] | None:
        """The stored connection without the key."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcip_connections WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data.pop("key_ciphertext", None)
        data["workspaces"] = _json_loads(data.pop("workspaces_json", "[]"))
        return data

    def get_api_key(self, user_id: int) -> str | None:
        """Decrypt the stored key for an outgoing request — the only reader."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key_ciphertext FROM mcip_connections WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(bytes(row["key_ciphertext"])).decode()
        except InvalidToken:
            # Wrong DEMO_ENCRYPTION_KEY, or a corrupted row: treat as absent.
            self.delete_connection(user_id)
            return None

    def set_workspace(self, user_id: int, workspace_id: int, workspace_name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE mcip_connections SET workspace_id = ?, workspace_name = ? "
                "WHERE user_id = ?",
                (workspace_id, workspace_name, user_id),
            )
            return cursor.rowcount > 0

    def delete_connection(self, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM mcip_connections WHERE user_id = ?", (user_id,)
            )
            return cursor.rowcount > 0

    def delete_all_connections(self) -> int:
        """Forget every stored user key (the MCip base URL or client changed)."""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM mcip_connections")
            return cursor.rowcount

    # -- admin settings ------------------------------------------------------

    def get_settings(self) -> dict[str, dict[str, Any]]:
        """Every stored setting row, secret values still encrypted."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM app_settings").fetchall()
        return {row["name"]: dict(row) for row in rows}

    def get_setting(self, name: str) -> str | None:
        row = self.get_settings().get(name)
        return row["value"] if row else None

    def set_setting(self, name: str, value: str | None, *, updated_by: str | None) -> None:
        """Store a setting; ``None`` deletes it."""
        with self._connect() as connection:
            if value is None:
                connection.execute("DELETE FROM app_settings WHERE name = ?", (name,))
                return
            connection.execute(
                """
                INSERT INTO app_settings (name, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (name, value, now_iso(), updated_by),
            )

    def set_client_key(self, key: str | None, *, updated_by: str | None) -> None:
        """Store the API client key encrypted (``None`` clears it)."""
        if key is None:
            self.set_setting(SETTING_CLIENT_KEY, None, updated_by=updated_by)
            self.set_setting(SETTING_CLIENT_KEY_PREFIX, None, updated_by=updated_by)
            return
        token = self._fernet.encrypt(key.encode()).decode()
        self.set_setting(SETTING_CLIENT_KEY, token, updated_by=updated_by)
        self.set_setting(SETTING_CLIENT_KEY_PREFIX, key_prefix(key), updated_by=updated_by)

    def get_client_key(self) -> str | None:
        """Decrypt the client key for an outgoing request — the only reader."""
        token = self.get_setting(SETTING_CLIENT_KEY)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    # -- conversations -------------------------------------------------------

    def create_conversation(self, user_id: int, title: str) -> int:
        stamp = now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO conversations
                    (user_id, mcip_conversation_id, title, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?)
                """,
                (user_id, title, stamp, stamp),
            )
            return int(cursor.lastrowid)

    def set_mcip_conversation_id(self, user_id: int, conversation_id: int, mcip_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET mcip_conversation_id = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (mcip_id, now_iso(), conversation_id, user_id),
            )

    def touch_conversation(self, user_id: int, conversation_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
                (now_iso(), conversation_id, user_id),
            )

    def list_conversations(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, mcip_conversation_id, title, created_at, updated_at "
                "FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, user_id: int, conversation_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_conversation(self, user_id: int, conversation_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            )
            return cursor.rowcount > 0

    # -- turn requests (Guide §8 idempotency memory) -------------------------

    def get_turn_request(self, user_id: int, client_request_id: str) -> dict[str, Any] | None:
        """What the first attempt with this id sent as ``conversation_id``
        (``mcip_conversation_id`` NULL = it started a new conversation)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM turn_requests WHERE user_id = ? AND client_request_id = ?",
                (user_id, client_request_id),
            ).fetchone()
        return dict(row) if row else None

    def record_turn_request(
        self, user_id: int, client_request_id: str, mcip_conversation_id: int | None
    ) -> None:
        """Remember the first attempt's conversation param for retries.

        MCip's idempotency key is scoped by it (``new:ws…`` vs. the
        conversation id), so a retry has to repeat the *original* value — the
        conversation row's stored MCip id is a different request. First write
        wins (``INSERT OR IGNORE``); old rows are pruned lazily.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=TURN_REQUEST_TTL_HOURS)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM turn_requests WHERE created_at < ?", (cutoff.isoformat(),)
            )
            connection.execute(
                "INSERT OR IGNORE INTO turn_requests "
                "(user_id, client_request_id, mcip_conversation_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, client_request_id, mcip_conversation_id, now_iso()),
            )


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def _json_loads(value: str) -> Any:
    import json

    try:
        return json.loads(value)
    except ValueError:
        return []
