# agents-relay

Thin relay over [agents-harness](https://github.com/Lolaplex/agents-harness) `runner.loop`: one subprocess per turn, trailer parsing for session metadata. No identity store, no traces.

## Serve

```bash
pip install -e .
export RELAY_SECRET=dev
export LOOP_CMD="python -m runner.loop"
export LOOP_PROVIDER=echo
python -m agents_relay serve
```

## HTTP API

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Liveness |
| `/v1/turn` | POST | Buffered turn JSON |
| `/v1/stream` | POST | SSE token stream |
| `/v1/webhook/{source}` | POST | Alert ingress (`ci`, `coolify`, `schedule`, …) |
| `/v1/a2a/mail` | POST | Board mailbox proxy |
| `/v1/a2a/peers` | GET | Board peers (`?project=slug`) |

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

Response: `{ "reply", "session", "user_id", "alias", "returncode" }`

### POST /v1/stream

Same body as `/v1/turn`. SSE events: `delta`, `trailer`.

### Client SDK

```bash
python -m agents_gateway client turn "hello" --url http://127.0.0.1:8787 --secret dev
```

Library: `agents_gateway.client.GatewayClient`

## Env

| Variable | Default | Notes |
|----------|---------|-------|
| `LOOP_CMD` | `python -m runner.loop` | Harness subprocess |
| `LOOP_PROVIDER` | `echo` | Default provider when body omits `provider` |
| `GATEWAY_SECRET` | | `X-Gateway-Secret` header |
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
