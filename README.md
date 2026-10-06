# MCip chat demo

A complete reference integration for the **MCip External Chat API** (`/api/v1/ext`):
a browser chat UI in front of a small FastAPI backend that holds **each user's own
MCip API key** and relays MCip's SSE answer stream back to the browser.

It is the working example that accompanies the integration guide
([`docs/integration-guide.md`](docs/integration-guide.md)): every error-code branch,
retry rule and storage rule in the guide has a named place in this code — the
[file map](#which-file-shows-what) below points at them.

```
Your user ──► this demo's UI ──► this demo's backend ── looks up this user's key
                                       │
                                       ▼   POST /api/v1/ext/chat
                                   MCip External Chat API
                                       │   Authorization: Bearer ss_pat_…  (that user)
                                       ▼
                                   SSE: start → delta… → citation… → done
```

What it demonstrates, end to end:

- **Per-user keys.** The key is pasted once (write-only in the UI), validated with
  `GET /me`, encrypted at rest (Fernet), and only ever shown back as its 16-character
  prefix. Disconnecting deletes the stored ciphertext.
- **The SSE relay.** MCip's `start` / `delta` / `status` / `citation` /
  `action_rejected` / `error` / `done` events are normalized for the browser, with a
  `ui` state and a one-line `advice` attached to every error. MCip's `: keep-alive`
  heartbeats are forwarded as SSE comments, so slow turns stream through proxies
  (Cloudflare cuts idle connections at 100 s) without a stall.
- **Retries with a countdown.** 429 / 409-busy / 5xx / network failures are retried in
  the backend (up to 4 attempts, same `client_request_id`) while the browser shows each
  retry live. A stream cut before `done` becomes `STREAM_TRUNCATED` — the browser's
  **Retry** button re-sends the same `client_request_id`, and a turn that finished
  server-side comes back as stored JSON ("recovered, not charged again").
- **A working GUI.** Sign-in, workspace picker, streaming markdown with citations,
  conversation list, transcript pagination from MCip, delete, stop, light/dark theme,
  responsive drawer — as plain static files, no build step.
- **A workspace switcher in the sidebar.** The dropdown lists what the connected key
  may use (plain text when there is exactly one); switching scopes the conversation
  list — chats of other workspaces stay hidden and their transcripts 404, so a stale
  tab can never chat into the wrong workspace. **Refresh** re-reads the workspace list
  from MCip (`GET /me`); a workspace the key lost clears the choice and reopens the
  picker, and a snapshot older than 10 minutes is refreshed once per boot.
- **Chat hardening.** HttpOnly session cookie, CSRF double-submit on every
  state-changing route, strict CSP (no inline code), demo-side rate limits, and a log
  filter that redacts `ss_pat_…` keys.

## Quick start

You need an MCip deployment with an **API client** registered for this demo, bound
to the organisation whose workspaces the demo users chat in — see
[`docs/integration-guide.md` §2](docs/integration-guide.md#2-onboarding). Each demo
user then creates their own *External system* chat key in MCip and pastes it into the
"Connect MCip" screen.

**Two roles.** A **demo admin** sets the **MCip address** and the **API client
key** (`ss_cli_…`, from the client's page in MCip) in **Settings**; a **common
user** only signs in and connects their own MCip chat key. Changing either admin
setting disconnects every user, so no stored key is ever sent to a different MCip
or used for another client. Nothing about MCip is hard-coded: any organization's
API client works.

### Option A — locally with uv (Python 3.12+)

```bash
uv sync
cp .env.example .env                 # set the two secrets below
uv run python -m app.admin_cli add-user admin --admin   # prints a password once
uv run uvicorn app.main:app --port 8090
# sign in as admin -> Settings: MCip address + API client key
```

No uv? Create a venv, `pip install` the pinned dependencies from `pyproject.toml`,
then run the same two commands with that Python.

### Option B — Docker Compose

```bash
cp .env.example .env                 # the two secrets
docker compose up -d --build
docker compose exec demo python -m app.admin_cli add-user admin --admin
```

The image installs from `requirements.txt` — the frozen, hash-pinned export of
`uv.lock` (transitive dependencies included, every wheel verified with
`pip --require-hashes`). After changing `pyproject.toml` or `uv.lock`, regenerate it:

```bash
uv export --frozen --no-dev --format requirements-txt --no-emit-project \
    --output-file requirements.txt
```

Either way the demo is on <http://127.0.0.1:8090> (loopback only). Run it as a
single Uvicorn worker: the rate limiter is an in-process, in-memory counter, so
`--workers N` would multiply the configured limits by N.

### Configuration

**In the GUI (demo admin → Settings),** stored in the database:

| Setting | Purpose |
|---|---|
| MCip address | Your MCip deployment, e.g. `https://mcip.example.com` (no `/api/v1/ext` suffix). `https://` only; `http://` is accepted for localhost (or with `DEMO_ALLOW_INSECURE_MCIP=true`). **Test connection** checks it, and your own key plus the client key with `GET /me`. |
| API client key | The `ss_cli_…` key MCip shows once when the API client is created or its key rotated. Sent as `X-MCip-Client-Key` on every MCip call. Stored Fernet-encrypted; only its prefix is ever shown. Optional while the MCip client doesn't require one. |

Until an admin saves an address, admins land on Settings and common users see "Not
set up yet".

**In the environment** (and `.env`, if present — real environment variables win).
Only the first two are required; they protect the database and sessions, so they
deliberately stay out of the GUI:

| Variable | Purpose |
|---|---|
| `DEMO_ENCRYPTION_KEY` | Fernet key that encrypts stored MCip API keys at rest. Rotating it disconnects everyone. |
| `DEMO_SESSION_SECRET` | Signs the demo's session cookies (32+ random characters). |
| `MCIP_BASE_URL` | Optional first-run default for the MCip address, used until an admin saves one in Settings. |
| `DEMO_ALLOW_INSECURE_MCIP` | `false` (default). `true` allows an `http://` MCip address for a non-local host (labs only). |
| `DEMO_ALLOW_REGISTER` | `false` (default): accounts are created with the admin CLI. `true` opens self-registration. |
| `DEMO_HTTPS_ONLY` | `true` when served over HTTPS: cookies get the `Secure` flag and HSTS is sent. |
| `DEMO_TRUST_CF_HEADER` | `true` (default): client IPs are read from `CF-Connecting-IP`, which the documented Cloudflare-tunnel deployment guarantees. Set `false` if the port is ever reachable directly. |
| `DEMO_DB_PATH` | SQLite file (default `./data/demo.db`). |
| `DEMO_PORT` | Listen port (default `8090`). |
| `DEMO_LOGIN_RATE_PER_MIN` / `DEMO_CHAT_RATE_PER_MIN` | Demo-side limits: logins per IP, messages per user (defaults 5 / 20). |

Manage demo users where the database and `DEMO_ENCRYPTION_KEY` live:

```bash
python -m app.admin_cli add-user admin --admin   # a demo admin (Settings)
python -m app.admin_cli add-user alice [--password '…']   # a common user
python -m app.admin_cli set-admin alice [--revoke]
python -m app.admin_cli list-users
python -m app.admin_cli reset-password alice
python -m app.admin_cli remove-user alice        # also drops their key + chats
```

### Demo accounts (the test deployment)

The demo's test deployment has `DEMO_ALLOW_REGISTER` off; its accounts come from
`demo-admin add-user` (and `reset-password`). One demo admin and five common users
for testers:

| Username | Password | Role |
|---|---|---|
| `admin` | `Admin@2026!` | demo admin — Settings: the MCip address and the client key |
| `tester1` | `Tester@2026!` | common user |
| `tester2` | `Tester@2026!` | common user |
| `tester3` | `Tester@2026!` | common user |
| `tester4` | `Tester@2026!` | common user |
| `tester5` | `Tester@2026!` | common user |

The database stores only argon2 hashes, never the passwords themselves. Rotate one with
`python -m app.admin_cli reset-password tester1`, delete one with `remove-user`. If this
repository ever becomes public, rotate these first.

## The 10-minute acceptance path

1. **Sign in** as a user you created — on the test deployment, one of the
   `tester1`–`tester5` accounts above. With `DEMO_ALLOW_REGISTER=true` you can register
   in the UI instead.
2. **Connect MCip**: paste the user's `ss_pat_…` chat key. The demo answers with who
   you are connected as, the key's expiry and the workspaces it may use — and never
   echoes the key. A wrong paste (e.g. a full-access key) shows the exact
   `errorCode` and the fix next to it.
3. **Choose a workspace** from the key's list — the sidebar dropdown switches later
   (each workspace keeps its own conversations), and **Refresh** re-reads the list
   from MCip.
4. **Ask a question.** The answer streams in as markdown; status lines appear while the
   agent works; citations from your MCip documents are listed under the answer.
5. **Send a second question** in the same chat, then start a **New chat** — the sidebar
   lists local conversations; opening an older one reloads its transcript *from MCip*
   (use "Load older messages" for pagination).
6. **Watch a retry.** Send two messages on the same conversation at once (e.g. two
   tabs): the second gets `CONVERSATION_BUSY`, shows "retrying in Ns (attempt k/4)",
   then either continues or offers Retry. Stop a running answer with **Stop** and use
   **Retry** — the same `client_request_id` is reused, so a finished turn comes back as
   a replay instead of costing credits again.
7. **Delete a conversation** — it is deleted in MCip too.
8. **Disconnect** (or Sign out and back in): the stored key ciphertext is gone; the key
   itself stays valid in MCip until the user deletes it there.

## Which file shows what

| File | Read it for | Guide |
|---|---|---|
| `app/mcip.py` | The `/ext` HTTP client: request shapes, SSE parsing, `Retry-After` / `retry_after_ms` rules, the replay/content-type branch. Adapted from the example client in the guide. | §3–§5, §8 |
| `app/relay.py` | The retry loop and the browser protocol: retry notices, `ui`/`advice` on errors, truncation → `STREAM_TRUNCATED`, what is retried and what is not. | §5, §7, §8 |
| `app/errors.py` | The `errorCode` → `ui` state + `advice` table the whole UI switches on. | §6 |
| `app/main.py` | The demo's own API: session/CSRF, connect flow (`GET /me`), workspace, chat SSE route, transcripts, delete; CSP and rate limits. | §4, §7, §11 |
| `app/store.py` | SQLite + Fernet: how keys are stored, the 16-char prefix rule, log redaction. | §11 |
| `app/config.py` | `.env` / environment loading. | — |
| `app/admin_cli.py` | `demo-admin` user management. | — |
| `static/app.js` / `static/chat.js` | The GUI: session boot, API calls, the chat state machine (SSE parsing, retry UI, replay notice). | — |
| `tests/fake_mcip.py` | A scriptable fake MCip (ASGI app): SSE scripts, error queues, idempotent replay — no network needed. | — |
| `tests/` | The suite: relay semantics, error table, key storage, CSRF/API, admin CLI, workspace scoping (plus opt-in browser checks). | — |
| `docs/integration-guide.md` | The full API guide this demo implements, in-repo. | all |
| `docs/deploy-cloudflare-tunnel.md` | Runbook for exposing the demo on a hostname via Cloudflare Tunnel + Access. | §7 |

`app/mcip.py` and `app/relay.py` are deliberately small and dependency-light — they are
the parts to copy into your own caller backend.

## Security notes

- **Keys are passwords.** They go in through one route, are encrypted at rest, are used
  only server-side as the outgoing `Authorization` header, and never appear in any
  response, URL or log line (a logging filter redacts `ss_pat_…` patterns; the UI and
  logs use the 16-character prefix).
- **The browser is treated as hostile**: HttpOnly `SameSite=Lax` signed session cookie,
  CSRF double-submit token on every `POST`/`PUT`/`DELETE`, strict
  `Content-Security-Policy` (`default-src 'self'`, no inline code), `no-referrer`,
  `nosniff`, and vendored JS with recorded hashes (`static/vendor/README.md`).
- **Limits**: demo-side login 5/min per IP (Cloudflare-aware via `CF-Connecting-IP`
  when `DEMO_TRUST_CF_HEADER` is on) and 20 messages/min per user, counted in-process
  (single worker); MCip's own per-key/client limits still apply and surface as
  `RATE_LIMITED` / `CLIENT_RATE_LIMITED` with their countdowns.
- **Scope**: this is a demo — SQLite, a single process, in-memory rate limiter. For
  anything internet-facing run it behind an identity-aware proxy; see
  [`docs/deploy-cloudflare-tunnel.md`](docs/deploy-cloudflare-tunnel.md). Report
  vulnerabilities as described in [`SECURITY.md`](SECURITY.md).

## Tests

```bash
uv run pytest                 # the suite, no network
uv run ruff check .           # lint
```

The suite runs the demo app and MCip (a scriptable fake, `tests/fake_mcip.py`) against
each other over in-process ASGI transports — SSE replay semantics, retry budgets, the
error table, Fernet storage, CSRF, the CLI, workspace scoping.

The workspace switcher is also verified in a real browser (opt-in — Playwright and
its Chromium build are not part of the default dev environment):

```bash
uv sync --group browser
uv run playwright install chromium
uv run pytest -m browser -v   # switching, refresh, streaming lock, a11y
```

Those tests run the demo app on a loopback port, seed the session cookie, and drive
headless Chromium against it; MCip stays the in-process fake, so nothing touches the
network. A live check against a real deployment is opt-in too:

```bash
MCIP_SMOKE_BASE_URL=https://mcip.example.com \
MCIP_SMOKE_KEY=ss_pat_… MCIP_SMOKE_WORKSPACE_ID=12 \
uv run pytest -m smoke -v
```

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
