"""Configuration: read `.env` (if present) then the process environment.

The demo needs three settings to run (Guide §3): the MCip base URL, a Fernet
key for stored MCip keys, and a secret for the session cookie. Everything else
has a working default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "demo.db"


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from ``.env``; never override real env vars."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    mcip_base_url: str
    encryption_key: str
    session_secret: str
    port: int
    db_path: Path
    allow_register: bool
    https_only: bool
    trust_cf_header: bool
    login_rate_per_minute: int
    chat_rate_per_minute: int

    @property
    def mcip_host(self) -> str:
        return self.mcip_base_url.split("://", 1)[-1].rstrip("/")


def load_settings(*, require_mcip: bool = True) -> Settings:
    """Build settings from the environment.

    ``require_mcip=False`` is for the admin CLI, which only needs the database
    and the encryption key.
    """
    load_dotenv()
    base_url = os.environ.get("MCIP_BASE_URL", "").strip().rstrip("/")
    if require_mcip and not base_url:
        raise SystemExit(
            "MCIP_BASE_URL is not set. Copy .env.example to .env and set it to "
            "your deployment, e.g. https://mcip.example.com"
        )
    encryption_key = os.environ.get("DEMO_ENCRYPTION_KEY", "").strip()
    if not encryption_key:
        raise SystemExit(
            "DEMO_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet;'
            ' print(Fernet.generate_key().decode())"'
        )
    session_secret = os.environ.get("DEMO_SESSION_SECRET", "").strip()
    if require_mcip and not session_secret:
        raise SystemExit(
            "DEMO_SESSION_SECRET is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return Settings(
        mcip_base_url=base_url,
        encryption_key=encryption_key,
        session_secret=session_secret or "unused-by-cli",
        port=_int("DEMO_PORT", 8090),
        db_path=Path(os.environ.get("DEMO_DB_PATH", "").strip() or DEFAULT_DB_PATH),
        allow_register=_flag("DEMO_ALLOW_REGISTER", False),
        https_only=_flag("DEMO_HTTPS_ONLY", False),
        # On by default: the documented deployment is behind the Cloudflare
        # tunnel with the port bound to loopback, where CF-Connecting-IP is
        # trustworthy. Set to false if the port is ever published directly.
        trust_cf_header=_flag("DEMO_TRUST_CF_HEADER", True),
        login_rate_per_minute=_int("DEMO_LOGIN_RATE_PER_MIN", 5),
        chat_rate_per_minute=_int("DEMO_CHAT_RATE_PER_MIN", 20),
    )
