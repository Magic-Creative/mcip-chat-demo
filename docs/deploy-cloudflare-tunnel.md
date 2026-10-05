# Runbook: expose the demo via Cloudflare Tunnel

How to put this demo on a hostname without opening any inbound port: the demo
listens on `127.0.0.1:8090`, a Cloudflare Tunnel (`cloudflared`) dials out to
Cloudflare, and Cloudflare Access puts an identity check in front of the whole
site.

Read the security notes first — **the demo holds live MCip API keys for every user
you add**, so treat the hostname as sensitive:

- keep the demo bound to loopback (`docker-compose.yml` already publishes
  `127.0.0.1:8090:8090`; never change that to `0.0.0.0`);
- set `DEMO_HTTPS_ONLY=true` so session cookies are marked `Secure` (and HSTS is
  sent);
- put **Cloudflare Access** in front (below), or an equivalent identity-aware
  proxy, before sharing the URL with anyone;
- the Access application below is deliberately broad (Allow → Everyone with an
  email one-time PIN): it hides the site from scanners, and the demo's own
  account login is the real gate. Give each person their own demo account
  (`demo-admin add-user`); never share logins, and never reuse an MCip key
  across accounts.

## 1. Prepare the demo

### On MCip (once)

- **API client:** an organization owner creates it at **Organization settings →
  API clients** (or a system admin at **Admin → API clients**, choosing the
  organization). Every client belongs to one organization, and its keys can only
  reach that organization's workspaces.
- **What the demo can reach is decided by membership.** API clients have no
  workspace allowlist, so a key reaches every workspace of the client's
  organization where its user has the chat permission. For a public demo, use a
  dedicated organization or workspace (for example `Chat Demo`) with sample
  documents only, and make the demo users members of that workspace only.
- **Demo users** each create their own key at **User Settings → API Keys →
  Create API key**, with **Use for: External system: <your client>**.
- To cut a user off, revoke their key from the client's **Keys** panel; to stop
  the whole demo, disable the client.

### On the host

```bash
cd mcip-chat-demo
cp .env.example .env
# edit .env:
#   MCIP_BASE_URL=https://<your-mcip-host>
#   DEMO_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
#   DEMO_SESSION_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
#   DEMO_HTTPS_ONLY=true
docker compose up -d --build
docker compose exec demo python -m app.admin_cli add-user alice
curl -fsS http://127.0.0.1:8090/healthz   # {"status":"ok"}
```

The SQLite database (users, encrypted keys, conversations) lives in the
`demo-data` volume; `docker compose down` keeps it, `down -v` deletes it.

## 2. Install cloudflared

Follow Cloudflare's install instructions for your OS
(<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>),
then:

```bash
cloudflared --version
cloudflared tunnel login        # opens a browser; pick the zone
```

## 3. Create a tunnel and its DNS route

```bash
cloudflared tunnel create mcip-chat-demo
# prints the tunnel UUID and writes credentials to ~/.cloudflared/<UUID>.json

cloudflared tunnel route dns mcip-chat-demo chatbot-mcip.igsl-group.uk
```

If you were given an existing (shared) tunnel instead, do **not** create a new
one: add a new ingress rule to its config (§4), validate it (§5), and restart
that tunnel — its config is shared property, coordinate the restart.

## 4. Ingress configuration

