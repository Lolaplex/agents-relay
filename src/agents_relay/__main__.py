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
from .telegram_adapter import inbound_payload, inject_text, send_to_user, start_telegram_thread

log = logging.getLogger("agents_relay")


def _chat_id_from_args(args: argparse.Namespace) -> int | None:
    raw = str(getattr(args, "user", "") or getattr(args, "chat_id", "") or "").strip()
    if not raw:
        print("Error: --user or --chat-id is required", file=sys.stderr)
        return None
    try:
        return int(raw)
    except ValueError:
        print("Error: --user must be a numeric chat id", file=sys.stderr)
        return None


def _cmd_send(args: argparse.Namespace) -> int:
    chat_id = _chat_id_from_args(args)
    if chat_id is None:
        return 2
    config = RelayConfig.from_env()
    try:
        send_to_user(
            token=config.telegram_bot_token,
            chat_id=chat_id,
            text=str(args.text),
            allowed=config.telegram_allowed_chat_ids,
        )
    except PermissionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: send failed ({exc})", file=sys.stderr)
        return 1
    return 0


def _cmd_inject(args: argparse.Namespace) -> int:
    chat_id = _chat_id_from_args(args)
    if chat_id is None:
        return 2
    config = RelayConfig.from_env()
    try:
        result = inject_text(chat_id=chat_id, text=str(args.text), config=config)
    except PermissionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: inject failed ({exc})", file=sys.stderr)
        return 1
    print(json.dumps(inbound_payload(result), ensure_ascii=False))
    return 0 if result.returncode == 0 else 1


def _help_json() -> dict:
    return {
        "name": "agents-relay",
        "version": __version__,
        "commands": {
            "serve": {"description": "Start HTTP /v1/turn, /v1/inject, and optional Telegram polling"},
            "send": {
                "description": "Send one Telegram message to an allowlisted chat id",
                "flags": ["--user", "--chat-id", "--text"],
            },
            "inject": {
                "description": "Run one turn as an allowlisted Telegram chat (thinking edits + JSON reply)",
                "flags": ["--user", "--chat-id", "--text"],
            },
        },
        "flags": ["--help-json"],
    }


def _add_chat_text_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", dest="user", default="", help="Target chat id")
    parser.add_argument("--chat-id", dest="chat_id", default="", help="Alias for --user")
    parser.add_argument("--text", required=True, help="Message body")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-relay")
    parser.add_argument("--help-json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="Run relay")
    serve_p.add_argument("--no-telegram", action="store_true", help="Disable Telegram polling")
    send_p = sub.add_parser("send", help="Send one Telegram message to an allowlisted chat id")
    _add_chat_text_flags(send_p)
    inject_p = sub.add_parser(
        "inject",
        help="Run one turn as an allowlisted Telegram chat (thinking edits + JSON reply)",
    )
    _add_chat_text_flags(inject_p)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if not getattr(args, "help_json", False):
        try:
            from .updates import check_for_updates
            check_for_updates("agents-relay", __version__)
        except Exception:
            pass
    if getattr(args, "help_json", False):
        print(json.dumps(_help_json(), indent=2))
        return 0
    if args.command == "send":
        return _cmd_send(args)
    if args.command == "inject":
        return _cmd_inject(args)
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
