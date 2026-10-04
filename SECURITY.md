# Security

This is a **demo/reference integration**, not a hardened product. Still, it
handles real MCip API keys, so the rules below are part of the example:

* Users can only connect a purpose-made **chat-scoped key** (`ss_pat_...`) of
  their own. The demo never accepts MCip passwords.
* Keys are **encrypted at rest** (Fernet, `DEMO_ENCRYPTION_KEY`) in the SQLite
  database and are never sent to the browser, put in URLs, or logged. Only the
  key's 16-character prefix is stored in clear text and displayed.
* All state-changing HTTP endpoints require a CSRF token (double-submit
  cookie) and an authenticated session cookie (`HttpOnly`, `SameSite=Lax`,
  `Secure` when `DEMO_HTTPS_ONLY=true`).
* The browser talks only to the demo backend; every `/api/v1/ext` call is
  made server-side with the user's key.
* Rate limits: logins per IP, chat turns per user (see `app/main.py`).

## Reporting a vulnerability

Do not open a public issue for anything that could expose an API key or a
user's data. Report privately to the maintainers (see the repository's
contact page) with steps to reproduce. You should get a reply within a few
business days.

## If a key leaks

1. Revoke the key in MCip (API keys page) — that stops it immediately.
2. Anything stored here is unusable without `DEMO_ENCRYPTION_KEY`; still,
   delete the user (`python -m app.admin_cli remove-user <name>`) to drop the
   ciphertext, and rotate the encryption key if the environment may be
   compromised (this disconnects every stored connection).
