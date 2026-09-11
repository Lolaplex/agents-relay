# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `agents-relay send --user/--chat-id --text` one-shot Telegram outbound (allowlist + env token). Hosts can deliver reminders without a poll turn.
- Forward loop tool execution traces from stderr to Telegram and HTTP alert HTML formatting.

### Changed
- Telegram loop-failure copy is English (`Turn failed (...)`) instead of a hardcoded German apology.

### Removed
- Committed `dist/` wheels. Build artifacts stay local; install from source or a release.

## [0.0.1] - 2026-09-05

### Added
- Initial release of `agents-relay` (stdlib-only HTTP `/v1/turn` & Telegram long-poll relay for `agents-harness`).
- Buffered subprocess execution of `runner.loop` with trailer parsing.
- Dynamic Telegram message update lifecycle (`thinking...` to formatted HTML answer).
- Zero-dependency architecture using standard library HTTP & urllib.
