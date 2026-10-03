# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Handle `/new` and `/reset` commands in Telegram adapter to start fresh sessions (`new_session=True`).

### Fixed
- Prevent race condition between stdout reader and stderr loop reader in `loop_client.run_loop_turn`.
- Extract informative root exception line in `telegram_adapter` rather than truncated `Traceback (most recent call last):` header.
- Suppress internal intermediate tool failure footnotes in Telegram formatting when the agent produces a valid final reply body.

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

[Unreleased]: https://github.com/Lolaplex/agents-relay/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/Lolaplex/agents-relay/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/Lolaplex/agents-relay/releases/tag/v0.0.1
