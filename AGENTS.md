# agents-gateway

Stdlib-only gateway: HTTP `/v1/turn` and optional Telegram long-poll. Spawns `runner.loop` per turn; no `~/.agents` writes.

## Commands

```bash
python -m agents_gateway --help-json
agents-gateway serve
```

Env: `LOOP_CMD`, `LOOP_PROVIDER`, `GATEWAY_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `GATEWAY_HOST`, `GATEWAY_PORT`, `TELEGRAM_POLL_TIMEOUT`.
