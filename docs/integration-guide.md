# Guide: External Chat API Integration

> For developers of an external system (the **caller**) that shows its own chat GUI and forwards its users' questions to MCip.
> Contract version: v1 (`/api/v1/ext`, OpenAPI `1.0.0`). A runnable reference of every rule below ships with this demo — start from its [`README`](../README.md), whose client is [`app/mcip.py`](../app/mcip.py).

## 1. How it works

Every one of your users has a real MCip account and their **own** API key. You send each question with that user's key, and MCip answers **as that user**: their workspaces, document access, Agent permissions and credits all apply, as they would in the MCip web UI.

```
Your user ──► Your GUI ──► Your backend ── looks up this user's MCip key
                               │
                               ▼  POST /api/v1/ext/chat   Authorization: Bearer ss_pat_…
                           MCip External Chat API ──► answer (SSE stream or JSON)
```

## 2. Onboarding

1. **An admin registers your system as an API client.** A system admin does this at **Admin → API clients** (`/admin/api-clients`), or an organization owner does it at **Organization settings → API clients** (`/org/<slug>/settings/api-clients`). The admin can restrict the client to some workspaces and IP ranges, and sets its limits. Ask the admin for the client's **name**; your users will pick it.
2. **A workspace admin turns on "API key access"** in the workspace settings of every workspace your users will chat in. It is off by default; without it you get `403 API_ACCESS_DISABLED`.
3. **Each user creates a key** at `/user-settings/api-key` → **Create API key**:
   - **Use for:** `External system: <your client name>`. This makes a *chat* key: it only works on `/api/v1/ext/*`, and only for your client.
   - **Expires in days (required):** default 90, maximum 365 (or less if the deployment sets `PAT_MAX_EXPIRY_DAYS`).
   - The key (`ss_pat_…`) is shown **once**. The user pastes it into your "Connect MCip" setting.
4. **You validate and store it.** Call `GET /api/v1/ext/me` with the key, show the user who they are connected as, let them pick a workspace from `workspaces`, and store the key as described in §11. Store `key.expires_at` too, so you can ask for a new key before it expires.

A full-access key ("MCip API (full access)") does **not** work here: it gets `403 API_KEY_SCOPE`.

## 3. Base URL, OpenAPI and auth

- Base URL: `https://<mcip-host>/api/v1/ext`
- OpenAPI document (public, no auth): `GET /api/v1/ext/openapi.json`; Swagger UI: `GET /api/v1/ext/docs`. They describe only the external routes and every event model.
- Every other request carries exactly one credential header:

```
Authorization: Bearer ss_pat_<…>
```

