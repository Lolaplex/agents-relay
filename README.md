# agents-relay

Stdlib HTTP and Telegram relay in front of [agents-harness](https://github.com/Lolaplex/agents-harness) `runner.loop`. One subprocess per turn. Trailer parsing for session metadata. No identity store, no traces, no writes under `~/.agents`.

## Install

```bash
python -m pip install -e .
```

Needs `runner.loop` on `PATH` (`LOOP_CMD`, default `python -m runner.loop`). That comes from agents-harness, not this package.

## Commands

Machine catalog: `python -m agents_relay --help-json` (do not scrape `--help`).

| Command | Purpose |
|---------|---------|
| `agents-relay serve` | HTTP `/v1/turn` (and `/v1/alert`, `/webhook/alert`). Telegram long-poll if `TELEGRAM_BOT_TOKEN` is set |
| `agents-relay serve --no-telegram` | HTTP only |
| `agents-relay send --user <chat_id> --text "..."` | One outbound Telegram message (`--chat-id` alias). Allowlist + token from env |

## Env

| Variable | Role |
|----------|------|
| `LOOP_CMD` | Loop argv (default `python -m runner.loop`) |
| `LOOP_PROVIDER` | Provider name passed through (default `echo`) |
| `RELAY_SECRET` | Shared secret (`GATEWAY_SECRET` fallback). Empty = no auth |
| `TELEGRAM_BOT_TOKEN` | Enables poll + `send` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated numeric ids |
| `RELAY_HOST` / `RELAY_PORT` | Bind (default `127.0.0.1:8787`; `GATEWAY_HOST` / `GATEWAY_PORT` fallbacks) |
| `TELEGRAM_POLL_TIMEOUT` | Long-poll seconds, clamped 1–50 (default 50) |

HTTP secret headers: `X-Relay-Secret`, `Relay-Secret`, `X-Gateway-Secret`, `Gateway-Secret`.

## Constraints

- Python 3.10+, stdlib only.
- Does not import harness internals. Spawns `LOOP_CMD` per turn.
- Klanker / hosts own identity and traces. This process is I/O only.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## License

MIT. See [LICENSE](LICENSE).
