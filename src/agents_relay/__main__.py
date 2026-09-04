"""CLI: serve HTTP relay and optional Telegram poll."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys

from . import __version__
from .config import RelayConfig
from .http_adapter import serve_http
from .telegram_adapter import start_telegram_thread

log = logging.getLogger("agents_relay")


def _help_json() -> dict:
    return {
        "name": "agents-relay",
        "version": __version__,
        "commands": {"serve": {"description": "Start HTTP /v1/turn and optional Telegram polling"}},
        "flags": ["--help-json"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-relay")
    parser.add_argument("--help-json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="Run relay")
    serve_p.add_argument("--no-telegram", action="store_true", help="Disable Telegram polling")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if getattr(args, "help_json", False):
        print(json.dumps(_help_json(), indent=2))
        return 0
    if args.command != "serve":
        build_parser().print_help()
        return 0

    config = RelayConfig.from_env()
    server = serve_http(config)
    tg_thread = None
    tg_stop = None
    if not args.no_telegram and config.telegram_bot_token:
        tg_thread, tg_stop = start_telegram_thread(config)

    def _shutdown(*_sig) -> None:
        log.info("shutting down")
        if tg_stop is not None:
            tg_stop.set()
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown()
    finally:
        server.server_close()
        if tg_thread is not None:
            tg_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
