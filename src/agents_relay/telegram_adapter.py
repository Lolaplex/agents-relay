"""Telegram long-poll adapter (urllib only)."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn
from .telegram_format import format_telegram_html, status_html

log = logging.getLogger("agents_relay.telegram")

API_BASE = "https://api.telegram.org"


def _api_url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token: str, chat_id: int, text: str, *, parse_mode: str = "HTML") -> dict:
    body = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    try:
        return _post_json(_api_url(token, "sendMessage"), body)
    except urllib.error.HTTPError as exc:
        if parse_mode and exc.code == 400:
            body.pop("parse_mode", None)
            return _post_json(_api_url(token, "sendMessage"), body)
        raise


def edit_message(token: str, chat_id: int, message_id: int, text: str, *, parse_mode: str = "HTML") -> dict:
    body = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    try:
        return _post_json(_api_url(token, "editMessageText"), body)
    except urllib.error.HTTPError as exc:
        if parse_mode and exc.code == 400:
            body.pop("parse_mode", None)
            return _post_json(_api_url(token, "editMessageText"), body)
        raise


def _allowed(chat_id: int, allowed: tuple[int, ...]) -> bool:
    if not allowed:
        return True
    return chat_id in allowed


def process_update(
    update: dict,
    *,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    if not chat_id or not _allowed(chat_id, config.telegram_allowed_chat_ids):
        return
    text = str(message.get("text") or "").strip()
    if not text:
        return
    user = str((message.get("from") or {}).get("username") or chat_id)
    token = config.telegram_bot_token
    if not token:
        return

    thinking_msg = send_message(token, chat_id, status_html("thinking..."), parse_mode="HTML")
    thinking_id = (thinking_msg.get("result") or {}).get("message_id") if isinstance(thinking_msg, dict) else None

    runner = on_turn or run_loop_turn
    result = runner(channel="telegram", user=user, message=text)
    reply_html = format_telegram_html(result.reply, ())

    if thinking_id:
        try:
            edit_message(token, chat_id, thinking_id, reply_html, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message(token, chat_id, reply_html, parse_mode="HTML")


def telegram_poll_loop(
    config: RelayConfig,
    *,
    stop_event: threading.Event | None = None,
    on_turn: Callable[..., LoopTurnResult] | None = None,
) -> None:
    token = config.telegram_bot_token
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; telegram poll disabled")
        return
    offset = 0
    stop = stop_event or threading.Event()
    while not stop.is_set():
        params = {
            "timeout": config.telegram_poll_timeout,
            "offset": offset,
            "allowed_updates": json.dumps(["message"]),
        }
        url = _api_url(token, "getUpdates") + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=config.telegram_poll_timeout + 10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("getUpdates failed: %s", exc)
            time.sleep(2)
            continue
        for update in data.get("result") or []:
            offset = int(update.get("update_id", offset)) + 1
            try:
                process_update(update, config=config, on_turn=on_turn)
            except Exception:
                log.exception("telegram update failed")


def start_telegram_thread(
    config: RelayConfig,
    *,
    on_turn: Callable[..., LoopTurnResult] | None = None,
) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=telegram_poll_loop,
        kwargs={"config": config, "stop_event": stop_event, "on_turn": on_turn},
        name="telegram-poll",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