Create `~/.cloudflared/config.yml` (or edit the shared tunnel's file):

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /home/<user>/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: chatbot-mcip.igsl-group.uk
    service: http://127.0.0.1:8090
  # ...other rules of a shared tunnel stay above / below as they were...
  - service: http_status:404   # must be the last rule
```

Notes:

- `originRequest.noTLSVerify` is **not** needed — the demo speaks plain HTTP on
  loopback.
- Do not enable Cloudflare's caching for this hostname (the app already sends
  `Cache-Control: no-store` on pages and SSE).
- SSE passes through tunnels unmodified; the demo streams with
  `X-Accel-Buffering: no` and forwards MCip's `: keep-alive` comments (sent
  every 15 s of upstream silence) to the browser, so both legs — MCip → demo
  and demo → browser — stay well under Cloudflare's 100 s idle limit.

Validate and run:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel run mcip-chat-demo        # foreground test
```

### On the dev VM (existing Docker tunnel)

The dev host runs the demo on the same VM as dev MCip, behind the **existing**
`cloudflared-dev` Docker container — it uses host networking, its config is
`~/.cloudflared/dev-only-config.yml` and its origin cert
`cert-igsl-group.pem`. There is no systemd unit here; the tunnel is restarted
with `docker`.

1. Insert this rule **above** the existing entries in `dev-only-config.yml`
   (first match wins):

   ```yaml
   ingress:
     - hostname: chatbot-mcip.igsl-group.uk
       service: http://127.0.0.1:8090   # correct: the container is on the host network
     # ...the existing dev-mcintelligentplus rule(s) stay below...
   ```

2. Route DNS through the existing tunnel. `route dns` needs the tunnel **UUID**
   (the `tunnel:` field of `dev-only-config.yml`) and the origin cert:

   ```bash
   docker run --rm -v ~/.cloudflared:/etc/cloudflared cloudflare/cloudflared:latest \
     tunnel --origincert /etc/cloudflared/cert-igsl-group.pem \
     route dns <TUNNEL-UUID> chatbot-mcip.igsl-group.uk
   ```

   (Or add a CNAME `chatbot-mcip` → `<TUNNEL-UUID>.cfargotunnel.com` in the
   Cloudflare dashboard instead.)

3. Validate against the dev config, then restart the container:

   ```bash
   docker run --rm -v ~/.cloudflared:/etc/cloudflared cloudflare/cloudflared:latest \
     tunnel --config /etc/cloudflared/dev-only-config.yml ingress validate
   docker restart cloudflared-dev
   ```

Restarting `cloudflared-dev` briefly drops **dev MCip** too (it shares the
tunnel): do it in a quiet window, and only with approval for that shared host.

## 5. Run it as a service

```bash
sudo cloudflared service install     # uses ~/.cloudflared/config.yml
sudo systemctl status cloudflared
sudo systemctl restart cloudflared   # after any config change
```

## 6. Protect the hostname (Cloudflare Access)

In the Cloudflare dashboard → **Zero Trust → Access → Applications → Add an
application → Self-hosted**:

1. Application domain: `chatbot-mcip.igsl-group.uk` (add both the hostname and
   `chatbot-mcip.igsl-group.uk/*` if prompted).
2. Policy: **Allow** → **Everyone**, with the **One-time PIN** identity
   provider (email) — anyone who can receive an email can reach the page, and
   the demo's own account login is the real gate. Prefer a narrower list?
   Replace *Everyone* with **Emails** → the exact addresses you invited. Keep
   the default (or raise) session duration so people are not re-prompted all day.
3. Identity providers: One-time PIN (email) works with no extra setup; SSO if
   your org has it.

Visitors now pass Cloudflare's email check before they ever reach the demo, and
then sign in to the demo itself — two layers on purpose, with the demo login
doing the actual authorization.

The app's rate limiter reads `CF-Connecting-IP` (Cloudflare always sets it)
because `DEMO_TRUST_CF_HEADER` defaults to `true`. That is only sound while the
demo port is bound to loopback, as above — if you ever publish the port beyond
`127.0.0.1`, set `DEMO_TRUST_CF_HEADER=false` so clients cannot spoof the header.

## 7. Verify end to end

```bash
curl -fsS https://chatbot-mcip.igsl-group.uk/healthz
```

In a browser: sign in, connect a test MCip key, pick a workspace, ask a question
and watch the stream. Then check the hardening actually arrived:

```bash
curl -sI https://chatbot-mcip.igsl-group.uk/ | grep -iE 'content-security-policy|strict-transport'
# set-cookie on /api/session should carry Secure; HttpOnly ...
curl -sI https://chatbot-mcip.igsl-group.uk/api/session | grep -i set-cookie
```

## 8. Updates and operations

- Deploy a new build: `docker compose up -d --build` (the volume and keys
  survive; `DEMO_ENCRYPTION_KEY` must stay the same or every stored connection
  becomes unreadable and is dropped).
- Users: `docker compose exec demo python -m app.admin_cli list-users` /
  `add-user` / `remove-user`.
- Logs: `docker compose logs -f demo` — key material never appears in them (a
  filter redacts `ss_pat_…`), but tokens in URLs should still never be added to
  the app.
- Rotating `DEMO_SESSION_SECRET` signs everyone out. Rotating
  `DEMO_ENCRYPTION_KEY` disconnects everyone (users reconnect with their MCip
  keys).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cloudflare **502** from the hostname | Demo container down or port mismatch: `docker compose ps`, `curl http://127.0.0.1:8090/healthz`, check the ingress `service:` line. |
| Cloudflare **524** on a long answer | Something buffers; confirm the request is streaming (`text/event-stream`) and that no other proxy sits in front adding a 100 s cap. |
| Login works but cookies don't stick | `DEMO_HTTPS_ONLY=false` while serving HTTPS (cookie not `Secure` is fine, but check `https_only` matches the scheme the browser uses), or an Access policy interfering with the session cookie path. |
| Repeated login prompts | Cloudflare Access session duration is very short; lengthen it in the Access application. |
| `429` immediately for everyone | The MCip admin's per-client limits, or the demo's own login limit — check `docker compose logs demo`. |
