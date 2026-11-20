# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `chat_allowed` checks `<ws>/members.json` via `agents_workspace.members.member_role` before the env allowlist (workspace#15): a member chat_id passes even when `TELEGRAM_ALLOWED_CHAT_IDS` is empty; the env list / `AGENTS_RELAY_ALLOW_ANYONE` remain the fallback when no workspace member row matches.
- `POST /v1/push/send` — VAPID delivery via lazy `pywebpush` import; `501` when the package is absent while the key/subscribe endpoints stay live. `approvals.create_pending` fires a best-effort `notify_approval_pending` push to stored subscriptions (gone endpoints 404/410 are pruned). Storage layout per klanker#37: keypair `<ws>/relay/vapid.json`, subscriptions `<ws>/relay/push.json`.
- Web Push plumbing (klanker#37): `GET /v1/push/vapid-key` serves the host's VAPID public key — minted on demand as pure-stdlib secp256r1 scalar multiply (no crypto dep), private scalar persisted hex in `<host>/keys/vapid.json` (0600). `POST /v1/push/subscribe` upserts browser PushSubscriptions by endpoint into `<ws>/relay/push/subscriptions.jsonl` (serve state dir when no `?workspace=`). The actual send path (JWT + aes128gcm payload encryption) is still open — it needs a crypto dependency decision.
- `GET /v1/runs/stream` — SSE attach to a running web turn (klanker#92). Look up by `?run=<id>` or `?workspace=&persona=&session=`; replays the buffered event tail (start/status/delta/trailer, capped at 2000 events) then fans out live, ending after the trailer. Active runs now keep an event buffer + meta under a per-run condition, so other devices watch the same turn instead of polling traces.
- `GET /?token=<RELAY_WEB_TOKEN>` performs the cookie login directly: issues the HttpOnly cookie and 302-redirects to `/`, so a printed serve URL is paste-ready without touching the login form (pairs with `klanker serve` auto-token).
- Runtime model switch: `GET /v1/model` returns the workspace pick plus provider registry; `POST /v1/model {provider, model?, workspace?}` persists the pick into the workspace (harness `runner.model`), `{clear: true}` removes it. Telegram `/model [<provider>[:<model>] | clear]` stores a per-chat override that rides into turns as `--provider`/`--model` — chat pref > explicit flag > workspace pick > default.
- `POST /v1/send` — plain Telegram notification without running a turn (`{chat_id|user, text}`), allowlist-enforced, relay-secret gated. The `agents-relay send` CLI covers the same ground locally; the route exists for `klanker send` and remote callers.
- Cron/routine delivery matrix: `/v1/turn` and `/v1/alert` accept `deliver` (or `deliver_to`) with comma-separated `channel:user` targets — `telegram:<chat>`, `telegram:*`/`all`, `none` — so routine output reaches an arbitrary allowlisted chat instead of always the first entry. The response reports `delivered` chat ids and `skipped` targets.
- Text-to-speech replies for Telegram voice notes: with `AGENTS_RELAY_TTS=1` (and `GROQ_API_KEY` set), a voice-in message gets a `sendAudio` mp3 back synthesized by Groq `playai-tts` — the text reply still lands first. `AGENTS_RELAY_TTS_VOICE` / `AGENTS_RELAY_TTS_MODEL` override the voice and model.
- Speech-to-text for Telegram voice/audio messages: the file downloads into the relay inbox and is transcribed via Groq Whisper (`whisper-large-v3-turbo`) into the turn text. Opt-in via `GROQ_API_KEY` (resolved through workspace/host secrets first); without a key the user gets a notice instead of a silent drop.
- `GET /v1/presence/offers` + `POST /v1/presence/publish`: signed `road-declaration/1` offers in `AGENTS_PRESENCE_DIR` (default `~/.agents/presence`). Signing/verification shells out to `agents_keys` — the relay never touches key material. Forged/expired/broken offers list as invalid rows with their refusal reason; publish self-verifies before writing.

### Changed
- Interrupt-and-redirect: a new Telegram message while exactly one turn is running stops that turn and becomes the new turn (a chat that already queued extra messages keeps burst-queue semantics). `/stop` now drops a per-job `AGENTS_INTERRUPT_FILE` marker first — the loop exits cleanly between rounds — then kills the process tree as the hard backstop. The marker path rides to the child as `AGENTS_INTERRUPT_FILE` (`<state>/interrupts/<job>.interrupt`).
- Streamed turns read child pipes in OS chunks (`read1`/`os.read` up to 4096 bytes) instead of `stream.read(1)`, decode UTF-8 incrementally so split multibyte characters survive, and batch stdout deltas on a ~30 ms window (flushed early on newline, 4 KB batch cap, status, trailer, or EOF). `iter_loop_stream` and `iter_loop_stream_live` share the same pump/marker framing, and the web SSE emits `: ping` keepalives during quiet stretches with `Cache-Control: no-cache, no-transform` so proxies do not buffer the stream.
- The web stream turn cap (`iter_loop_stream_live`, spawn-to-trailer wall clock) honors `AGENTS_RELAY_TURN_TIMEOUT` seconds; default stays 900.
- Tests isolate with `AGENTS_HOST_DIR`; the suite dropped the `AGENTS_HOME` alias entirely.
- `RELAY_SECRET`, `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET`, and `RELAY_WEB_TOKEN` resolve via `agents_workspace.resolve_secret()`: `<ws>/secrets.json` > `<host>/secrets.json` > env. The `GATEWAY_SECRET` alias still works as the env fallback; a broken workspace resolver degrades to env-only.

### Fixed
- Telegram replies no longer surface agents-traces frozen addresses: `<ref digest=…/>` and `<stdio …/>` markers are stripped in `strip_model_dumps`, so a long assistant message arrives as ordered ≤4096-char chunks instead of a broken `<ref digest=…>` line (the >4096 split itself was already tag-safe).
- `/health` gets the same CORS origin echo as `/v1/*`: cross-origin dashboards poll it for up/down status, and without the echo every remote showed as "down" so the workspace tree never loaded.
- `mirror_regular_file` opens source and destination with `O_BINARY` on Windows: bytes `\x1a` (Ctrl+Z, DOS EOF) no longer truncate mirrored attachments.
- Child process output is decoded as UTF-8 with `errors="replace"` instead of the locale default (`cp1252` on Western-European Windows), and spawned children get `PYTHONUTF8=1`. German umlauts and any non-ASCII text no longer come back mojibake in turn results.
- Unhandled route errors now answer a JSON `500` instead of dropping the socket: `TurnHandler` wraps `handle_one_request`, so a handler exception (e.g. a locked task file in the `handoff?wake=1` spawn path on Windows) no longer reaches the client as a bare `RemoteDisconnected`.
- Handoff wake threads are tracked (`wake.drain()` joins live threads for shutdown/tests) and a failed result write logs instead of killing the thread mid-flight. Root cause of the flaky `TestHandoffWake` failures (transient Windows file locks on the atomic task `os.replace`) is fixed in agents-workspace `_replace`/`_load`.

### Added
- Relay-held remote mounts: `GET/POST/DELETE /v1/remotes` manages a registry in `<state>/remotes.json` (`id`, `base_url`, `label`, secret), and `GET/POST/DELETE /v1/remote/<id>/<upstream-path>` proxies to the remote relay with its stored Bearer injected — SSE streams forward chunked. Remote secrets never reach the client, browser/PWA dashboards only talk to their own origin, and remote relays without browser CORS work through the mount. Client `secret`/`token` query params are stripped upstream.
- Tauri webview origins (`tauri://localhost`, `http(s)://tauri.localhost`) are always echoed on `/v1/*` CORS so the bundled desktop shell works against any relay without `RELAY_CORS_ORIGINS`. Browsers cannot produce these origins, and `/v1/*` still requires its token, so exposure is unchanged.
- Loopback dev origins (`http(s)://localhost:*`, `127.0.0.1`, `::1`) are echoed on `/v1/*` CORS the same way, so dev servers and local PWAs reach any relay without `RELAY_CORS_ORIGINS`.
- Accepted turn attachments are copied into `<workdir>/.relay-inbox/` (generated name, regular file only, 20 MB cap, symlinks refused) so workdir-jailed file tools can read them. With no session workdir, the inbox directory is appended to the child `AGENTS_WORKDIR_ALLOW`.
- `AGENTS_RELAY_VISION` (`auto`, `on`, `off`). `auto` follows `AGENTS_VISION` when set, otherwise the provider manifest (`vision: true` or `capabilities` containing `vision`) from `AGENTS_PROVIDERS_DIR` or the installed agents-harness provider data. An image with vision off or unsupported is named in the reply. `on` and `off` are passed to the child as `AGENTS_VISION`.

### Fixed
- CORS preflight now allows `PUT` and `DELETE`: the allowlist only echoed `GET, POST, OPTIONS`, so same-origin-separate apps (dev server, desktop shell on a dev origin) could list sessions but never delete them.
- `.relay-inbox/.gitignore` contains `*` so a workdir `git add` does not commit attachment copies.
- `AGENTS_RELAY_VISION=auto` passes the resolved decision to the child as `AGENTS_VISION=1` or `0`, so the harness does not send an image the relay has already reported as not shown.
- A missing, oversized, malformed, or failed attachment is reported on the reply instead of being omitted. Telegram downloads and inline base64 uploads use the same 20 MB cap as URL downloads.
- `POST /v1/stream` materializes `attachments` the same way as `/v1/turn`. A web chat turn can send an attachment without text.

### Removed
- Bundled hand-written PWA (`src/agents_relay/web/`); the relay serves only a configured static dir (`RELAY_STATIC_DIR`/`AGENTS_RELAY_WEB_DIR`) - the dashboard build comes from klanker `web/` (option 1b).

### Added
- Telegram `/persona` command: `/persona` shows current + available personas, `/persona <id>` pins a per-chat persona (stored in `<ws>/relay/chat_prefs.json`), `/persona default` resets. Turns run under the pinned persona so sessions stay separate while workspace memory stays shared.
- Telegram `/workspace` command: `/workspace` lists workspaces, `/workspace <id>` switches the chat to a real workspace (turns then run with `workspace=<id>`), `/workspace new <id> [label]` creates and switches, `/workspace serve` resets to the serve-selected workspace. A pinned persona that does not exist in the target workspace is reset to that workspace's default persona.
- `DELETE /v1/workspaces/<id>[?hard=1]` archives the workspace to `workspaces/.trash` (or removes it with `hard=1`), returning the archive path. Refuses the serve-selected workspace with 409; invalid ids and traversals get 400, missing secret 401. Telegram `/workspace rm <id>` archives the same way and refuses the chat's current workspace.
- `DELETE /v1/workspaces/<ws>/personas/<p>/sessions/<s>` removes a session record file; 404 when unknown, 400 on invalid ids, 501 when agents-traces is absent.

### Changed
- `select_workspace()` now reads `AGENTS_WORKSPACE` from the process env, so the serve-pinned workspace wins when several workspaces exist (previously `/persona` and other control paths failed with "No workspace resolved." whenever a second workspace was present).
- `.mailmap` maps legacy `Klix`/`Felix Kempf` author names to `Klix927` (no history rewrite; display only).
- Telegram progress UX: the thinking message animates rotating dots (1-3 dots, random start, 0.9s cadence) on a timer instead of echoing tool-call status. Tool detail stays in traces and `/v1/stream` for dashboard clients.

### Fixed
- `humanize_status` drops machine noise (Python `*.py:NN: Warning`, `Traceback`, `File "..."` frames, `*Error:` lines) so interpreter warnings can no longer surface as chat messages.
- Outbound Telegram replies pass `workspace`/`persona` from the resolved session into `record_message` so assistant events stamp the same identity fields as turn events.

### Added
- Persona + session HTTP routes (P2): `GET /v1/personas?workspace=<id>` (workspace-scoped, equal personas), `POST /v1/workspaces` accepts `{id,label}` (create yields the workspace's first persona `default`), `GET/POST /v1/workspaces/<ws>/personas[/<p>]` and `GET/POST /v1/workspaces/<ws>/personas/<p>/sessions[/<s>]` (list, mint, history incl. trace events). Ids only - traversal/path/label refs are 400. Session routes use agents-traces lazily; 501 when unavailable.
- Persona inbox routes (P2 handoffs): `GET/POST /v1/workspaces/<ws>/personas/<p>/inbox` (list, create task) and `POST .../inbox/<task>/handoff {to, note}` — moves ownership to the target persona via `agents_workspace.inbox`.

### Fixed
- Attachments: the allowed root now follows the turn's workspace (`ws.relay_state`) instead of the env-selected workspace — fixes ambiguous-workspace 500s and cross-workspace allowance when `/v1/turn` carries a workspace id.
- Tests: `sys.executable` replaces bare `python` in `loop_cmd` (python3-only
  images); `test_cli_local_decide` detaches a closed stdin pipe before
  `communicate()` instead of flushing it.

### Changed
- Docs updated to the Host > Workspace layout (`<ws>/relay`, `<host>/clients`,
  `AGENTS_HOST_DIR`). Historic entries below keep the old names.

### Added
- `GatewayClient.workspace` — sent as the `workspace` field on `/v1/turn` and `/v1/stream` and persisted in repl prefs.
- `workdir` field on `/v1/turn`, `/v1/stream`, `/v1/chat/stream`, `TurnParams`, and both `run_loop_turn` variants — sets the child turn's session cwd (`AGENTS_WORKDIR`) independent of the workspace.

### Changed
- Env renames via `agents_workspace.compat.read_alias`: `AGENTS_RELAY_DIR` (alias `AGENTS_RELAY_STATE`), `AGENTS_RELAY_WEB_DIR` (alias `RELAY_STATIC_DIR`), `RELAY_SECRET`/`RELAY_HOST`/`RELAY_PORT` canonical with `GATEWAY_*` deprecated aliases. `GatewayConfig` is a deprecated module-level alias for `RelayConfig` (warns on import).
- Client-app paths resolve on the host: `paths.host_dir()`/`clients_dir()` return `<host>` / `<host>/clients` via `agents_workspace.Host` (helper `agents_home()` removed). `migrate_legacy_dot_client` still lifts `~/.client` once with a DeprecationWarning.
- Update-check cache lives at `<host>/cache/updates/`.
- `resolve_state_dir` never falls back to the shared legacy `~/.agents-relay` anymore. Relay state (approvals, inbox, attachments) always lives under the selected workspace's `relay/` domain and is created on demand. A present legacy dir triggers one `DeprecationWarning` pointing at `agents-workspace migrate`.
- `workspace` and `persona` on `/v1/turn`, `/v1/alert`, `/v1/stream`, and `/v1/chat/stream` are strict ids now: paths, labels, and traversal (`../x`) are rejected with HTTP 400. The workspace resolves via `Host` per request and the child gets `ws.env()` — no process-global env mutation.
- Attachments materialize into the selected workspace's `relay/inbox`, never the server workspace's.

### Added
- `GET /app/` serves a bundled static herd PWA (installable chat client) same-origin with the API — a phone on the tailnet can pin `https://<node>.ts.net/app/` and talk to `/v1/*` with no cross-origin fetch and no CORS change. `/app` redirects to `/app/`; paths stay inside the web root. `AGENTS_RELAY_WEB_DIR` overrides the bundled dir. Static files stay public; `/v1/*` keeps the secret check.
- Multi-workspace turns: `"workspace"` on `/v1/turn`, `/v1/stream`, and `/v1/chat/stream` runs the loop subprocess with that workspace's env (`AGENTS_WORKSPACE_ROOT`, `AGENTS_WORKDIR`, and every domain path). `GET /v1/workspaces` lists the default plus every workspace under `AGENTS_WORKSPACES_DIR`; `POST /v1/workspaces {"label": ...}` creates one.
- Web surface for a browser client: `RELAY_WEB_TOKEN` enables cookie auth, `RELAY_STATIC_DIR` serves a dashboard build at `/`. Login exchanges the token for an HttpOnly `relay_web` cookie (30 days, HMAC-signed). `RELAY_WEB_USER` and `RELAY_WEB_SECURE` tune it. The relay ships no UI of its own.
- `POST /v1/chat/stream` streams `start`, `status` (humanized stderr progress), `delta` (answer tokens), `trailer`, and `error` events. `POST /v1/chat/stop` kills the turn's process tree by `run` id. `GET /v1/auth/status`, `POST /v1/auth/login`, `POST /v1/auth/logout`.
- `iter_loop_stream_live` in `turn.py`: stdout and stderr feed one queue, so progress is visible while tools run. Cancellation reuses `jobs.kill_process_tree`.

### Added
- Dashboard API (agents-relay#12): `GET /v1/traces?workspace=&persona=&session=&limit=` tails the workspace's agents-traces daily JSONL files (`<ws>/traces/YYYY-MM-DD.jsonl`) filtered by session id and returns the newest `limit` raw lines (`{"lines": [...], "note": ""}`); without `session` it lists that persona's on-disk session ids instead. `persona` narrows events to that stamp (unstamped events still match). `GET /v1/approvals?workspace=` lists pending tool approvals (`{id, tool, summary}`) and `POST /v1/approvals/<id>` with `{"allow": true|false}` resolves one through the same `approvals/<id>.json` file the Telegram callback and `approve --decide` write. `?workspace=` targets `<ws>/relay/approvals`; without it the serve state dir is used.
- `RELAY_CORS_ORIGINS`: comma-separated origin allowlist for cross-origin dashboard fetches. Matching `/v1/*` responses carry `Access-Control-Allow-Origin` and `OPTIONS` preflights answer 204 with `Access-Control-Allow-Methods`/`-Headers`. Unset means no CORS headers at all; same-origin static serving is unaffected.

### Changed
- `_check_secret` accepts the `relay_web` cookie. With `RELAY_WEB_TOKEN` set and `RELAY_SECRET` empty, the cookie is the only gate on every endpoint instead of "no auth".
- `GET /` serves `index.html` from `RELAY_STATIC_DIR` when it exists and keeps `{"ok": true}` otherwise. `_parse_query` keeps `/` instead of collapsing it to an empty path.

### Added
- Active handoff wake (#18): `POST /v1/workspaces/<ws>/personas/<p>/inbox/<task>/handoff?wake=1` moves the task (peer policy `accepts_messages`/`allow_peers` and the hop window enforced by `agents_workspace.inbox`), then runs it as a detached background turn under `routine:handoff:<task_id>` so interactive chat stays clean. The child env carries `AGENTS_PERSONA`, `AGENTS_PERSONA_HOP`, and `AGENTS_THREAD_ID` (new `extra_env` on `loop_client.run_loop_turn`). The task transitions `open -> running -> done|failed` with `result`/`reply_summary`; tasks with `mode=ask` or a `reply_to` field are handed back to the origin persona as the result notification. Answers 202 `{ok, task, wake: true}`.
- Team inbox feed: `GET /v1/workspaces/<ws>/inbox` returns per-persona counts (`open`/`running`/`done`/`failed`, `unread`, `total`) plus the newest 20 `handoff`/`reply` events across personas for the Web UI badge and activity feed.

## [0.1.1] - 2026-11-08

### Added
- `agents-relay approve --local` waits on `$AGENTS_RELAY_STATE/approvals/<id>.json` without Telegram. `--decide <id> --allow` or `--deny` writes that same decision.

### Fixed
- `POST /v1/stream` closes the HTTP connection after the trailer, so a client reading the event stream receives `delta` and `trailer` instead of waiting on keep-alive.
- `LOOP_CMD` splits with the platform's quoting rules, and the value is no longer quote-stripped at both ends. A quoted program path such as `C:\Program Files\Python\python.exe` survives on Windows.

## [0.1.0] - 2026-10-06

### Added
- `agents-relay approve --user <chat_id> --timeout N` reads a tool-approval JSON object on stdin, sends a Telegram Approve/Deny keyboard, and exits 0/1/2. The poll loop answers `callback_query` and writes `$AGENTS_RELAY_STATE/approvals/<id>.json`. Only allowlisted users can decide.
- Telegram updates run on worker threads. Each chat can run up to `AGENTS_RELAY_MAX_JOBS_PER_CHAT` turns (default 3) and queues the rest. `/jobs`, `/stop`, and `/stop <id>` list and kill those process groups. The thinking-status edit is unchanged.
- Long Telegram replies are split at paragraph boundaries into valid HTML messages instead of being cut at 4096 characters.
- Telegram photos and documents, and `/v1/turn` `attachments` (`path` or `http(s)` `url` plus `mime`), are passed to `runner.loop` as repeatable `--attach`.
- Handle `/new` and `/reset` commands in Telegram adapter to start fresh sessions (`new_session=True`).
- CLI (and MCP, when present) check PyPI at most once per day for a newer release and print one stderr / tool-response line (`uv tool upgrade …`). Disabled with `AGENTS_NO_UPDATE_CHECK=1` or when `CI` is set; offline/timeout stays silent.

### Changed
- The Telegram adapter refuses to start when `TELEGRAM_ALLOWED_CHAT_IDS` is empty unless `AGENTS_RELAY_ALLOW_ANYONE=1` (loud warning). Empty allowlists no longer mean "everyone".
- Relay files live in `AGENTS_RELAY_STATE` (default `~/.agents-relay`). Nothing under `~/.agents`.
- Telegram turns pass the numeric chat id as `runner.loop --user` so approval `{user}` substitution addresses that chat.
- `approve` falls back to `AGENTS_RELAY_APPROVER`, or the single allowlisted chat, when `--user` is missing or not numeric. Otherwise it exits 2 with a JSON note.
- Local `/v1/turn` attachment paths must resolve inside `AGENTS_RELAY_ATTACH_DIR` (default: relay state dir). `mime` sets the saved file extension.
- Telegram status and fallback text is English (`One moment …`, `Calendar done.`, `(empty reply)`). `AGENTS_RELAY_WAIT_TEXT` overrides the placeholder shown while a turn runs.

### Fixed
- Prevent race condition between stdout reader and stderr loop reader in `loop_client.run_loop_turn`.
- Extract informative root exception line in `telegram_adapter` rather than truncated `Traceback (most recent call last):` header.
- Suppress internal intermediate tool failure footnotes in Telegram formatting when the agent produces a valid final reply body.
- `agents-relay serve` exits promptly on SIGTERM/SIGINT. The signal handler called `server.shutdown()` on the thread running `serve_forever()` and deadlocked, so `docker stop` waited for the kill timeout.

## [0.0.2] - 2026-09-27

### Added
- `POST /v1/inject` and `agents-relay inject --user/--chat-id --text` run one turn as an allowlisted Telegram `chat_id` through the same thinking-edit handler as long-poll. Caller gets JSON (`reply`, `traces`, session). Telegram still shows the live bubble. Denylist and empty text are rejected.
- `agents-relay send --user/--chat-id --text` one-shot Telegram outbound (allowlist + env token). Hosts can deliver reminders without a poll turn.
- Forward loop tool execution traces from stderr to Telegram and HTTP alert HTML formatting.
- Real-time Telegram message editing during tool runs with throttled status streaming.
- Photo and caption support in Telegram long-poll adapter (downloads incoming images to local inbox and attaches path to turn).
- Automatic Markdown table transformation to mobile-readable Telegram bullet lists with inline formatting.

### Changed
- Telegram in-progress status and leftover tool traces read as short prose (module names and exit codes kept). Empty model replies after thinking/tools synthesize a fallback from those steps instead of `Fertig.` or silence. Long answers no longer get truncated to make room for a CLI tools dump.
- CI runs only on pull requests to `main`.
- README now documents install, the real CLI (`serve` / `send` / `--help-json`), env, and the verify command.
- CI runs only on pull requests to `dev`/`main` and on manual dispatch, not on branch pushes or GitHub release events.
- Telegram loop-failure copy is English (`Turn failed (...)`) instead of a hardcoded German apology.

### Fixed
- A turn is no longer killed after 10 minutes. The relay waits until the loop exits; each model call still stops on the provider first-byte and idle limits. A configured turn limit that does fire returns one sentence and does not include the process command.
- Raw HTML tags no longer leak into Telegram when `editMessageText` returns 400 (`message is not modified` ignored; HTML stripped on entity errors).

### Removed
- Automatic PyPI Trusted Publishing and GitHub Release creation from Actions (no tag-triggered upload).
- Committed `dist/` wheels. Build artifacts stay local; install from source or a release.

## [0.0.1] - 2026-09-05

### Added
- Stdlib-only HTTP `/v1/turn` and Telegram long-poll relay for `agents-harness`.
- Buffered `runner.loop` subprocess with trailer parsing.
- Telegram message lifecycle from `thinking...` to formatted HTML answer.

[Unreleased]: https://github.com/Lolaplex/agents-relay/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Lolaplex/agents-relay/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Lolaplex/agents-relay/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/Lolaplex/agents-relay/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/Lolaplex/agents-relay/releases/tag/v0.0.1
