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

Env: `LOOP_CMD`, `LOOP_PROVIDER`, `RELAY_SECRET` (fallback `GATEWAY_SECRET`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `AGENTS_RELAY_ALLOW_ANYONE`, `AGENTS_RELAY_APPROVER`, `AGENTS_RELAY_DIR` (legacy `AGENTS_RELAY_STATE`), `AGENTS_RELAY_ATTACH_DIR`, `AGENTS_RELAY_VISION` (`auto`/`on`/`off`), `AGENTS_RELAY_MAX_JOBS`, `AGENTS_RELAY_MAX_JOBS_PER_CHAT`, `RELAY_HOST`, `RELAY_PORT`, `TELEGRAM_POLL_TIMEOUT`, `AGENTS_RELAY_WEB_DIR`, `AGENTS_RELAY_TURN_TIMEOUT` (secs, default 900). `GET /v1/discover` scans the tailnet for `klanker serve` health probes (server-side mirror of the desktop scan).

Telegram `serve` refuses an empty allowlist unless `AGENTS_RELAY_ALLOW_ANYONE=1`. Approval and inbox files go under `AGENTS_RELAY_DIR` (default `<ws>/relay`). Poll commands: `/jobs`, `/stop`, `/stop <id>`.

Handoffs: `POST .../inbox/<task>/handoff` moves task ownership (peer policy enforced by `agents_workspace.inbox`); `?wake=1` also runs it detached as `routine:handoff:<task>` and writes `done`/`failed` + `reply_summary` back. `GET /v1/workspaces/<ws>/inbox` is the team feed (per-persona counts + recent handoffs).

## Hub channel (`channel.py` / `channel_client.py`)

`channel.py` is the hub-side channel manager: one connected device = one channel = one seq space. Downstream is a per-device SSE stream (`GET /v1/hub/stream`), upstream is batched envelopes (`POST /v1/hub/send`); kinds `hello`/`presence`/`op`/`rpc-req`/`rpc-res`/`ack`/`resync`/`ws:announce`/`ws:revoke`/`approval-req`/`approval-res`, with a replay ring behind `?since=` resume and `op` payloads broadcast to the other connected devices. `channel_client.py` is the device side: redeems a pair into device credentials, then runs SSE reader + batched POST sender + reconnect resume; `rpc-req` frames dispatch to local methods (`ping`, `status`, `turn.stream`) — the device pulls work, the hub never calls it unauthenticated.

- Control frames stay off the data path: `ws:announce`/`ws:revoke` route hub-side to `set_ws_handler` and device-side to `ChannelClient.on_ws` (ws-share metadata, not datoms); `approval-req`/`approval-res` mirror host approvals (`set_approval_handler` hub-side, `on_approval` device-side; `approval-res` is validated terminal-decision-only). `set_connect_handler` fires on device (re)connect.
- `devices.py` — per-device scoped credentials with a role (`host`/`client`/`ws_member`) and a ws allowlist. Meta in `<host>/clients/devices.json`; bearer tokens only in the host secret vault (`DEVICE_<ID>_TOKEN`), never persisted to meta or re-emitted after the redeem response. `GET /v1/hub/devices` lists, `DELETE /v1/hub/devices/<id>` revokes (channel teardown included). Device tokens authorize the agent-facing surface only — never credential minting/management — and `ws_member` callers are restricted to their allowlisted workspaces.
- `pairing.py` — ephemeral single-use pair codes (`/v1/pair/create`, `/v1/pair/redeem`; ~60 s TTL, in-memory only, rate-limited). Modes `web`/`remote`/`ws`: `POST /v1/ws/invite` (device-token auth, owner check via the klanker-wired `ws_invite_auth` hook) mints ws-scoped codes; redeeming one mints `ws_member` device credentials allowlisted to that ws AND fires `ws_grant_hook` so the host app applies membership. `channel_client.connect_hub` redeems either credential kind and returns the granted ws id; re-linking a known hub re-sends the stored token for an in-place grant (`reused: true`, no second device minted).
- `leases.py` — session run-lock leases (`/v1/hub/lease` acquire/renew/release, `/v1/hub/leases` snapshot — device tokens may read it for remote lease checks, ws_member rows stay ws-scoped). In-memory `LeaseBook` singleton, TTL 45 s + fencing token, per-workspace `lease_ttl_fn`; one live run per (ws, session) — a dropped device's lease expires, never deadlocks. Turn entrypoints (`/v1/turn`, `/v1/chat/stream`, `/v1/send`, run-stop) acquire/release via `turn_lease_hook`/`turn_lease_release_hook`/`run_done_hook`, and every acquire/release emits a `lease.changed` SSE.
- `approvals.py` — host-owned approval records under `<host>/run/approvals/`; `POST /v1/approvals/request` + `GET /v1/approvals/<id>/wait` are turn-authenticated via `Bearer AGENTS_APPROVAL_TOKEN`. `on_pending_fn`/`on_decided_fn` hooks drive cross-device approval mirroring; `request_hash` binds a granted record to its exact tool+args (changed args → 409), and `from_mirror` records can never echo a mirror back.
- `workspaces.py` + `store_api.py` + `providers_api.py` + `storeproj.py` — `/v1/workspaces` CRUD/personas/sessions/inbox, `PATCH /v1/workspaces/<id>` per-ws separation settings (label/workdir/workdir_lock/browser_profile/approval_mode) plus ownership/share fields (`sync_policy`, `sync_hosts`, `default_host`, `repos`, `execution` — refused to `ws_member`; emits `workspace.changed` SSE + `ws_policy_changed_fn` for klanker's share-reconcile), `store/append`/`store/items`/`store/pane` per-workspace agents-store routes (Radicale config optional), `/v1/providers` manifest writes under `<host>/providers` (env-sourced rows read-only). `storeproj.py` unions the synced `msg:*`/`session:*` projection in `<ws>/store.db` into session list/detail so a ws-share peer sees the same continuity without the peer's trace files; `session:deleted` tombstones suppress dead sessions everywhere.
- Presence/hosts + live links — `GET /v1/workspaces/<ws>/hosts` aggregates members, presence and active leases for the host picker (device callers must be admitted via `ws.allows_device`); `GET /v1/hub/links` + `POST /v1/hub/connect` + `DELETE /v1/hub/links/<id>` are admin-only routes onto klanker hooks (`hub_links_fn`/`hub_connect_fn` — redeem invite, write hubs.json+vault, spawn ChannelClient, no serve restart); `POST /v1/workspaces/<ws>/sessions/<sid>/host` switches a session's host via `session_host_switch_fn` (409 passthrough, emits `session.bound`). `GET /v1/events` is the in-memory SSE bus (`workspace` filter + heartbeat): `device.connected`/`device.disconnected`, `ws.announced`/`ws.revoked`, `lease.changed`, `session.bound`, `workspace.changed`.
- `/v1/remote/<id>/<path>` — authenticated proxy into a paired device's local API (`/v1/remotes` manages mounts); turn-capable POSTs route through `remote_turn_dispatch`/`remote_query_dispatch` hooks and `X-Klanker-Hop` counts spoke→hub→spoke hops (508 on a loop). Other workspace management surfaces are registered by klanker at serve time (`/v1/klanker/*`, module `klanker.mgmt` in the klanker repo), not by this package.

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
  of letting the spawned loop die. `session/new`/`session/load` advertise
  `provider`/`model` `configOptions` (model list = manifest `models`
  field); `session/set_config_option` stores the pick per session and it
  reaches the turn as `--provider`/`--model`. MCP child log spam is kept
  out of thought chunks (`FASTMCP_LOG_LEVEL=WARNING` + a status filter).
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
