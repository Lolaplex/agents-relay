# agents-relay

Stdlib-only relay: HTTP `/v1/turn`, `/v1/inject`, and optional Telegram long-poll. Spawns `runner.loop` per turn; no `~/.agents` writes.

## Commands

```bash
python -m agents_relay --help-json
agents-relay serve
agents-relay send --user <chat_id> --text "hello"
agents-relay inject --user <chat_id> --text "hello"
agents-relay approve --user <chat_id> --timeout 300  # JSON request on stdin
```

Env: `LOOP_CMD`, `LOOP_PROVIDER`, `RELAY_SECRET` (fallback `GATEWAY_SECRET`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `AGENTS_RELAY_ALLOW_ANYONE`, `AGENTS_RELAY_STATE`, `AGENTS_RELAY_MAX_JOBS`, `AGENTS_RELAY_MAX_JOBS_PER_CHAT`, `RELAY_HOST`, `RELAY_PORT`, `TELEGRAM_POLL_TIMEOUT`.

Telegram `serve` refuses an empty allowlist unless `AGENTS_RELAY_ALLOW_ANYONE=1`. Approval and inbox files go under `AGENTS_RELAY_STATE` (default `~/.agents-relay`), never `~/.agents`. Poll commands: `/jobs`, `/stop`, `/stop <id>`.
