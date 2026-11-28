<h1 align="center">agents-relay</h1>

<p align="center">
  <a href="https://github.com/Lolaplex/agents-relay/releases"><img src="https://img.shields.io/badge/version-0.2.0-blue.svg?style=flat-square" alt="Version 0.2.0"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-relay/"><img src="https://img.shields.io/pypi/v/agents-relay.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <strong>Stdlib HTTP, Telegram, and Discord relay in front of agents-harness runner.loop.</strong><br>
  One subprocess per turn. Trailer parsing for session metadata. No identity store, no traces — state stays in the workspace relay dir.
  Approval decisions and downloaded files live in the workspace relay dir (<code>AGENTS_RELAY_DIR</code>, default <code>&lt;ws&gt;/relay</code>).
</p>

---

## Quickstart

```bash
pip install agents-relay
# Discord gateway support needs the optional websocket dependency:
pip install 'agents-relay[discord]'
```

Needs `runner.loop` on `PATH` (`LOOP_CMD`, default `python -m runner.loop`). That comes from [agents-harness](https://github.com/Lolaplex/agents-harness) 0.1.0 or newer (approvals, `--detached-session`, and the `{user}` approval substitution need it).

> [!TIP]
> **🤖 Agent-Driven Setup:**
> Give your coding agent **this repo** (clone or URL), then tell it to **"install agents-relay and configure it to relay between your chat interface and agents-harness."**

---

## Architecture

| Component | Responsibility |
| :--- | :--- |
| **HTTP Adapter** | Serves `/v1/turn` and `/v1/inject` endpoints with secret header validation |
| **Telegram Adapter** | Long-poll bot updates with real-time thinking status edits, parallel jobs, and Approve/Deny callbacks |
| **Discord Adapter** | Real bot user on the Gateway WebSocket with mention/reply/DM routing, parallel jobs, and Approve/Deny buttons |
| **Loop Client** | Spawns buffered `runner.loop` subprocess per turn with trailer parsing and repeatable `--attach` |
| **Outbound Send** | Direct Telegram/Discord push via `agents-relay send` / `agents-relay discord-send` for reminders and notifications |
| **Approval Gate** | `agents-relay approve` asks an allowlisted chat and blocks on a decision file |
| **Web Surface** | Cookie login, static assets from `RELAY_STATIC_DIR`, `/v1/chat/stream` with status + deltas, `/v1/chat/stop` |

- **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM logic.
- **No harness state**: No identity storage, no traces. Relay files (approvals, inbox) stay in `<ws>/relay` (`AGENTS_RELAY_DIR`).
- **Streaming Status**: Throttled progress streaming and real-time status edits during long-running tool rounds.
- **Zero Bloat**: Python standard library (`http.server`, `urllib.request`, `subprocess`) plus `agents-workspace` for the Host/Workspace path contract. The Discord extra adds only `websocket-client` for the persistent Gateway socket.

---

## Capabilities & Roadmap

### Core Capabilities (Implemented)

- [x] **HTTP Turn Endpoints (`/v1/turn`, `/v1/inject`)**: Buffered turn execution with secret header validation.
- [x] **Telegram Long-Poll Gateway**: Bot API updates with real-time thinking status message editing.
- [x] **Discord Gateway Adapter**: Real bot user with Guild/DM/message-content intents, mention/reply/DM routing, thinking message cleanup, 2000-character Markdown splitting, parallel jobs, and interrupt-and-redirect.
- [x] **Direct Outbound CLI (`send`, `inject`, `approve`)**: Host-initiated push messages, simulated turns, and Telegram/Discord Approve/Deny for mutating tools.
- [x] **Parallel chat jobs**: Updates are dispatched off the adapter thread. Per-chat cap (default 3) with a queue. `/jobs`, `/stop`, `/stop <id>` kill the loop process group.
- [x] **Long replies**: Split into several Telegram messages at paragraph boundaries with balanced HTML, or Discord messages with balanced Markdown code fences. No silent limit cut.
- [x] **Attachments**: Telegram photos/documents, Discord attachments, and `/v1/turn` `attachments` are passed to the loop as `--attach`.
- [x] **Mobile Formatting**: Automatic Markdown table conversion to Telegram-readable bullet lists.
- [x] **Trailer Parsing**: Extracts session, user ID, alias, and return code from harness stdout trailers.
- [x] **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM state.
- [x] **Zero Bloat Runtime**: Python standard library + `agents-workspace`; optional extras stay lazy (`websocket-client` only when Discord is enabled).
- [x] **SSE Token Streaming (`/v1/stream`)**: Server-sent events (`delta` + `trailer`) for client web UIs.
- [x] **Web Surface**: Cookie login, static assets from `RELAY_STATIC_DIR`, streamed status + answer, stop button. The UI itself lives in the Klanker repo, not here.
- [x] **A2A Board Mailbox Proxy (`/v1/a2a/mail`, `GET /v1/a2a/peers`)**: Peer-to-peer agent relay for board instances.
- [x] **Inbound Webhook Hub (`/v1/webhook/{source}`)**: Generic structured ingress for CI/CD and deployment alerts.
- [x] **Client SDK (`agents_relay.client.GatewayClient`)**: `turn()`, `stream()`, `health()` with prefs persistence.
- [x] **Hub channel & device pairing**: paired devices hold scoped bearer credentials (`host`/`client`/`ws_member` roles + ws allowlists); per-device SSE downlink (`/v1/hub/stream`), batched uplink (`/v1/hub/send`), replay/resume, device-bound `rpc-req`/`rpc-res`, and ws-share control frames (`ws:announce`/`ws:revoke`).
- [x] **Session run-lock leases**: one live run per workspace+session across devices (TTL + fencing tokens, `/v1/hub/lease` + `/v1/hub/leases`).
- [x] **Presence, hosts & live links**: `GET /v1/workspaces/<ws>/hosts` host-picker surface, ws-scoped invites (`/v1/ws/invite`), hub links (`/v1/hub/connect`, `/v1/hub/links`), session host switching, and the `/v1/events` SSE bus.
- [x] **Web approvals**: `GET /v1/approvals` list + `POST /v1/approvals/<id>` decide + `approval.changed` SSE; inside turns, `POST /v1/approvals/request` + `GET /v1/approvals/<id>/wait` run on a per-turn `AGENTS_APPROVAL_TOKEN` (no relay secret needed) and mirror to connected devices.
- [x] **Web Push**: VAPID key management (`/v1/push/vapid-key`, alias `/v1/push/key`), subscriptions (`/v1/push/subscribe`), and `/v1/push/send` (optional `pywebpush`, otherwise native `cryptography` RFC 8291/8292 delivery).

### Planned (Roadmap)

- [ ] **Web inbox**: mirror `send` into a browser-visible inbox (`/v1/inbox`).
- [ ] **Server history**: `/v1/history?session=` from agents-traces instead of browser-only storage.
- [ ] **Multi-Channel Adapters**: Signal and additional gateway adapters alongside Telegram and Discord.

---

## Commands

Machine-readable catalog: `python -m agents_relay --help-json` (do not scrape `--help`).

| Command | Purpose |
| :--- | :--- |
| `agents-relay serve` | HTTP `/v1/turn`, `/v1/inject` (and `/v1/alert`, `/webhook/alert`). Telegram long-poll if `TELEGRAM_BOT_TOKEN` is set; Discord Gateway if `DISCORD_BOT_TOKEN` is set |
| `agents-relay serve --no-telegram --no-discord` | HTTP only |
| `agents-relay send --user <chat_id> --text "..."` | One outbound Telegram message (`--chat-id` alias). Allowlist + token from env |
| `agents-relay inject --user <chat_id> --text "..."` | Same inbound handler as poll (thinking edits + final). JSON on stdout. Allowlist + token from env |
| `agents-relay discord-send --user <channel_id> --text "..."` | One outbound Discord channel message (`--chat-id` alias). Allowlist + token from env |
| `agents-relay discord-inject --user <channel_id> --text "..."` | Same inbound handler as the Discord Gateway. JSON on stdout. Allowlist + token from env |
| `agents-relay approve --user <chat_id> --timeout 300` | Read a tool-approval JSON object on stdin. Send Approve/Deny. Exit 0 approved, 1 denied, 2 timeout or unavailable. One JSON line on stdout: `{"note":"..."}`. Use `--telegram` or `--discord` when both adapters are configured and the numeric target is ambiguous |

Telegram and Discord chats can send `/jobs`, `/stop`, and `/stop <id>` while `serve` is running their adapter. `/stop` kills that chat's `runner.loop` process group (the loop and child tools that stayed in the group). Discord also supports `/persona`, `/workspace`, and `/model` with the same per-chat preference store semantics as Telegram.

---

## Environment

| Variable | Description |
| :--- | :--- |
| `LOOP_CMD` | Loop argv (default `python -m runner.loop`) |
| `LOOP_PROVIDER` | Provider name passed through (default `echo`) |
| `RELAY_SECRET` | Shared secret (`GATEWAY_SECRET` fallback). Empty = no auth |
| `TELEGRAM_BOT_TOKEN` | Enables poll + `send` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated numeric IDs. Empty refuses the Telegram adapter unless `AGENTS_RELAY_ALLOW_ANYONE=1` |
| `DISCORD_BOT_TOKEN` | Enables the Discord Gateway adapter and `discord-send` / `discord-inject` |
| `DISCORD_ALLOWED_CHANNEL_IDS` | Comma-separated numeric Discord channel IDs (DM channel IDs or guild text/thread IDs). Empty refuses the Discord adapter unless `AGENTS_RELAY_ALLOW_ANYONE=1` |
| `DISCORD_ALLOWED_USER_IDS` | Comma-separated numeric Discord user IDs allowed to run turns and press approval buttons. Empty refuses the Discord adapter unless `AGENTS_RELAY_ALLOW_ANYONE=1`; unlike Telegram chat IDs, Discord channel IDs do not identify a single user |
| `DISCORD_REQUIRE_MENTION` | `1` means guild messages must mention the bot or reply to it. DMs always count as addressed. Default `0` mirrors Telegram: every message in an allowlisted channel is ingress |
| `DISCORD_APPROVER` | Discord channel id used by `approve --discord` when `--user` is missing or not numeric. Falls back to `AGENTS_RELAY_APPROVER`, then a sole `DISCORD_ALLOWED_CHANNEL_IDS` entry |
| `AGENTS_RELAY_ALLOW_ANYONE` | `1` opts in to empty adapter allowlists. Logs a loud warning. Anyone who can message the bot can run turns and approve tools |
| `AGENTS_RELAY_DIR` | Relay files (default `<ws>/relay`, resolved per workspace; legacy alias `AGENTS_RELAY_STATE`). Approvals: `$AGENTS_RELAY_DIR/approvals/<id>.json`. Inbox: `.../inbox/` |
| `AGENTS_RELAY_APPROVER` | Chat id used by `approve` when `--user` is missing or not numeric (HTTP turns often pass `anonymous`) |
| `AGENTS_RELAY_ATTACH_DIR` | Only local `/v1/turn` attachment paths under this directory are accepted (default: the relay state dir) |
| `AGENTS_RELAY_VISION` | `auto` (default), `on`, or `off`. `auto` uses `AGENTS_VISION` if set, otherwise the provider manifest's `vision` / `capabilities`. `on` and `off` are passed to the child as `AGENTS_VISION` |
| `AGENTS_RELAY_MAX_JOBS` | Global cap on parallel chat turns (default 8) |
| `AGENTS_RELAY_MAX_JOBS_PER_CHAT` | Parallel turns per chat before extra messages queue (default 3, queue cap 20) |
| `AGENTS_RELAY_WAIT_TEXT` | Telegram placeholder while a turn runs (default `One moment …`). Status and fallback lines are English |
| `RELAY_HOST` / `RELAY_PORT` | Bind (default `127.0.0.1:8787`; `GATEWAY_HOST` / `GATEWAY_PORT` fallbacks) |
| `AGENTS_RELAY_WEB_DIR` | Static dir for `GET /app/` (default: bundled `agents_relay/web/` herd PWA) |
| `TELEGRAM_POLL_TIMEOUT` | Long-poll seconds, clamped 1–50 (default 50) |
| `RELAY_WEB_TOKEN` | Enables cookie auth and the web surface. Empty = no web surface |
| `RELAY_WEB_USER` | Harness `--user` for web turns (default `web`) |
| `RELAY_STATIC_DIR` | Directory served at `/` (the dashboard build). Empty = no static assets |
| `RELAY_WEB_SECURE` | `0` drops the `Secure` cookie flag for plain-HTTP local testing (default on) |

`POST /v1/turn` accepts `attachments`: a list of `{"path": "/abs/file", "mime": "image/png"}`, `{"url": "https://...", "mime": "..."}`, or `{"data": "<base64>", "mime": "...", "name": "shot.png"}`. A local `path` must already exist and must resolve inside `AGENTS_RELAY_ATTACH_DIR` (default: the relay state dir). Paths outside that directory are rejected with HTTP 400. `url` must be `http` or `https`. Downloads, inline data, Telegram files, and Discord files are capped at 20 MB. When `mime` is set, the saved file extension matches it (`image/jpeg` → `.jpg`) so the harness can infer the type. A file that fails to land is named in the reply.

Each accepted file is copied into the session workdir at `<workdir>/.relay-inbox/<generated-name>` and that copy is the `--attach` path. The copy is a regular file under that directory only: generated name, no symlink follow, same 20 MB cap. The directory's `.gitignore` is `*`, so a `git add` in the workdir does not commit the copies. Workdir file tools can still read them. The relay inbox keeps the original. With no session workdir, the inbox directory is added to the child `AGENTS_WORKDIR_ALLOW` instead. Photos and documents from Telegram and attachments from Discord take the same path. The loop `--user` for a Telegram or Discord turn is the numeric chat/channel id so `AGENTS_APPROVAL_CMD` can substitute `{user}`.

Image parts are decided by the harness from `AGENTS_VISION` and the provider manifest (`vision: true` or `capabilities` containing `vision`). `AGENTS_RELAY_VISION=on|off` forces that flag for the child. When an image arrives and vision is off or the manifest does not claim it, the reply says the image was not shown and lists the path. A per-model catalog (for example which OpenRouter ids accept images) belongs on the provider manifest or in `AGENTS_VISION`, not in this package.

`approve --user` may be a non-numeric turn id (HTTP defaults to `anonymous`). Then the prompt goes to `AGENTS_RELAY_APPROVER`, or to the only entry in the selected adapter's allowlist when that list has exactly one target. Otherwise approve exits 2 and prints `{"note":"..."}`. A numeric `--user` is used as given. When both Telegram and Discord are configured, a numeric id present in exactly one adapter allowlist selects that adapter; use `--telegram` or `--discord` for an overlap or non-obvious target.

`approve` and `serve` must share `AGENTS_RELAY_DIR` on the same host. The Telegram poll loop answers `callback_query`; the Discord gateway answers button interactions. Only allowlisted user ids can press Approve or Deny. In a private Telegram chat that id is the chat id. In a Telegram group, list both the group chat id and the approving user id. Discord always uses `DISCORD_ALLOWED_USER_IDS` for the pressing user independently of the target channel unless `AGENTS_RELAY_ALLOW_ANYONE=1`.

### Discord bot setup

Discord does not push ordinary guild messages over HTTP webhooks, so the adapter keeps one persistent Gateway WebSocket connection. Create one Discord application/bot, enable the privileged **Message Content Intent**, invite the bot with permission to read/send messages in the target channels, then set:

```bash
pip install 'agents-relay[discord]'
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_CHANNEL_IDS=123456789012345678
DISCORD_ALLOWED_USER_IDS=234567890123456789
DISCORD_REQUIRE_MENTION=1   # recommended for shared guild channels
agents-relay serve
```

The adapter requests Guilds, Guild Messages, Direct Messages, and Message Content intents. In guild channels it responds to every message when `DISCORD_REQUIRE_MENTION=0`; with `1` it responds to direct mentions and replies to the bot. DMs are always addressed. Replies land as normal bot-user messages, not webhook pseudo-users, so the bot has real Discord presence and mention autocomplete as one identity.

HTTP secret headers supported: `X-Relay-Secret`, `Relay-Secret`, `X-Gateway-Secret`, `Gateway-Secret`; `Authorization: Bearer` works too. The relay secret is never read from query params (the one query-token exception is `GET /?token=<web_token>`, which mints the HttpOnly cookie and redirects — see [Web surface](#web-surface)). Paired-device bearer tokens (scoped, revocable via `DELETE /v1/hub/devices/<id>`) and the web `relay_web` cookie authorize the same agent-facing surface, while credential minting/management and admin routes stay operator-only.

---

## HTTP API

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Liveness (`{"ok": true}`) |
| `/` | GET | `index.html` from `RELAY_STATIC_DIR`, else `{"ok": true}` |
| `/app/` | GET | Static herd PWA (installable chat client, same-origin with `/v1/*`) |
| `/v1/auth/login` | POST | `{"token": "..."}` → HttpOnly `relay_web` cookie (30 days) |
| `/v1/auth/logout` | POST | Clears the cookie |
| `/v1/auth/status` | GET | `{"authenticated": bool, "user": "..."}` |
| `/v1/chat/stream` | POST | SSE for a browser client: `start`, `status`, `delta`, `trailer`, `error` |
| `/v1/chat/stop` | POST | `{"run": "..."}` kills that turn's process tree |
| `/v1/turn` | POST | Buffered turn JSON |
| `/v1/stream` | POST | SSE token stream (`delta` + `trailer` events) |
| `/v1/inject` | POST | Inject a turn into the Telegram chat flow (`?async=1` queues a job) |
| `/v1/alert`, `/webhook/alert` | POST | Alert ingress, forces notify |
| `/v1/webhook/{source}` | POST | Generic webhook ingress (`X-Webhook-Secret` header) |
| `/v1/a2a/mail` | POST | Board mailbox proxy |
| `/v1/a2a/peers` | GET | Board peers (`?project=slug`) |
| `/v1/pair/create`, `/v1/pair/redeem` | POST | Ephemeral single-use device pair codes (modes `web`/`remote`/`ws`) |
| `/v1/ws/invite` | POST | Mint a ws-scoped invite code (device-token auth + owner check) |
| `/v1/hub/stream` | GET | Per-device SSE downlink (`?since=` resume) |
| `/v1/hub/send` | POST | Batched upstream envelopes |
| `/v1/hub/devices`, `/v1/hub/devices/<id>` | GET, DELETE | List / revoke paired devices |
| `/v1/hub/lease`, `/v1/hub/leases` | POST, GET | Session run-lock acquire/renew/release + snapshot (one live run per ws+session) |
| `/v1/hub/connect`, `/v1/hub/links`, `/v1/hub/links/<id>` | POST, GET, DELETE | Live hub-link management (admin-only, klanker hooks) |
| `/v1/workspaces/<ws>/hosts` | GET | Member presence + active leases for the host picker |
| `/v1/workspaces/<ws>/sessions/<sid>/host` | POST | Switch a session's host (`session_host_switch_fn` hook) |
| `/v1/events` | GET | SSE bus (`?workspace=` filter): device/ws/lease/session/workspace changes |
| `/v1/approvals` | GET | List host approval records (operator-only) |
| `/v1/approvals/<id>` | POST | Decide a pending approval `{"allow": true|false}` |
| `/v1/approvals/request`, `/v1/approvals/<id>/wait` | POST, GET | Turn-token approval round-trip (mirrored across devices via `approval-req`/`approval-res` frames) |
| `/v1/remote/<id>/<path>` | any | Authenticated proxy into a paired device's local API (`/v1/remotes` manages mounts) |

### Web surface

The relay does not ship a UI. It serves whatever directory `RELAY_STATIC_DIR` points at, so a dashboard build can be mounted on the same origin as the API and needs no CORS.

- `RELAY_WEB_TOKEN` enables cookie auth. Login exchanges the token for an HttpOnly `relay_web` cookie, so the token never lands in JS storage and `EventSource` works.
- `/v1/chat/stream` interleaves stderr progress (`status`, humanized) with stdout answer tokens (`delta`), then a `trailer` with the session id.
- `/v1/chat/stop` kills the turn's process tree by `run` id.
- The client keeps the thread and session id; the relay stays stateless.
- With `RELAY_WEB_TOKEN` set and `RELAY_SECRET` empty, the cookie is the only gate on every endpoint. Set `RELAY_SECRET` too if CLI clients need header auth.

```bash
RELAY_WEB_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
RELAY_STATIC_DIR=/srv/klanker-dashboard
agents-relay serve
# open http://127.0.0.1:8787/ and paste the token
```

### POST /v1/turn

```json
{
  "channel": "overlay",
  "user": "user",
  "text": "hello",
  "session": "ses_…",
  "user_id": "u_…",
  "new_session": false,
  "provider": "openai.default",
  "persona": "default",
  "project": "demo"
}
```

`text` or `message` carries the prompt. `provider`/`persona`/`project` are
passed through to `runner.loop` when set (otherwise `LOOP_PROVIDER` applies).
`attachments` accepts local paths under `AGENTS_RELAY_ATTACH_DIR`, `http(s)`
URLs, or base64 `data`. A copy is placed in the session workdir
`.relay-inbox` so file tools can read it. Response: `{ "reply", "session", "user_id", "alias", "returncode",
"notified" }` — HTTP 200, or 502 when `returncode != 0`.

### POST /v1/stream

Same body as `/v1/turn`. SSE events: `{"type":"delta","text":…}` then
`{"type":"trailer","session","user_id","alias","returncode"}`.

### Client SDK

```python
from agents_relay.client import GatewayClient

client = GatewayClient(base_url="http://127.0.0.1:8787", secret="…")
client.health()
client.turn("hello")
for event in client.stream("hello"):
    ...
```

Or via CLI: `python -m agents_relay client turn "hello" --url http://127.0.0.1:8787 --secret dev` (`client health` also available).

### A2A / board env

| Variable | Default | Notes |
|----------|---------|-------|
| `WEBHOOK_SECRET` | | `X-Webhook-Secret` for `/v1/webhook/*` |
| `BOARD_URL` | | agents-board base for A2A routes |
| `BOARD_PROJECT` | | Default project slug |
| `BOARD_KEY_SLUG` | `gateway` | agents-keys slug for board auth |

## Client config layout

Human `client-*` apps store **UI prefs only** under **`<host>/clients/`** (`AGENTS_HOST_DIR`, default `~/.agents`):

| Path | App |
|------|-----|
| `clients/host/profiles.json` | client-host |
| `clients/overlay/config.json` | client-overlay |

**Sessions** are not client files. Alias bindings live in `identity.json`; conversation bodies and thread resume live in `traces/`. Clients pass `channel`, `user`, and `project`; the harness resolves the latest `ses_…` via `TraceStore.find_latest_session`.

Deprecated `~/.client/` is migrated on first access (host + overlay only).

---

## Constraints

- Python 3.10+, standard library + `agents-workspace` only.
- Does not import harness internals. Strictly spawns `LOOP_CMD` per turn.
- Klanker / host agents own identity and traces. This process is strictly I/O.

---

## Tests

```bash
pytest
```

or via standard library:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

---

## License

MIT. See [LICENSE](LICENSE).
