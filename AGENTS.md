# agents-relay

Stdlib-only relay: HTTP `/v1/turn` and optional Telegram long-poll. Spawns `runner.loop` per turn; no `~/.agents` writes.

## Commands

```bash
python -m agents_relay --help-json
agents-relay serve
agents-relay send --user <chat_id> --text "hello"
```

Env: `LOOP_CMD`, `LOOP_PROVIDER`, `RELAY_SECRET` (fallback `GATEWAY_SECRET`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `RELAY_HOST`, `RELAY_PORT`, `TELEGRAM_POLL_TIMEOUT`.
