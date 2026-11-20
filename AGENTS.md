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

Env: `LOOP_CMD`, `LOOP_PROVIDER`, `RELAY_SECRET` (fallback `GATEWAY_SECRET`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `AGENTS_RELAY_ALLOW_ANYONE`, `AGENTS_RELAY_APPROVER`, `AGENTS_RELAY_STATE`, `AGENTS_RELAY_ATTACH_DIR`, `AGENTS_RELAY_VISION` (`auto`/`on`/`off`), `AGENTS_RELAY_MAX_JOBS`, `AGENTS_RELAY_MAX_JOBS_PER_CHAT`, `RELAY_HOST`, `RELAY_PORT`, `TELEGRAM_POLL_TIMEOUT`, `AGENTS_RELAY_WEB_DIR`.

Telegram `serve` refuses an empty allowlist unless `AGENTS_RELAY_ALLOW_ANYONE=1`. Approval and inbox files go under `AGENTS_RELAY_STATE` (default `<ws>/relay`). Poll commands: `/jobs`, `/stop`, `/stop <id>`.

Handoffs: `POST .../inbox/<task>/handoff` moves task ownership (peer policy enforced by `agents_workspace.inbox`); `?wake=1` also runs it detached as `routine:handoff:<task>` and writes `done`/`failed` + `reply_summary` back. `GET /v1/workspaces/<ws>/inbox` is the team feed (per-persona counts + recent handoffs).

## ACP (Agent Client Protocol)

`agents_relay/acp.py` speaks ACP in both directions over NDJSON stdio:

- `agents-relay acp-serve` — klanker as an ACP **agent** (Zed and other ACP
  clients spawn this). `session/new` mints `ses_acp_*` ids bound to the
  client cwd; `session/prompt` streams `runner.loop` deltas as
  `agent_message_chunk` and stderr status as `agent_thought_chunk`;
  `session/cancel` kills the turn process tree. Harness approval files
  (`AGENTS_APPROVAL_CMD` pending under `AGENTS_RELAY_DIR/approvals`) bridge
  to `session/request_permission`, so the client's allow/deny UI decides
  them (`AGENTS_APPROVAL_CMD="agents-relay approve --local"`).
  `initialize` advertises `llm_api_key` in `authMethods`; `authenticate`
  stores the received key as `LLM_API_KEY` in the host `secrets.json`.
  Before each turn the selected `LOOP_PROVIDER` manifest is resolved —
  missing url/model/key is reported as an `agent_message_chunk` instead
  of letting the spawned loop die.
- `agents-relay acp-run` — klanker as an ACP **client**. Accepts the
  `runner.loop` argv shape so it is a `LOOP_CMD` drop-in; spawns the agent
  named by `AGENTS_ACP_CMD` (e.g. `devin acp`, `ssh box devin acp`), reuses
  persisted `acpSessionId`s via `session/load`, streams reply text to
  stdout, ends with the loop trailer marker. `authenticate` is only sent
  when `AGENTS_ACP_API_KEY` is set (`meta.api_key`) — agents with stored
  creds (e.g. `devin auth login`) run without it and fail loudly at
  `session/new` if genuinely unauthenticated.

Foreign agents register as `acp.<name>` providers: `serve` drops
`<host>/providers/acp.<name>.json` (`kind: acp-agent`) when `AGENTS_ACP_CMD`
is set, so `/v1/providers`, `/v1/model`, and workspace picks see them like
manifest providers. Turns with `provider=acp.*` spawn `acp-run` instead of
`LOOP_CMD` (flag > workspace pick > env); `--model` maps to
`session/set_config_option`. Agent model/mode lists are probed on demand and
cached in `<state>/acp_models/<name>.json` (24 h, `?refresh=1` re-probes).

Env: `AGENTS_ACP_CMD` (required for acp-run), `AGENTS_ACP_API_KEY`,
`AGENTS_ACP_CMD_<NAME>` (per-provider command, `acp.devin` → `AGENTS_ACP_CMD_DEVIN`),
`AGENTS_ACP_NAME` (provider name pin).
