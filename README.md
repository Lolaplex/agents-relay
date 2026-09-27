# agents-relay

<p align="center">
  <a href="https://github.com/Lolaplex/agents-relay/releases"><img src="https://img.shields.io/badge/version-0.0.2-blue.svg?style=flat-square" alt="Version 0.0.2"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-relay/"><img src="https://img.shields.io/pypi/v/agents-relay.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <strong>Stdlib HTTP and Telegram relay in front of agents-harness runner.loop.</strong><br>
  One subprocess per turn. Trailer parsing for session metadata. No identity store, no traces, no writes under <code>~/.agents</code>.
</p>

---

## Quickstart

```bash
pip install agents-relay
```

Needs `runner.loop` on `PATH` (`LOOP_CMD`, default `python -m runner.loop`). That comes from [agents-harness](https://github.com/Lolaplex/agents-harness).

> [!TIP]
> **🤖 Agent-Driven Setup:**
> Give your coding agent **this repo** (clone or URL), then tell it to **"install agents-relay and configure it to relay between your chat interface and agents-harness."**

---

## Architecture

| Component | Responsibility |
| :--- | :--- |
| **HTTP Adapter** | Serves `/v1/turn` and `/v1/inject` endpoints with secret header validation |
| **Telegram Adapter** | Long-poll bot updates with real-time thinking status edits |
| **Loop Client** | Spawns buffered `runner.loop` subprocess per turn with trailer parsing |
| **Outbound Send** | Direct Telegram push via `agents-relay send` for reminders and notifications |

- **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM logic.
- **Stateless Relay**: No identity storage, no traces, no local state under `~/.agents`.
- **Streaming Status**: Throttled progress streaming and real-time status edits during long-running tool rounds.
- **Zero Bloat**: Pure Python standard library (`http.server`, `urllib.request`, `subprocess`).

---

## Capabilities & Roadmap

### Core Capabilities (Implemented)

- [x] **HTTP Turn Endpoints (`/v1/turn`, `/v1/inject`)**: Buffered turn execution with secret header validation.
- [x] **Telegram Long-Poll Gateway**: Bot API updates with real-time thinking status message editing.
- [x] **Direct Outbound CLI (`send`, `inject`)**: Host-initiated push messages and simulated turns without background polling.
- [x] **Mobile Formatting**: Automatic Markdown table conversion to Telegram-readable bullet lists.
- [x] **Trailer Parsing**: Extracts session, user ID, alias, and return code from harness stdout trailers.
- [x] **Subprocess Isolation**: One `runner.loop` process per request. No in-process LLM state.
- [x] **Zero Bloat Runtime**: Pure Python standard library only.

### Planned (Roadmap)

- [ ] **SSE Token Streaming (`/v1/stream`)**: Direct server-sent events for client web UIs.
- [ ] **A2A Board Mailbox Proxy (`/v1/a2a/mail`, `/v1/a2a/peers`)**: Peer-to-peer agent relay for board instances.
- [ ] **Inbound Webhook Hub (`/v1/webhook/{source}`)**: Direct ingress for CI/CD and deployment alerts.
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

---

## Environment

| Variable | Description |
| :--- | :--- |
| `LOOP_CMD` | Loop argv (default `python -m runner.loop`) |
| `LOOP_PROVIDER` | Provider name passed through (default `echo`) |
| `RELAY_SECRET` | Shared secret (`GATEWAY_SECRET` fallback). Empty = no auth |
| `TELEGRAM_BOT_TOKEN` | Enables poll + `send` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated numeric IDs |
| `RELAY_HOST` / `RELAY_PORT` | Bind (default `127.0.0.1:8787`; `GATEWAY_HOST` / `GATEWAY_PORT` fallbacks) |
| `TELEGRAM_POLL_TIMEOUT` | Long-poll seconds, clamped 1–50 (default 50) |

HTTP secret headers supported: `X-Relay-Secret`, `Relay-Secret`, `X-Gateway-Secret`, `Gateway-Secret`.

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
