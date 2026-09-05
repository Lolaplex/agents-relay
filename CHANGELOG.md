# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.1] - 2026-09-05

### Added
- Initial release of `agents-relay` (stdlib-only HTTP `/v1/turn` & Telegram long-poll relay for `agents-harness`).
- Buffered subprocess execution of `runner.loop` with trailer parsing.
- Dynamic Telegram message update lifecycle (`thinking...` to formatted HTML answer).
- Zero-dependency architecture using standard library HTTP & urllib.
