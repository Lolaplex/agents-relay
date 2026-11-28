"""CLI: serve HTTP relay, optional Telegram/Discord adapters, ops commands, client smoke tests."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from typing import Any

from . import __version__
from .approvals import (
    decide_approval,
    resolve_approver_chat,
    run_http_approve,
    run_local_approve,
)
from .client import GatewayClient
from .config import RelayConfig
from .discord_adapter import inbound_payload as discord_inbound_payload
from .discord_adapter import inject_text as discord_inject_text
from .discord_adapter import run_approve as run_discord_approve
from .discord_adapter import send_to_user as send_discord_to_user
from .discord_adapter import start_discord_thread
from .http_adapter import serve_http
from .jobs import shutdown_jobs
from .telegram_adapter import inbound_payload, inject_text, run_approve, send_to_user, start_telegram_thread

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
            allow_anyone=config.allow_anyone,
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


def _cmd_discord_send(args: argparse.Namespace) -> int:
    channel_id = _chat_id_from_args(args)
    if channel_id is None:
        return 2
    config = RelayConfig.from_env()
    try:
        send_discord_to_user(
            token=config.discord_bot_token,
            channel_id=channel_id,
            text=str(args.text),
            allowed=config.discord_allowed_channel_ids,
            allow_anyone=config.allow_anyone,
        )
    except (PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: discord send failed ({exc})", file=sys.stderr)
        return 1
    return 0


def _cmd_discord_inject(args: argparse.Namespace) -> int:
    channel_id = _chat_id_from_args(args)
    if channel_id is None:
        return 2
    config = RelayConfig.from_env()
    try:
        result = discord_inject_text(channel_id=channel_id, text=str(args.text), config=config)
    except (PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: discord inject failed ({exc})", file=sys.stderr)
        return 1
    print(json.dumps(discord_inbound_payload(result), ensure_ascii=False))
    return 0 if result.returncode == 0 else 1


def _resolve_discord_approver_channel(
    raw_user: str,
    config: RelayConfig,
) -> tuple[int | None, str]:
    text = (raw_user or "").strip()
    if text:
        try:
            return int(text), ""
        except ValueError:
            pass
    configured = (config.discord_approver or config.approver or "").strip()
    if configured:
        try:
            return int(configured), ""
        except ValueError:
            return None, "DISCORD_APPROVER/AGENTS_RELAY_APPROVER is not a numeric Discord channel id"
    if len(config.discord_allowed_channel_ids) == 1:
        return int(config.discord_allowed_channel_ids[0]), ""
    return None, (
        "no numeric Discord approver channel; set DISCORD_APPROVER "
        "or allow exactly one channel in DISCORD_ALLOWED_CHANNEL_IDS"
    )


def _approve_route(raw_user: str, config: RelayConfig, explicit: str = "") -> str:
    if explicit in {"telegram", "discord"}:
        return explicit
    has_telegram = bool(config.telegram_bot_token)
    has_discord = bool(config.discord_bot_token)
    if has_discord and not has_telegram:
        return "discord"
    if has_telegram and not has_discord:
        return "telegram"
    if has_telegram and has_discord:
        try:
            numeric = int((raw_user or "").strip())
        except (TypeError, ValueError):
            numeric = 0
        if numeric:
            in_discord = numeric in config.discord_allowed_channel_ids
            in_telegram = numeric in config.telegram_allowed_chat_ids
            if in_discord and not in_telegram:
                return "discord"
            if in_telegram and not in_discord:
                return "telegram"
    return "telegram"


def _cmd_approve(args: argparse.Namespace) -> int:
    config = RelayConfig.from_env()
    decide_id = str(getattr(args, "decide", "") or "").strip()
    if decide_id:
        if bool(getattr(args, "allow", False)) == bool(getattr(args, "deny", False)):
            note = "pass exactly one of --allow or --deny"
            print(json.dumps({"note": note}, ensure_ascii=False))
            print(f"Error: {note}", file=sys.stderr)
            return 2
        code, note = decide_approval(
            config,
            decide_id,
            allow=bool(args.allow),
            remember=bool(getattr(args, "remember", False)),
        )
        print(json.dumps({"note": note}, ensure_ascii=False))
        if code != 0:
            print(f"Error: {note}", file=sys.stderr)
        return code
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        print("Error: invalid JSON on stdin", file=sys.stderr)
        return 2
    if not isinstance(request, dict):
        print("Error: approval request must be a JSON object", file=sys.stderr)
        return 2
    opts: dict[str, Any] = {}
    if getattr(args, "local", False):
        code, note = run_local_approve(config, request, float(args.timeout), out_opts=opts)
        print(json.dumps({"note": note, **opts}, ensure_ascii=False))
        return code
    raw_user = str(getattr(args, "user", "") or getattr(args, "chat_id", "") or "")
    relay_url = os.environ.get("AGENTS_RELAY_URL", "").strip() or (config.relay_url or "").strip()
    approval_token = os.environ.get("AGENTS_APPROVAL_TOKEN", "").strip()
    if relay_url and approval_token:
        ws_id = os.environ.get("AGENTS_WORKSPACE", "").strip()
        code, note = run_http_approve(
            relay_url,
            approval_token,
            request,
            float(args.timeout),
            user=raw_user,
            workspace=ws_id,
            out_opts=opts,
        )
        print(json.dumps({"note": note, **opts}, ensure_ascii=False))
        return code
    explicit = "discord" if getattr(args, "discord", False) else "telegram" if getattr(args, "telegram", False) else ""
    route = _approve_route(raw_user, config, explicit)
    if route == "discord":
        if not config.discord_bot_token:
            print(json.dumps({"note": "discord token not set"}, ensure_ascii=False))
            print("Error: discord token not set", file=sys.stderr)
            return 2
        channel_id, why = _resolve_discord_approver_channel(raw_user, config)
        if channel_id is None:
            print(json.dumps({"note": why}, ensure_ascii=False))
            print(f"Error: {why}", file=sys.stderr)
            return 2
        code, note = run_discord_approve(
            channel_id=channel_id,
            timeout=float(args.timeout),
            request=request,
            config=config,
        )
        print(json.dumps({"note": note}, ensure_ascii=False))
        return code
    if not config.telegram_bot_token:
        ident = str(request.get("id") or "").strip()
        if ident:
            print(
                f"Approval pending ({ident}) — decide with: "
                f"agents-relay approve --decide {ident} --allow",
                file=sys.stderr,
            )
        code, note = run_local_approve(config, request, float(args.timeout), out_opts=opts)
        print(json.dumps({"note": note, **opts}, ensure_ascii=False))
        return code
    chat_id, why = resolve_approver_chat(
        raw_user,
        approver=config.approver,
        allowed=config.telegram_allowed_chat_ids,
    )
    if chat_id is None:
        print(json.dumps({"note": why}, ensure_ascii=False))
        print(f"Error: {why}", file=sys.stderr)
        return 2
    code, note = run_approve(
        chat_id=chat_id,
        timeout=float(args.timeout),
        request=request,
        config=config,
    )
    print(json.dumps({"note": note}, ensure_ascii=False))
    return code


def _help_json() -> dict:
    return {
        "name": "agents-relay",
        "version": __version__,
        "commands": {
            "serve": {"description": "Start HTTP /v1/turn, /v1/stream, /v1/inject, /v1/alert, /v1/webhook/*, /v1/a2a/* plus optional Telegram polling and Discord gateway"},
            "send": {
                "description": "Send one Telegram message to an allowlisted chat id",
                "flags": ["--user", "--chat-id", "--text"],
            },
            "inject": {
                "description": "Run one turn as an allowlisted Telegram chat (thinking edits + JSON reply)",
                "flags": ["--user", "--chat-id", "--text"],
            },
            "discord-send": {
                "description": "Send one Discord message to an allowlisted channel id",
                "flags": ["--user", "--chat-id", "--text"],
            },
            "discord-inject": {
                "description": "Run one turn as an allowlisted Discord channel (thinking edits + JSON reply)",
                "flags": ["--user", "--chat-id", "--text"],
            },
            "approve": {
                "description": "Block until a tool call is approved or denied (JSON on stdin). --local waits on the approval file. --decide writes one decision.",
                "flags": ["--user", "--chat-id", "--timeout", "--local", "--telegram", "--discord", "--decide", "--allow", "--deny"],
            },
            "acp-serve": {
                "description": "Run as an ACP agent over stdio (Zed/ACP clients spawn this; prompts drive runner.loop)",
            },
            "acp-run": {
                "description": "Drive a foreign ACP agent (AGENTS_ACP_CMD) with a runner.loop-shaped CLI; usable as LOOP_CMD",
                "flags": ["--message", "--session", "--workdir", "--deliver", "--new-session"],
            },
            "channel": {
                "description": "Pair this device to a hub and run the persistent channel (agents-relay#40)",
                "subcommands": ["connect", "disconnect", "list", "run"],
            },
            "client": {
                "description": "Smoke-test relay turn/stream/health",
                "subcommands": ["turn", "health"],
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
    serve_p.add_argument("--no-discord", action="store_true", help="Disable Discord gateway")
    send_p = sub.add_parser("send", help="Send one Telegram message to an allowlisted chat id")
    _add_chat_text_flags(send_p)
    inject_p = sub.add_parser(
        "inject",
        help="Run one turn as an allowlisted Telegram chat (thinking edits + JSON reply)",
    )
    _add_chat_text_flags(inject_p)
    discord_send_p = sub.add_parser("discord-send", help="Send one Discord message to an allowlisted channel id")
    _add_chat_text_flags(discord_send_p)
    discord_inject_p = sub.add_parser(
        "discord-inject",
        help="Run one turn as an allowlisted Discord channel (thinking edits + JSON reply)",
    )
    _add_chat_text_flags(discord_inject_p)
    approve_p = sub.add_parser("approve", help="Block until an approval file is decided")
    approve_p.add_argument("--user", dest="user", default="", help="Target chat id")
    approve_p.add_argument("--chat-id", dest="chat_id", default="", help="Alias for --user")
    approve_p.add_argument("--timeout", type=float, default=300, help="Seconds to wait (default 300)")
    approve_p.add_argument("--local", action="store_true", help="Wait on the approval file without a chat adapter")
    route_group = approve_p.add_mutually_exclusive_group()
    route_group.add_argument("--telegram", action="store_true", help="Send the approval prompt through Telegram")
    route_group.add_argument("--discord", action="store_true", help="Send the approval prompt through Discord")
    approve_p.add_argument("--decide", default="", help="Resolve this pending id and exit")
    approve_p.add_argument("--allow", action="store_true", help="With --decide, allow the tool")
    approve_p.add_argument("--deny", action="store_true", help="With --decide, deny the tool")
    approve_p.add_argument("--remember", action="store_true", help="With --decide --allow, persist allow rule")
    sub.add_parser(
        "acp-serve",
        help="Run as an ACP agent over stdio (Zed/ACP clients spawn this)",
    )
    acp_run_p = sub.add_parser(
        "acp-run",
        help="Drive a foreign ACP agent via AGENTS_ACP_CMD; runner.loop-shaped argv",
    )
    # Mirrors the flags build_loop_argv emits so `LOOP_CMD="agents-relay acp-run"`
    # works as a drop-in; most are accepted and ignored (ACP has no analog).
    acp_run_p.add_argument("--message", default="")
    acp_run_p.add_argument("--session", default="")
    acp_run_p.add_argument("--workdir", default="")
    acp_run_p.add_argument("--deliver", default="stream", choices=["stream", "buffered"])
    acp_run_p.add_argument("--new-session", action="store_true")
    acp_run_p.add_argument("--acp-mode", default="")
    acp_run_p.add_argument("--thought-level", default="")
    acp_run_p.add_argument("--speed", default="")
    for ignored in (
        "--user", "--user-id", "--channel", "--complete", "--provider",
        "--model", "--project", "--persona", "--turn-mode", "--reasoning",
    ):
        acp_run_p.add_argument(ignored, nargs="?", const="", default="")
    acp_run_p.add_argument("--attach", action="append", default=[])
    chan_p = sub.add_parser("channel", help="Hub channel: pair this device to a hub")
    chan_sub = chan_p.add_subparsers(dest="channel_cmd")
    cc = chan_sub.add_parser("connect", help="Redeem a pair code on a hub and store the link")
    cc.add_argument("hub_url", help="Hub relay origin, e.g. https://klanker.example")
    cc.add_argument("credential", help="Pair code/token from the hub")
    cc.add_argument("--label", default="", help="Device label shown on the hub")
    cd = chan_sub.add_parser("disconnect", help="Remove a stored hub link")
    cd.add_argument("hub_id")
    chan_sub.add_parser("list", help="List stored hub links")
    cr = chan_sub.add_parser("run", help="Run stored hub channels in the foreground")
    cr.add_argument("--hub", default="", help="Only this hub id")
    client_p = sub.add_parser("client", help="Relay client smoke tests")
    client_sub = client_p.add_subparsers(dest="client_cmd")
    turn_p = client_sub.add_parser("turn", help="POST /v1/turn")
    turn_p.add_argument("message", help="User message")
    turn_p.add_argument("--url", default="http://127.0.0.1:8787")
    turn_p.add_argument("--secret", default="")
    turn_p.add_argument("--channel", default="cli")
    turn_p.add_argument("--user", default="local")
    turn_p.add_argument("--provider", default="")
    turn_p.add_argument("--stream", action="store_true")
    health_p = client_sub.add_parser("health", help="GET /health")
    health_p.add_argument("--url", default="http://127.0.0.1:8787")
    return parser


def _channel_main(args: argparse.Namespace) -> int:
    from .channel_client import (
        ChannelClient,
        _load_hubs,
        connect_hub,
        disconnect_hub,
    )

    cmd = getattr(args, "channel_cmd", "") or ""
    if cmd == "connect":
        try:
            row = connect_hub(args.hub_url, args.credential, label=args.label)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        out = {k: v for k, v in row.items()}
        print(json.dumps({"ok": True, **out}, indent=2, ensure_ascii=False))
        return 0
    if cmd == "disconnect":
        if not disconnect_hub(args.hub_id):
            print(f"Error: unknown hub: {args.hub_id!r}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "hub": args.hub_id}))
        return 0
    if cmd == "list":
        rows = _load_hubs()
        print(json.dumps({"ok": True, "hubs": rows}, indent=2, ensure_ascii=False))
        return 0
    if cmd == "run":
        ids = [args.hub] if args.hub else list(_load_hubs())
        if not ids:
            print("Error: no stored hub links — run `channel connect` first",
                  file=sys.stderr)
            return 2
        config = RelayConfig.from_env()
        clients = []
        for hub_id in ids:
            try:
                c = ChannelClient(hub_id, config=config)
            except Exception as exc:  # noqa: BLE001
                print(f"Error: hub {hub_id!r}: {exc}", file=sys.stderr)
                return 1
            clients.append(c)
            threading.Thread(target=c.run_forever, daemon=True).start()
            print(f"channel up: {hub_id} -> {c.hub_url}", file=sys.stderr)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            for c in clients:
                c.stop()
        return 0
    print("Error: channel subcommand required (connect|disconnect|list|run)",
          file=sys.stderr)
    return 2


def _client_main(args: argparse.Namespace) -> int:
    if args.client_cmd == "health":
        client = GatewayClient(base_url=args.url)
        print(json.dumps(client.health(), indent=2))
        return 0
    if args.client_cmd == "turn":
        client = GatewayClient(
            base_url=args.url,
            secret=args.secret,
            channel=args.channel,
            user=args.user,
            provider=args.provider,
        )
        if args.stream:
            for event in client.stream(args.message):
                print(json.dumps(event, ensure_ascii=False))
            return 0
        data = client.turn(args.message)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0 if int(data.get("returncode", 1)) == 0 else 1
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    # stdout must stay clean on the stdio ACP surface — skip the update probe.
    acp_cmd = getattr(args, "command", "") in ("acp-serve", "acp-run")
    if not getattr(args, "help_json", False) and not acp_cmd:
        try:
            from .updates import check_for_updates
            check_for_updates("agents-relay", __version__)
        except Exception:
            pass
    if getattr(args, "help_json", False):
        print(json.dumps(_help_json(), indent=2))
        return 0
    if args.command == "acp-serve":
        from .acp import serve_acp

        return serve_acp(RelayConfig.from_env())
    if args.command == "acp-run":
        from .acp import acp_loop_main

        return acp_loop_main(args, RelayConfig.from_env())
    if args.command == "client":
        return _client_main(args)
    if args.command == "channel":
        return _channel_main(args)
    if args.command == "send":
        return _cmd_send(args)
    if args.command == "inject":
        return _cmd_inject(args)
    if args.command == "discord-send":
        return _cmd_discord_send(args)
    if args.command == "discord-inject":
        return _cmd_discord_inject(args)
    if args.command == "approve":
        return _cmd_approve(args)
    if args.command != "serve":
        build_parser().print_help()
        return 0

    config = RelayConfig.from_env()
    server = serve_http(config)
    bound_url = str(getattr(server, "relay_url", "") or "").strip()
    if bound_url:
        if not config.relay_url:
            config = replace(config, relay_url=bound_url)
        os.environ.setdefault("AGENTS_RELAY_URL", bound_url)
    tg_thread = None
    tg_stop = None
    discord_thread = None
    discord_stop = None
    if not args.no_telegram and config.telegram_bot_token:
        tg_thread, tg_stop = start_telegram_thread(config)
    if not args.no_discord and config.discord_bot_token:
        discord_thread, discord_stop = start_discord_thread(config)

    # Outbound persistent channels to paired hubs (agents-relay#40).
    hub_clients: list = []
    try:
        from .channel_client import run_hub_channels

        hub_clients = run_hub_channels(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("hub channel boot failed: %s", exc)

    stopping = threading.Event()

    def _shutdown(*_sig) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("shutting down")
        if tg_stop is not None:
            tg_stop.set()
        if discord_stop is not None:
            discord_stop.set()
        shutdown_jobs()
        # Signal handlers run on the main thread, which is inside serve_forever().
        # server.shutdown() waits for serve_forever() to return, so calling it here
        # would deadlock. Ask from another thread; serve_forever() exits within one poll.
        threading.Thread(target=server.shutdown, name="relay-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown()
    finally:
        server.server_close()
        for c in hub_clients:
            c.stop()
        if tg_thread is not None:
            tg_thread.join(timeout=2)
        if discord_thread is not None:
            discord_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
