<h1 align="center">agents-relay</h1>

<p align="center">
  <a href="https://github.com/Lolaplex/agents-relay/releases"><img src="https://img.shields.io/badge/version-0.1.0-blue.svg?style=flat-square" alt="Version 0.1.0"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-relay/"><img src="https://img.shields.io/pypi/v/agents-relay.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <strong>Stdlib HTTP and Telegram relay in front of agents-harness runner.loop.</strong><br>
  One subprocess per turn. Trailer parsing for session metadata. No identity store, no traces, no writes under <code>~/.agents</code>.
  Approval decisions and downloaded files live in the relay state dir (<code>AGENTS_RELAY_STATE</code>, default <code>~/.agents-relay</code>).
</p>

---

## Quickstart

```bash
pip install agents-relay
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
| **Loop Client** | Spawns buffered `runner.loop` subprocess per turn with trailer parsing and repeatable `--attach` |
| **Outbound Send** | Direct Telegram push via `agents-relay send` for reminders and notifications |
| **Approval Gate** | `agents-relay approve` asks an allowlisted chat and blocks on a decision file |
| **Web Surface** | Cookie login, static assets from `RELAY_STATIC_DIR`, `/v1/chat/stream` with status + deltas, `/v1/chat/stop` |

- **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM logic.
- **No harness state**: No identity storage, no traces, no writes under `~/.agents`. Relay files (approvals, inbox) stay in `AGENTS_RELAY_STATE`.
- **Streaming Status**: Throttled progress streaming and real-time status edits during long-running tool rounds.
- **Zero Bloat**: Pure Python standard library (`http.server`, `urllib.request`, `subprocess`).

---

## Capabilities & Roadmap

### Core Capabilities (Implemented)

- [x] **HTTP Turn Endpoints (`/v1/turn`, `/v1/inject`)**: Buffered turn execution with secret header validation.
- [x] **Telegram Long-Poll Gateway**: Bot API updates with real-time thinking status message editing.
- [x] **Direct Outbound CLI (`send`, `inject`, `approve`)**: Host-initiated push messages, simulated turns, and Telegram Approve/Deny for mutating tools.
- [x] **Parallel Telegram jobs**: Updates are dispatched off the poll thread. Per-chat cap (default 3) with a queue. `/jobs`, `/stop`, `/stop <id>` kill the loop process group.
- [x] **Long replies**: Split into several Telegram messages at paragraph boundaries with balanced HTML. No silent 4096 cut.
- [x] **Attachments**: Telegram photos and documents, and `/v1/turn` `attachments`, are passed to the loop as `--attach`.
- [x] **Mobile Formatting**: Automatic Markdown table conversion to Telegram-readable bullet lists.
- [x] **Trailer Parsing**: Extracts session, user ID, alias, and return code from harness stdout trailers.
- [x] **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM state.
- [x] **Zero Bloat Runtime**: Pure Python standard library only.
- [x] **SSE Token Streaming (`/v1/stream`)**: Server-sent events (`delta` + `trailer`) for client web UIs.
- [x] **Web Surface**: Cookie login, static assets from `RELAY_STATIC_DIR`, streamed status + answer, stop button. The UI itself lives in the Klanker repo, not here.
- [x] **A2A Board Mailbox Proxy (`/v1/a2a/mail`, `GET /v1/a2a/peers`)**: Peer-to-peer agent relay for board instances.
- [x] **Inbound Webhook Hub (`/v1/webhook/{source}`)**: Generic structured ingress for CI/CD and deployment alerts.
- [x] **Client SDK (`agents_relay.client.GatewayClient`)**: `turn()`, `stream()`, `health()` with prefs persistence.

### Planned (Roadmap)

- [ ] **Web approvals**: `/v1/approvals` list + decide + an `approval` SSE event, so a browser client can gate mutating tools without Telegram.
- [ ] **Web inbox + push**: mirror `send` into a web inbox, `/v1/inbox`, Web Push (VAPID) for reminders while the app is closed.
- [ ] **Server history**: `/v1/history?session=` from agents-traces instead of browser-only storage.
- [ ] **Multi-Channel Adapters**: Signal / Discord gateway adapters alongside Telegram.

---

## Commands

Machine-readable catalog: `python -m agents_relay --help-json` (do not scrape `--help`).

| Command | Purpose |
| :--- | :--- |
| `agents-relay serve` | HTTP `/v1/turn`, `/v1/inject` (and `/v1/alert`, `/webhook/alert`). Telegram long-poll if `TELEGRAM_BOT_TOKEN` is set |
| `agents-relay serve --no-telegram` | HTTP only |
| `agents-relay send --user <chat_id> --text "..."` | One outbound Telegram message (`--chat-id` alias). Allowlist + token from env |
| `agents-relay inject --user <chat_id> --text "..."` | Same inbound handler as poll (thinking edits + final). JSON on stdout. Allowlist + token from env |
| `agents-relay approve --user <chat_id> --timeout 300` | Read a tool-approval JSON object on stdin. Send Approve/Deny. Exit 0 approved, 1 denied, 2 timeout or unavailable. One JSON line on stdout: `{"note":"..."}` |

Telegram chats can send `/jobs`, `/stop`, and `/stop <id>` while `serve` is polling. `/stop` kills that chat's `runner.loop` process group (the loop and child tools that stayed in the group).

---

## Environment

| Variable | Description |
| :--- | :--- |
| `LOOP_CMD` | Loop argv (default `python -m runner.loop`) |
| `LOOP_PROVIDER` | Provider name passed through (default `echo`) |
| `RELAY_SECRET` | Shared secret (`GATEWAY_SECRET` fallback). Empty = no auth |
| `TELEGRAM_BOT_TOKEN` | Enables poll + `send` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated numeric IDs. Empty refuses the Telegram adapter unless `AGENTS_RELAY_ALLOW_ANYONE=1` |
| `AGENTS_RELAY_ALLOW_ANYONE` | `1` opts in to an empty allowlist. Logs a loud warning. Anyone who can message the bot can run turns and approve tools |
| `AGENTS_RELAY_STATE` | Relay files (default `~/.agents-relay`). Approvals: `$AGENTS_RELAY_STATE/approvals/<id>.json`. Inbox: `.../inbox/` |
| `AGENTS_RELAY_APPROVER` | Chat id used by `approve` when `--user` is missing or not numeric (HTTP turns often pass `anonymous`) |
| `AGENTS_RELAY_ATTACH_DIR` | Only local `/v1/turn` attachment paths under this directory are accepted (default: the relay state dir) |
| `AGENTS_RELAY_MAX_JOBS` | Global cap on parallel Telegram turns (default 8) |
| `AGENTS_RELAY_MAX_JOBS_PER_CHAT` | Parallel turns per chat before extra messages queue (default 3, queue cap 20) |
| `AGENTS_RELAY_WAIT_TEXT` | Telegram placeholder while a turn runs (default `One moment …`). Status and fallback lines are English |
| `RELAY_HOST` / `RELAY_PORT` | Bind (default `127.0.0.1:8787`; `GATEWAY_HOST` / `GATEWAY_PORT` fallbacks) |
| `AGENTS_RELAY_WEB_DIR` | Static dir for `GET /app/` (default: bundled `agents_relay/web/` herd PWA) |
| `TELEGRAM_POLL_TIMEOUT` | Long-poll seconds, clamped 1–50 (default 50) |
| `RELAY_WEB_TOKEN` | Enables cookie auth and the web surface. Empty = no web surface |
| `RELAY_WEB_USER` | Harness `--user` for web turns (default `web`) |
| `RELAY_STATIC_DIR` | Directory served at `/` (the dashboard build). Empty = no static assets |
| `RELAY_WEB_SECURE` | `0` drops the `Secure` cookie flag for plain-HTTP local testing (default on) |

`POST /v1/turn` accepts `attachments`: a list of `{"path": "/abs/file", "mime": "image/png"}` or `{"url": "https://...", "mime": "..."}`. A local `path` must already exist and must resolve inside `AGENTS_RELAY_ATTACH_DIR` (default: the relay state dir). Paths outside that directory are rejected with HTTP 400. `url` must be `http` or `https` and is downloaded into the relay inbox (20 MB cap). When `mime` is set, the saved file extension matches it (`image/jpeg` → `.jpg`) so the harness can infer the type. Each file is passed to the loop as `--attach`. Photos and documents from Telegram take the same `--attach` path. The loop `--user` for a Telegram turn is the numeric chat id so `AGENTS_APPROVAL_CMD` can substitute `{user}`.

`approve --user` may be a non-numeric turn id (HTTP defaults to `anonymous`). Then the prompt goes to `AGENTS_RELAY_APPROVER`, or to the only entry in `TELEGRAM_ALLOWED_CHAT_IDS` when that list has exactly one chat. Otherwise approve exits 2 and prints `{"note":"..."}`. A numeric `--user` is used as given.

`approve` and `serve` must share `AGENTS_RELAY_STATE` on the same host. The poll loop answers `callback_query` and writes the decision. Only allowlisted user ids can press Approve or Deny. In a private chat that id is the chat id. In a group, list both the group chat id and the approving user id.

HTTP secret headers supported: `X-Relay-Secret`, `Relay-Secret`, `X-Gateway-Secret`, `Gateway-Secret`. `Authorization: Bearer` and `?secret=`/`?token=` query params also work.

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
  "user": "fabian",
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
`attachments` accepts local paths under `AGENTS_RELAY_ATTACH_DIR` or `http(s)`
URLs. Response: `{ "reply", "session", "user_id", "alias", "returncode",
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
| `BOARD_URL` | | lolaplex-board base for A2A routes |
| `BOARD_PROJECT` | | Default project slug |
| `BOARD_KEY_SLUG` | `gateway` | agents-keys slug for board auth |

## Client config layout

Human `client-*` apps store **UI prefs only** under **`~/.agents/clients/`** (honours `AGENTS_HOME`):

| Path | App |
|------|-----|
| `clients/host/profiles.json` | client-host |
| `clients/overlay/config.json` | client-overlay |

**Sessions** are not client files. Alias bindings live in `identity.json`; conversation bodies and thread resume live in `traces/`. Clients pass `channel`, `user`, and `project`; the harness resolves the latest `ses_…` via `TraceStore.find_latest_session`.

Deprecated `~/.client/` is migrated on first access (host + overlay only).

---

## Constraints

- Python 3.10+, standard library only.
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
