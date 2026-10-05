"""``demo-admin``: manage demo users (issue #1184).

Run it where the demo's database and ``DEMO_ENCRYPTION_KEY`` live::

    python -m app.admin_cli add-user admin --admin    # the first demo admin
    python -m app.admin_cli add-user alice            # generates a password
    python -m app.admin_cli add-user alice --password '<chosen>'
    python -m app.admin_cli set-admin alice [--revoke]
    python -m app.admin_cli list-users
    python -m app.admin_cli reset-password alice
    python -m app.admin_cli remove-user alice

A generated password is printed once, on stdout, and never stored in clear
text (the database keeps only its argon2 hash). Removing a user deletes their
connection and conversations (``ON DELETE CASCADE``); stored MCip keys are
ciphertext only and are never printed.
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
from typing import TextIO

from app.config import load_settings
from app.store import Store

#: Ambiguous characters (0/O, 1/l/I) left out: passwords get typed by hand.
_PASSWORD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
GENERATED_PASSWORD_LENGTH = 16


def generate_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))


def _store() -> Store:
    # The CLI needs a database, not an MCip URL: ask only for what it uses.
    settings = load_settings(require_mcip=False)
    return Store(settings.db_path, settings.encryption_key)


def cmd_add_user(store: Store, args: argparse.Namespace, out: TextIO) -> int:
    if store.get_user(args.username):
        out.write(f"user '{args.username}' already exists\n")
        return 1
    password = args.password or generate_password()
    store.create_user(args.username, password, is_admin=args.admin)
    role = "admin" if args.admin else "user"
    out.write(f"created {role} '{args.username}'\n")
    if args.password is None:
        out.write(f"password: {password}\n")
        out.write("(shown once — send it to the user; stored only as a hash)\n")
    return 0


def cmd_list_users(store: Store, args: argparse.Namespace, out: TextIO) -> int:
    users = store.list_users()
    if not users:
        out.write("no users yet — add one with: python -m app.admin_cli add-user <name>\n")
        return 0
    out.write(
        f"{'id':>4}  {'username':<24} {'role':<6} {'connected':<9} {'workspace':<24} created\n"
    )
    for user in users:
        connection = store.get_connection(user["id"])
        connected = "yes" if connection else "no"
        workspace = (connection or {}).get("workspace_name") or "-"
        role = "admin" if user.get("is_admin") else "user"
        out.write(
            f"{user['id']:>4}  {user['username']:<24} {role:<6} {connected:<9} "
            f"{workspace:<24} {user['created_at']}\n"
        )
    return 0


def cmd_remove_user(store: Store, args: argparse.Namespace, out: TextIO) -> int:
    if not store.delete_user(args.username):
        out.write(f"no such user: '{args.username}'\n")
        return 1
    out.write(f"removed user '{args.username}' and their connection and conversations\n")
    return 0


def cmd_reset_password(store: Store, args: argparse.Namespace, out: TextIO) -> int:
    password = args.password or generate_password()
    if not store.set_password(args.username, password):
        out.write(f"no such user: '{args.username}'\n")
        return 1
    out.write(f"new password for '{args.username}': {password}\n")
    if args.password is None:
        out.write("(shown once — stored only as a hash)\n")
    return 0


def cmd_set_admin(store: Store, args: argparse.Namespace, out: TextIO) -> int:
    if not store.set_admin(args.username, not args.revoke):
        out.write(f"no such user: '{args.username}'\n")
        return 1
    role = "a common user" if args.revoke else "a demo admin"
    out.write(f"'{args.username}' is now {role}\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo-admin", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-user", help="Create a demo user.")
    add.add_argument("username")
    add.add_argument(
        "--password",
        default=None,
        help="Use this password instead of a generated one.",
    )
    add.add_argument(
        "--admin",
        action="store_true",
        help="Make the user a demo admin (sets the MCip address and client key).",
    )
    add.set_defaults(func=cmd_add_user)

    admin = sub.add_parser("set-admin", help="Make a user a demo admin (or --revoke).")
    admin.add_argument("username")
    admin.add_argument("--revoke", action="store_true", help="Make them a common user again.")
    admin.set_defaults(func=cmd_set_admin)

    listing = sub.add_parser("list-users", help="List demo users and connections.")
    listing.set_defaults(func=cmd_list_users)

    remove = sub.add_parser(
        "remove-user", help="Delete a user with their connection and conversations."
    )
    remove.add_argument("username")
    remove.set_defaults(func=cmd_remove_user)

    reset = sub.add_parser("reset-password", help="Set a new password.")
    reset.add_argument("username")
    reset.add_argument(
        "--password",
        default=None,
        help="Use this password instead of a generated one.",
    )
    reset.set_defaults(func=cmd_reset_password)
    return parser


def main(
    argv: list[str] | None = None, *, out: TextIO | None = None, store: Store | None = None
) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    store = store or _store()
    return args.func(store, args, out)


if __name__ == "__main__":
    sys.exit(main())
