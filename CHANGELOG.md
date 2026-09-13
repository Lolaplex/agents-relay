# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `agents-relay send --user/--chat-id --text` one-shot Telegram outbound (allowlist + env token). Hosts can deliver reminders without a poll turn.
- Forward loop tool execution traces from stderr to Telegram and HTTP alert HTML formatting.
- Real-time Telegram message editing during tool runs with throttled status streaming.
- Photo and caption support in Telegram long-poll adapter (downloads incoming images to local inbox and attaches path to turn).
- Automatic Markdown table transformation to mobile-readable Telegram bullet lists with inline formatting.

### Changed
- CI runs only on pull requests to `dev`/`main` and on manual dispatch, not on branch pushes or GitHub release events.
- Telegram loop-failure copy is English (`Turn failed (...)`) instead of a hardcoded German apology.

### Fixed
- Fixed raw HTML formatting tags appearing in Telegram when `editMessageText` returned 400 (message is not modified ignored; HTML tags stripped on entity errors).

### Removed
- Automatic PyPI Trusted Publishing and GitHub Release creation from Actions (no tag-triggered upload).
- Committed `dist/` wheels. Build artifacts stay local; install from source or a release.

## [0.0.1] - 2026-09-05

### Added
- Initial release of `agents-relay` (stdlib-only HTTP `/v1/turn` & Telegram long-poll relay for `agents-harness`).
- Buffered subprocess execution of `runner.loop` with trailer parsing.
- Dynamic Telegram message update lifecycle (`thinking...` to formatted HTML answer).
- Zero-dependency architecture using standard library HTTP & urllib.