No cookie, JWT or other identity header is accepted or needed. Every response carries `X-Request-ID`; quote it (or the body's `request_id`) when you report a problem. You may send your own `X-Request-ID`, and MCip will use it.

## 4. Endpoints

### 4.1 `GET /me`: validate a key

```http
GET /api/v1/ext/me
Authorization: Bearer ss_pat_…
```

```json
{
  "user": { "id": "0b9d3c4e-…", "display_name": "Ada Lee", "email": "ada@example.com" },
  "api_client": { "id": 7, "name": "Acme CRM" },
  "key": { "prefix": "ss_pat_AbCdEfGh", "expires_at": "2027-01-01T00:00:00Z" },
  "workspaces": [ { "id": 12, "name": "Support" } ]
}
```

`workspaces` are the ones this key may use: the user is a member, API access is on, the workspace is in the client's allowed list (if the admin set one), and for an organization's client it belongs to that organization. An empty list means nothing is usable yet (usually API access is off).

### 4.2 `POST /chat`: one turn

```http
POST /api/v1/ext/chat
Authorization: Bearer ss_pat_…
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "workspace_id": 12,
  "message": "What is our refund policy for enterprise customers?",
  "conversation_id": null,
  "stream": true,
  "external_user_ref": "crm-user-8841",
  "client_request_id": "5f0c1c8e-0d7a-4f7e-9d1e-2b6b1a2f9d10"
}
```

| Field | Type | Rules |
|---|---|---|
| `workspace_id` | int | Required. One of `GET /me` → `workspaces`. |
| `message` | string | Required, 1–32,000 characters. The whole body is at most 64 KB. |
| `conversation_id` | int \| null | `null` starts a new conversation. Otherwise an id returned by an earlier turn **of the same user in the same workspace**, else `404 CONVERSATION_NOT_FOUND`. |
| `stream` | bool | Default `true` (SSE). `false` returns one JSON body when the turn ends. |
| `external_user_ref` | string \| null | Optional, ≤ 200 chars. Stored with the turn for your correlation; never used for identity. |
| `client_request_id` | string \| null | Optional, 1–100 chars. Idempotency key (§8). Send one per turn. |

A new conversation is a private MCip thread of the user. It shows in their MCip chat history with a "via <client>" badge, and they can continue it in MCip.

**`stream: false` response (200, `application/json`):**

```json
{
  "conversation_id": 5521,
  "turn_id": "5521:1759480000000:3f9a1c",
  "status": "completed",
  "answer": "Enterprise customers can request a refund within 30 days [1].",
  "citations": [
    { "index": 1, "title": "Refund Policy 2026", "snippet": null, "document_id": 3381, "url": null }
  ],
  "rejected_actions": [],
  "usage": {
    "credits_micros": 41000,
    "model": "gpt-4.1",
    "prompt_tokens": 1200,
    "completion_tokens": 80,
    "total_tokens": 1280
  }
}
```

- `conversation_id` is an **integer**; pass it back to continue the conversation. `turn_id` is an opaque string (do not parse it).
- `status` is `completed` or `error`. With `error`, the body also has `"error": {"errorCode", "message", "retryable"}` and `answer` holds whatever was produced before the failure (§6.2).
- `usage.credits_micros` is the cost of the turn in micro-credits (1,000,000 = 1 credit), charged to the workspace's wallet exactly as a UI chat.

**`stream: true` response (200, `text/event-stream`):** see §5.

### 4.3 `GET /conversations/{conversation_id}/messages`: transcript

Rebuilds your GUI from MCip's copy of a conversation. Query: `limit` (1–100, default 50), `before` (a message id; returns older messages).

```http
GET /api/v1/ext/conversations/5521/messages?limit=50
```

```json
{
  "conversation_id": 5521,
  "messages": [
    { "id": 90121, "role": "user", "text": "What is our refund policy…?", "citations": [], "created_at": "2026-10-03T07:20:01Z" },
    { "id": 90122, "role": "assistant", "text": "Enterprise customers can request a refund within 30 days [1].",
      "citations": [ { "index": 1, "title": "Refund Policy 2026", "snippet": null, "document_id": 3381, "url": null } ],
      "created_at": "2026-10-03T07:20:09Z" }
  ],
  "has_more": true,
  "next_before": 90121
}
```

The **newest** page comes first; inside a page, messages are oldest first. While `has_more` is true, pass `next_before` as `before` to get the previous page. Only user and assistant text is returned (no tool calls or reasoning). Turns the user made in MCip's UI on the same thread are included.

### 4.4 `DELETE /conversations/{conversation_id}`

Deletes the conversation and its messages, as deleting it in MCip does. Response `200 {"conversation_id": 5521, "deleted": true}`. Someone else's or an unknown id gets `404 CONVERSATION_NOT_FOUND`.


## 5. The event stream (`stream: true`)

`text/event-stream`; each event is one `data:` line holding one compact JSON object, followed by a blank line. Lines starting with `:` are comments (`: keep-alive`) and must be ignored. There are no `event:` or `id:` SSE fields; the type is the JSON's `event` key.

```
data: {"event":"start","conversation_id":5521,"turn_id":"5521:1759480000000:3f9a1c"}

data: {"event":"status","text":"Searching the knowledge base"}

: keep-alive

data: {"event":"delta","text":"Enterprise customers can request a refund "}

data: {"event":"delta","text":"within 30 days [1]."}

data: {"event":"citation","index":1,"title":"Refund Policy 2026","snippet":null,"document_id":3381,"url":null}

data: {"event":"done","status":"completed","usage":{"credits_micros":41000,"model":"gpt-4.1","prompt_tokens":1200,"completion_tokens":80,"total_tokens":1280}}
```

| `event` | Fields | Notes |
|---|---|---|
| `start` | `conversation_id` (int), `turn_id` (string \| null) | Always the first event. Save `conversation_id`. |
| `delta` | `text` | Answer markdown; append in order. |
| `status` | `text` | Optional progress line (a thinking-step title). Show it transiently or ignore it. |
| `action_rejected` | `tool`, `summary` (string \| null), `reason`, `message` | An action was skipped (§9). The stream continues. |
| `citation` | `index`, `title`, `snippet`, `document_id`, `url` | Sent **after** the text, once per `[n]` the answer uses (§10). |
| `error` | `errorCode`, `message`, `retryable` | The turn failed after it started (§6.2). Always followed by `done`. |
| `done` | `status` (`completed` \| `error`), `usage` | Always the last event. |

Ignore unknown event types and unknown fields: new ones may be added within v1. If the connection ends without `done`, treat the turn as failed (retry as in §8).

## 6. Errors

### 6.1 HTTP errors

Every non-2xx `/ext` response has the same JSON body:

```json
{ "errorCode": "API_KEY_EXPIRED", "message": "The API key has expired.", "request_id": "req_0123456789ab" }
```

Some add fields: `retry_after_ms` (429, 409 busy) and `continue_url` (409 awaiting approval). `message` is safe to show to an end user. Branch on `errorCode`, never on `message`.

| Status | `errorCode` | Meaning | What the caller should do |
|---|---|---|---|
| 400 | `PROMPT_REFUSED` | The prompt-injection guard refused the message (JSON mode; in stream mode it is an `error` event). | Show `message`; let the user rephrase. Don't retry as is. |
| 401 | `API_KEY_MISSING` | No `Authorization: Bearer ss_pat_…` header. | Fix the integration. |
| 401 | `API_KEY_INVALID` | Unknown, revoked or deleted key (also: its API client was deleted). | Mark the user disconnected; ask them to reconnect with a new key. |
| 401 | `API_KEY_EXPIRED` | The key passed `expires_at`. | Ask the user to create a new key. |
| 401 | `API_USER_INACTIVE` | The MCip user is deactivated. | Stop using the key; disconnect the user. |
| 403 | `API_KEY_SCOPE` | Not a chat key (e.g. a full-access key). | Ask the user for an "External system" key. |
| 403 | `API_CLIENT_DISABLED` | An admin disabled your API client (kill switch). | Stop sending; contact the MCip admin. Affects every user. |
| 403 | `API_CLIENT_NOT_ALLOWED` | The key's user left the organization that owns your client. | Disconnect the user; contact the MCip admin if unexpected. |
| 403 | `API_CLIENT_IP_DENIED` | Your egress IP is not on the client's allowlist. | Give the MCip admin your egress IPs. |
| 403 | `API_ACCESS_DISABLED` | The workspace has "API key access" off. | Ask a workspace admin to turn it on. |
| 403 | `WORKSPACE_FORBIDDEN` | Not a member, not an allowed workspace for the client, another org's workspace, or the workspace doesn't exist. | Re-read `GET /me` and let the user pick again. |
| 403 | `FORBIDDEN` | The user's workspace role lacks the chat permission (create/read/delete chats). | Ask a workspace admin to change the role. |
| 404 | `CONVERSATION_NOT_FOUND` | Unknown conversation, another user's, or another workspace's. | Start a new conversation (`conversation_id: null`). |
| 409 | `CONVERSATION_BUSY` | A turn is still running on this conversation, or a request with the same `client_request_id` is still running. | Wait `retry_after_ms` (or `Retry-After`) and retry with the same `client_request_id`. |
| 409 | `CONVERSATION_AWAITING_APPROVAL` | The conversation has an approval pending from MCip's UI. | Show `message` and a link to `continue_url`; the user approves or rejects in MCip. Don't retry until then. |
| 413 | `REQUEST_TOO_LARGE` | Body over 64 KB. | Shorten the message. The server closes the connection. |
| 422 | `VALIDATION_ERROR` | A field is missing or out of range; `message` names it. | Fix the request. |
| 429 | `RATE_LIMITED` | Per-key limit (requests/min or concurrent turns), or the deployment-wide per-IP limit. | Wait `Retry-After`, then retry (§8). |
| 429 | `CLIENT_RATE_LIMITED` | Your API client's limit (all your users together). | Wait `Retry-After`; slow down globally. |
| 500 | `INTERNAL_ERROR` | Unexpected server error. | Retry once with the same `client_request_id`; then report `request_id`. |

Statuses that do not come from MCip's app (for example `502`/`503`/`504` from a proxy, or `503` when the server sheds load) may have a non-JSON body. Treat them as transient: retry with backoff.

### 6.2 Errors inside a turn

Once a turn has started, failures arrive as an `error` event followed by `done` with `status: "error"` (stream), or as `200` with `status: "error"` and `error` (JSON). Any text already produced is kept in `answer`.

| `errorCode` | `retryable` | What to do |
|---|---|---|
| `PREMIUM_QUOTA_EXHAUSTED` | false | The workspace's credits are used up. Tell the user; an MCip admin tops up. |
| `MODEL_RATE_LIMITED` | true | The model provider is throttling. Retry after a few seconds. |
| `MODEL_PROVIDER_UNAVAILABLE` | true | The provider is down or timing out. Retry later. |
| `MODEL_AUTH_FAILED` | false | MCip's model credentials are broken. Report to the MCip admin. |
| `MODEL_NOT_FOUND` | false | The workspace's model is misconfigured. Report to the MCip admin. |
| `MODEL_CONTEXT_LIMIT` | false | The conversation is too long for the model. Start a new conversation. |
| `CONVERSATION_BUSY` | true | Another turn took the conversation. Retry later. |
| `PROMPT_REFUSED` | false | Stream-mode refusal by the prompt guard. Let the user rephrase. |
| `INTERNAL_ERROR` | true | Anything else, including a stream that ended unexpectedly. Retry once. |

In JSON mode two of these become HTTP errors instead: `PROMPT_REFUSED` → `400`, and `CONVERSATION_BUSY` with no answer yet → `409`.

## 7. Streaming vs JSON, and timeouts

**Use streaming.** The user sees text as it's written, and while the agent works silently MCip sends `: keep-alive` after 15 s without output, so idle-timeout proxies don't cut the connection.

`stream: false` keeps the connection silent until the turn ends, which can take minutes (knowledge-base search, tools, slow models). Then:

- set your HTTP client's read timeout to **600 s**;
- every proxy between you and MCip needs an idle/read timeout of at least 600 s. **Cloudflare closes a connection with no bytes for 100 s (error 524)**, so `stream: false` through Cloudflare fails on long turns; use streaming there.

For streaming, set a read timeout well above 15 s (e.g. 60–120 s between bytes) and an overall turn budget of 600 s. Disable response buffering in your own proxies (`X-Accel-Buffering: no` is already sent).

If your user closes the chat, close the connection: MCip stops the turn. A turn you abandon mid-way is not stored for idempotent replay (§8).

## 8. Retries, `Retry-After` and idempotency

**Send a `client_request_id` (e.g. a UUID) with every turn, and reuse it on every retry of that turn.** MCip keeps it for **10 minutes**, per key and conversation (for a new conversation, per workspace):

| State of the first request | A retry with the same id gets |
|---|---|
| Still running | `409 CONVERSATION_BUSY`: wait and retry. |
| Completed | `200` with the stored **JSON** body of that turn, **even if the retry asked for `stream: true`**. Check `Content-Type` (`application/json` vs `text/event-stream`). The turn is not run or charged again. |
| Failed, refused or cut off | Nothing stored: the turn runs again (and costs credits again). |

What to retry:

| Response | Retry? |
|---|---|
| `429 RATE_LIMITED` / `CLIENT_RATE_LIMITED` | Yes, after `Retry-After`. |
| `409 CONVERSATION_BUSY` | Yes, after `retry_after_ms` (or `Retry-After`). |
| `5xx`, network error, stream ended without `done` | Once or twice, with backoff, same `client_request_id`. |
| `error` event with `retryable: true` | Yes, with backoff. Same id is fine (failed turns are not stored). |
| Every other 4xx, `retryable: false` | No. Fix the cause (§6). |

**`Retry-After`:** for `/ext` limits it is whole seconds (rounded up), and the body's `retry_after_ms` (also sent as a `retry-after-ms` header) is the precise value. The deployment-wide per-IP limit also sends whole seconds (older deployments sent e.g. `1 minute`). If the value isn't an integer and there is no `retry_after_ms`, wait 60 s. Add jitter, and cap your attempts (the demo in this repository makes 4).


## 9. Auto-rejected actions and Full access

The agent runs with the user's own **Agent permissions** (MCip → Settings → Agent permissions). When a turn reaches an action that needs a human approval (sending an email, writing to a connector, running an automation, …), the API cannot ask anyone, so MCip **rejects that action itself and the turn continues**: the model is told the action was skipped and answers without it.

Each skipped action is reported (in JSON mode, in `rejected_actions[]`):

```json
{
  "event": "action_rejected",
  "tool": "send_email",
  "summary": "Send email to finance@example.com",
  "reason": "permission_ask",
  "message": "The \"send_email\" action needs approval, so it was skipped. To allow actions like this from external systems, set Approval Mode to Full access in MCip (Settings → Agent permissions)."
}
```

| `reason` | Why | What the user can do |
|---|---|---|
| `permission_ask` | The user's Approval Mode or rules ask for approval. | Set **Approval Mode → Full access** (or allow that tool) in MCip; then the action runs without a prompt. |
| `org_policy` | The organization's agent policy requires approval; the user's setting can't override it. | Ask the organization admin. |
| `subagent` | A subagent or tool asked for approval. | Open the conversation in MCip, redo the action and approve it there. |
| `doom_loop` | The assistant kept repeating the same step, so it stopped. | Rephrase the question. |

**Show `message` to the user** next to the answer; it says why the action was skipped and how to change that. `summary` (may be null) describes the action; tool arguments are never exposed. A turn auto-rejects at most 3 rounds of pauses; after that it ends with the answer so far. The conversation is never left waiting for an approval. (`409 CONVERSATION_AWAITING_APPROVAL` only happens when an approval was started in MCip's UI on the same conversation.)

With **Full access** the user's own rules never pause a turn; organization policies and the repeat guard still can. Actions that do run are attributed to your API client in MCip's agent action log.

## 10. Citations

The answer is markdown and cites sources as `[1]`, `[2]`, …. Each `citation` (or `citations[]` entry) has `index` = the `n` in `[n]`, so you can turn `[n]` into a link or footnote.

- Citations are sent **after** the text, in order of first use, only for the labels this turn's answer uses. Numbers are stable across the conversation: a source cited as `[1]` in turn 1 is still `[1]` in turn 2.
- `title`: the document or page title (may be null).
- `document_id`: set for MCip knowledge-base documents; `url`: set for web pages and documents with an `http(s)` source. Either may be null.
- `snippet` is currently always null; don't rely on it.
- MCip documents need an MCip login to open; link them only for users who have one.
- The transcript endpoint returns the same labels for stored answers.

## 11. Key storage rules

- **Treat keys as passwords.** Store them encrypted at rest, on your backend only. Never send a key to a browser or mobile app, never put it in a URL, and never write it to logs, analytics or error reports. Log at most the first 16 characters (`key.prefix`, what MCip shows).
- **One key per user, one user per key.** MCip treats the key holder as that user (their documents, their credits, their actions). Never share a key between users or use one user's key for another.
- Keys are bound to your API client; they won't work for another system, and they only reach `/api/v1/ext/*`.
- Keys expire (max 365 days). Read `key.expires_at` from `GET /me` and ask the user for a new key before then. On `401` (`API_KEY_INVALID`, `API_KEY_EXPIRED`, `API_USER_INACTIVE`) mark the user disconnected and stop using the key.
- The user can revoke a key at any time (`/user-settings/api-key` → delete); it stops working on the next request.
- If you suspect a leak, tell the user (and the MCip admin) to delete the key at once.

## 12. Limits

| Limit | Value | When exceeded |
|---|---|---|
| `message` | 1–32,000 characters | `422 VALIDATION_ERROR` |
| Request body | 64 KB | `413 REQUEST_TOO_LARGE` |
| `external_user_ref` / `client_request_id` | ≤ 200 / 1–100 characters | `422` |
| Requests per key | 30/min (deployment setting `EXT_API_KEY_RATE_PER_MINUTE`), on every authenticated `/ext` route, `GET /me` included | `429 RATE_LIMITED` |
| Concurrent turns per key | 3 (`EXT_API_KEY_MAX_CONCURRENT`) | `429 RATE_LIMITED` |
| Requests per API client | set by the admin, default 600/min | `429 CLIENT_RATE_LIMITED` |
| Concurrent turns per API client | set by the admin, default 500 | `429 CLIENT_RATE_LIMITED` |
| Turns per conversation | 1 at a time | `409 CONVERSATION_BUSY` |
| Requests per source IP | 1024/min across the deployment | `429 RATE_LIMITED` |
| Transcript page | `limit` ≤ 100 (default 50) | `422` |
| Auto-reject rounds per turn | 3 | The turn ends with the answer so far. |
| Idempotency window | 10 minutes | The id is forgotten. |
| Key lifetime | default 90 days, max 365 (or `PAT_MAX_EXPIRY_DAYS`) | `401 API_KEY_EXPIRED` |

Requests/min limits are token buckets: a burst up to the limit is allowed, then requests refill evenly over the minute. Ask your MCip admin for the actual values of your deployment and client. File attachments in questions and files/charts in answers are not supported in v1.

## 13. Example client and reference integration

The demo this guide ships with is a complete reference integration:

- [`app/mcip.py`](../app/mcip.py) — a compact async client (Python 3.12+, `httpx` only) for §3–§5: validates a key with `GET /me`, opens a turn as an async event iterator, parses SSE and the JSON/replay bodies, and computes `Retry-After` delays exactly as §8 describes.
- [`app/relay.py`](../app/relay.py) — the retry loop (§8) and the browser protocol: retry notices, `error` + `done` framing, `ui`/`advice` per `errorCode` (§6).

Both are deliberately small; copy them into your backend, or use them as the checklist for your own client.

Minimal `curl` stream:

```bash
curl -N https://mcip.example.com/api/v1/ext/chat \
  -H "Authorization: Bearer $MCIP_API_KEY" -H "Content-Type: application/json" \
  -d '{"workspace_id":12,"message":"Hello","conversation_id":null,"stream":true,"client_request_id":"demo-1"}'
```

## 14. FAQ

**Can we use one service key for all our users?** No. Every request must carry the key of the MCip user it acts for; MCip has no service accounts for chat.

**Our user gets `403 API_ACCESS_DISABLED` / an empty `workspaces` list.** A workspace admin must turn on "API key access" in that workspace's settings, and the workspace must be allowed for your API client.

**Why does a retry with `stream: true` return JSON?** The first request with that `client_request_id` completed; you get its stored result (§8). Always branch on `Content-Type`.

**The assistant says it couldn't do something.** Look for `action_rejected`: the action needed approval. The user can switch to Full access in MCip, or redo the action in MCip (§9).

**Can the user continue the conversation in MCip?** Yes. It is a normal private thread in their chat history ("via <client>"). Turns made there show up in the transcript endpoint.

**Where are the credits charged?** Like a UI chat: an organization workspace charges the organization wallet, a personal workspace its owner. `usage.credits_micros` reports the turn's cost.

**What happens if our backend restarts mid-stream?** The turn stops. Retry with the same `client_request_id`: if the turn had completed you get its result; otherwise it runs again.

**Do conversations expire?** They follow MCip's normal thread retention. Delete one with `DELETE /conversations/{id}`.

**How do we see what the API offers?** `GET /api/v1/ext/openapi.json` (no auth).

**Who do we contact for 403 `API_CLIENT_*` or limit changes?** The MCip admin who registered your client; give them the `request_id`.
