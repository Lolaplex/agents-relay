"""Telegram long-poll adapter (urllib only)."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn
from .telegram_format import extract_traces, format_telegram_html, status_html

log = logging.getLogger("agents_relay.telegram")

API_BASE = "https://api.telegram.org"


def _api_url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


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
            body["text"] = _strip_html_tags(text)[:4096] or "(empty)"
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
        err_msg = ""
        try:
            err_msg = exc.read().decode("utf-8")
        except Exception:
            pass
        if "message is not modified" in err_msg.lower():
            return {"ok": True, "result": {"message_id": message_id}}
        if parse_mode and exc.code == 400:
            body.pop("parse_mode", None)
            body["text"] = _strip_html_tags(text)[:4096] or "(empty)"
            return _post_json(_api_url(token, "editMessageText"), body)
        raise


def _allowed(chat_id: int, allowed: tuple[int, ...]) -> bool:
    if not allowed:
        return True
    return chat_id in allowed


def send_to_user(
    *,
    token: str,
    chat_id: int,
    text: str,
    allowed: tuple[int, ...],
) -> dict:
    """One-shot outbound Telegram message. Enforces allowlist. No token in errors."""
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("chat_id is required")
    if not _allowed(chat_id, allowed):
        raise PermissionError("chat_id not in TELEGRAM_ALLOWED_CHAT_IDS")
    html = format_telegram_html(text, ())
    return send_message(token, chat_id, html, parse_mode="HTML")


def _download_telegram_file(token: str, file_id: str, dest_dir: Path) -> Path | None:
    try:
        info = _post_json(_api_url(token, "getFile"), {"file_id": file_id})
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            return None
        file_url = f"{API_BASE}/file/bot{token}/{file_path}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(file_path).suffix or ".jpg"
        target = dest_dir / f"photo_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
        req = urllib.request.Request(file_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            target.write_bytes(resp.read())
        return target
    except Exception as exc:
        log.warning("Failed to download photo %s: %s", file_id, exc)
        return None


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

    text = str(message.get("text") or message.get("caption") or "").strip()
    photo_list = message.get("photo") or []
    image_path: str | None = None
    token = config.telegram_bot_token

    if photo_list and isinstance(photo_list, list) and token:
        largest = photo_list[-1]
        file_id = largest.get("file_id") if isinstance(largest, dict) else None
        if file_id:
            override_home = os.environ.get("AGENTS_HOME")
            inbox_dir = (Path(override_home) if override_home else (Path.home() / ".agents")) / "inbox"
            saved = _download_telegram_file(token, file_id, inbox_dir)
            if saved:
                image_path = str(saved)

    if not text and not image_path:
        return

    if not text and image_path:
        text = f"[Photo received: {image_path}]"
    elif image_path:
        text = f"{text}\n\n[Photo attached: {image_path}]"

    user = str((message.get("from") or {}).get("username") or chat_id)
    if not token:
        return

    thinking_msg = send_message(token, chat_id, status_html("thinking..."), parse_mode="HTML")
    thinking_id = (thinking_msg.get("result") or {}).get("message_id") if isinstance(thinking_msg, dict) else None

    last_status_time = [0.0]
    last_status_val = [""]

    def handle_status(status_text: str) -> None:
        if not thinking_id:
            return
        cleaned = status_text.strip()
        if not cleaned or cleaned == last_status_val[0]:
            return
        now = time.monotonic()
        if now - last_status_time[0] < 1.5:
            return
        last_status_time[0] = now
        last_status_val[0] = cleaned
        try:
            edit_message(token, chat_id, thinking_id, status_html(cleaned), parse_mode="HTML")
        except Exception:
            pass

    runner = on_turn or run_loop_turn
    try:
        try:
            result = runner(channel="telegram", user=user, message=text, on_status=handle_status)
        except TypeError:
            result = runner(channel="telegram", user=user, message=text)
        _, err_traces = extract_traces(result.stderr or "")
        if not (result.reply or "").strip() and result.returncode != 0:
            log.error("Loop turn failed (rc=%d): %s", result.returncode, result.stderr)
            err_msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"Code {result.returncode}"
            reply_html = format_telegram_html(f"Turn failed ({err_msg}).", err_traces)
        else:
            reply_html = format_telegram_html(result.reply, err_traces)
    except Exception as exc:
        log.exception("Loop turn error: %s", exc)
        reply_html = format_telegram_html(f"Turn error: {exc}", ())

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
