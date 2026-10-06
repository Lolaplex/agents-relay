"""Telegram long-poll adapter (urllib only)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from .approvals import (
    APPROVAL_ID_RE,
    approval_keyboard,
    create_pending,
    format_approval_html,
    parse_callback_data,
    read_approval,
    record_decision,
    status_exit_code,
    wait_for_decision,
)
from .attachments import safe_filename
from .config import RelayConfig
from .jobs import Job, JobRegistry, get_registry
from .loop_client import LoopTurnResult, run_loop_turn
from .state import inbox_dir
from .telegram_format import (
    extract_traces,
    format_telegram_html,
    humanize_status,
    split_telegram_html,
    status_html,
    visible_reply,
)

log = logging.getLogger("agents_relay.telegram")

API_BASE = "https://api.telegram.org"
_CONTROL_COMMANDS = {"jobs", "stop"}


def _api_url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> dict:
    body: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    try:
        return _post_json(_api_url(token, "sendMessage"), body)
    except urllib.error.HTTPError as exc:
        if parse_mode and exc.code == 400:
            body.pop("parse_mode", None)
            body.pop("reply_markup", None)
            body["text"] = _strip_html_tags(text)[:4096] or "(empty)"
            return _post_json(_api_url(token, "sendMessage"), body)
        raise


def edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> dict:
    body: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
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
            body.pop("reply_markup", None)
            body["text"] = _strip_html_tags(text)[:4096] or "(empty)"
            return _post_json(_api_url(token, "editMessageText"), body)
        raise


def deliver_html(
    token: str,
    chat_id: int,
    html: str,
    *,
    thinking_id: int | None = None,
    reply_markup: dict | None = None,
) -> None:
    """Send HTML, splitting on paragraph/code boundaries. Edits the thinking bubble first."""
    parts = split_telegram_html(html or "")
    if not parts:
        parts = ["(empty)"]
    first, *rest = parts
    sent_first = False
    if thinking_id:
        try:
            edit_message(
                token,
                chat_id,
                thinking_id,
                first,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            sent_first = True
        except Exception:
            sent_first = False
    if not sent_first:
        send_message(token, chat_id, first, parse_mode="HTML", reply_markup=reply_markup)
    for part in rest:
        send_message(token, chat_id, part, parse_mode="HTML")


def chat_allowed(chat_id: int, config: RelayConfig) -> bool:
    """Allowlist membership. Empty allowlist denies unless AGENTS_RELAY_ALLOW_ANYONE=1."""
    try:
        numeric = int(chat_id)
    except (TypeError, ValueError):
        return False
    if numeric in config.telegram_allowed_chat_ids:
        return True
    if not config.telegram_allowed_chat_ids and config.allow_anyone:
        return True
    return False


def send_to_user(
    *,
    token: str,
    chat_id: int,
    text: str,
    allowed: tuple[int, ...],
    allow_anyone: bool = False,
) -> dict:
    """One-shot outbound Telegram message. Enforces allowlist. No token in errors."""
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("chat_id is required")
    permitted = chat_id in allowed or (not allowed and allow_anyone)
    if not permitted:
        raise PermissionError("chat_id not in TELEGRAM_ALLOWED_CHAT_IDS")
    html = format_telegram_html(text, ())
    parts = split_telegram_html(html)
    last: dict = {}
    for part in parts or ["(empty)"]:
        last = send_message(token, chat_id, part, parse_mode="HTML")
    return last


def _download_telegram_file(
    token: str,
    file_id: str,
    dest_dir: Path,
    *,
    dest_name: str | None = None,
) -> Path | None:
    try:
        info = _post_json(_api_url(token, "getFile"), {"file_id": file_id})
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            return None
        file_url = f"{API_BASE}/file/bot{token}/{file_path}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_name:
            name = safe_filename(str(dest_name))
        else:
            ext = Path(str(file_path)).suffix or ".bin"
            name = f"file{ext}"
        target = dest_dir / f"{uuid.uuid4().hex[:8]}_{name}"
        req = urllib.request.Request(file_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            target.write_bytes(resp.read())
        return target
    except Exception:
        log.warning("Failed to download telegram file")
        return None


def _collect_attachments(message: dict, token: str, config: RelayConfig) -> list[str]:
    dest = inbox_dir(config)
    paths: list[str] = []
    photo_list = message.get("photo") or []
    if photo_list and isinstance(photo_list, list):
        largest = photo_list[-1]
        file_id = largest.get("file_id") if isinstance(largest, dict) else None
        if file_id:
            saved = _download_telegram_file(token, str(file_id), dest, dest_name="photo.jpg")
            if saved is not None:
                paths.append(str(saved))
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        saved = _download_telegram_file(
            token,
            str(document["file_id"]),
            dest,
            dest_name=str(document.get("file_name") or "document"),
        )
        if saved is not None:
            paths.append(str(saved))
    return paths


def inbound_payload(result: LoopTurnResult) -> dict:
    """Caller JSON: visible answer + humanized traces. No tokens, no raw CLI dump."""
    _, err_traces = extract_traces(result.stderr or "")
    traces: list[str] = []
    seen: set[str] = set()
    for raw in err_traces:
        human = humanize_status(str(raw))
        if human and human not in seen:
            seen.add(human)
            traces.append(human)
    reply = visible_reply(result.reply or "", err_traces)
    return {
        "reply": reply,
        "traces": traces,
        "session": result.session,
        "user_id": result.user_id,
        "alias": result.alias,
        "returncode": result.returncode,
    }


def handle_inbound_text(
    *,
    chat_id: int,
    text: str,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    user: str | None = None,
    attachments: list[str] | None = None,
    on_pid: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> LoopTurnResult:
    """Same Telegram lifecycle as a poll text update: thinking edit, then final."""
    token = config.telegram_bot_token
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    loop_user = (user or str(chat_id)).strip() or str(chat_id)

    thinking_msg = send_message(token, chat_id, status_html("thinking..."), parse_mode="HTML")
    thinking_id = (thinking_msg.get("result") or {}).get("message_id") if isinstance(thinking_msg, dict) else None

    last_status_time = [0.0]
    last_status_val = [""]

    def handle_status(status_text: str) -> None:
        if not thinking_id:
            return
        cleaned = humanize_status(status_text)
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

    raw_lower = (text or "").strip().lower()
    is_new = raw_lower in ("/new", "/reset") or raw_lower.startswith(("/new ", "/reset "))
    turn_text = text
    if is_new:
        turn_text = re.sub(r"^/(new|reset)\s*", "", text, flags=re.IGNORECASE).strip()
        if not turn_text:
            turn_text = "Hallo"

    runner = on_turn or run_loop_turn
    result = LoopTurnResult(reply="", session="", user_id="", alias="", returncode=1, stderr="")
    try:
        runner_kwargs = {
            "channel": "telegram",
            "user": loop_user,
            "message": turn_text,
            "new_session": is_new,
            "on_status": handle_status,
            "attachments": list(attachments or ()),
            "on_pid": on_pid,
        }
        try:
            result = runner(**runner_kwargs)
        except TypeError:
            slim = {key: value for key, value in runner_kwargs.items() if key not in ("attachments", "on_pid")}
            try:
                result = runner(**slim)
            except TypeError:
                slim.pop("on_status", None)
                try:
                    result = runner(**slim)
                except TypeError:
                    result = runner(channel="telegram", user=loop_user, message=turn_text)
        _, err_traces = extract_traces(result.stderr or "")
        if cancelled and cancelled():
            reply_html = format_telegram_html("Stopped.", ())
        elif not (result.reply or "").strip() and result.returncode != 0:
            log.error("Loop turn failed (rc=%d): %s", result.returncode, result.stderr)
            err_lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
            err_msg = ""
            for line in reversed(err_lines):
                if line and not line.startswith("Traceback"):
                    err_msg = line
                    break
            if not err_msg:
                err_msg = err_lines[-1] if err_lines else f"Code {result.returncode}"
            reply_html = format_telegram_html(f"Turn failed ({err_msg}).", err_traces)
        else:
            reply_html = format_telegram_html(result.reply, err_traces)
        if not _strip_html_tags(reply_html):
            reply_html = format_telegram_html("", err_traces)
    except Exception as exc:
        log.exception("Loop turn error")
        line = str(exc).splitlines()[0].strip() if str(exc).strip() else exc.__class__.__name__
        if len(line) > 200:
            line = line[:200] + "..."
        reply_html = format_telegram_html(f"Turn error: {line}", ())
        result = LoopTurnResult(
            reply=f"Turn error: {line}",
            session="",
            user_id="",
            alias="",
            returncode=1,
            stderr="",
        )

    deliver_html(token, chat_id, reply_html, thinking_id=thinking_id if isinstance(thinking_id, int) else None)
    return result


def inject_text(
    *,
    chat_id: int,
    text: str,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    user: str | None = None,
) -> LoopTurnResult:
    """Allowlisted synthetic inbound: same handler as poll, plus caller payload."""
    if not chat_id:
        raise ValueError("chat_id is required")
    if not chat_allowed(chat_id, config):
        raise PermissionError("chat_id not in TELEGRAM_ALLOWED_CHAT_IDS")
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("text is required")
    return handle_inbound_text(
        chat_id=chat_id,
        text=stripped,
        config=config,
        on_turn=on_turn,
        user=user or str(chat_id),
    )


def process_update(
    update: dict,
    *,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    job: Job | None = None,
) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    if not chat_id or not chat_allowed(chat_id, config):
        return

    text = str(message.get("text") or message.get("caption") or "").strip()
    token = config.telegram_bot_token
    if not token:
        return
    attachments = _collect_attachments(message, token, config)
    if not text and not attachments:
        return
    if not text:
        text = "(attachment)"

    # Numeric chat id: harness substitutes this into `approve --user {user}`.
    handle_inbound_text(
        chat_id=chat_id,
        text=text,
        config=config,
        on_turn=on_turn,
        user=str(chat_id),
        attachments=attachments,
        on_pid=job.bind_pid if job is not None else None,
        cancelled=job.cancel.is_set if job is not None else None,
    )


def parse_control_command(text: str) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    head, _, rest = raw.partition(" ")
    name = head[1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name.lower()
    if name not in _CONTROL_COMMANDS:
        return None
    return name, rest.strip()


def _jobs_text(jobs: list[Job]) -> str:
    if not jobs:
        return "No running jobs."
    lines = [f"Jobs ({len(jobs)}):"]
    for job in jobs:
        state = "queued" if not job.running else f"pid {job.pid or '?'}"
        preview = job.preview or ""
        lines.append(f"• {job.id} {state} — {preview}")
    return "\n".join(lines)


def _handle_control(
    command: tuple[str, str],
    chat_id: int,
    config: RelayConfig,
    registry: JobRegistry,
) -> None:
    token = config.telegram_bot_token
    if not token:
        return
    name, rest = command
    if name == "jobs":
        body = _jobs_text(registry.list_chat(chat_id))
        deliver_html(token, chat_id, format_telegram_html(body))
        return
    job_id = rest.split()[0] if rest else None
    stopped = registry.stop(chat_id, job_id)
    if not stopped:
        body = "No matching job." if job_id else "No running jobs."
    elif job_id:
        body = f"Stopped {stopped[0]}."
    else:
        body = "Stopped: " + ", ".join(stopped) + "."
    deliver_html(token, chat_id, format_telegram_html(body))


def answer_callback(token: str, callback_id: str, text: str) -> None:
    if not token or not callback_id:
        return
    try:
        _post_json(
            _api_url(token, "answerCallbackQuery"),
            {"callback_query_id": callback_id, "text": text[:180]},
        )
    except Exception:
        log.warning("answerCallbackQuery failed")


def handle_callback_query(callback: dict, *, config: RelayConfig) -> None:
    token = config.telegram_bot_token
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    try:
        from_id = int((callback.get("from") or {}).get("id") or 0)
    except (TypeError, ValueError):
        from_id = 0
    message = callback.get("message") or {}
    try:
        chat_id = int((message.get("chat") or {}).get("id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    message_id = message.get("message_id")

    parsed = parse_callback_data(data)
    if parsed is None:
        answer_callback(token, callback_id, "Unknown action")
        return
    ident, decision = parsed
    if not chat_allowed(from_id, config):
        answer_callback(token, callback_id, "Not allowed")
        log.warning("approval callback rejected for non-allowlisted user %s", from_id)
        return
    if not chat_id:
        answer_callback(token, callback_id, "Expired")
        return
    record = read_approval(config, ident)
    if not record:
        answer_callback(token, callback_id, "Expired")
        return
    if chat_id and int(record.get("chat_id") or 0) != chat_id:
        answer_callback(token, callback_id, "Not allowed")
        return
    if record.get("status") != "pending":
        answer_callback(token, callback_id, "Already decided")
        return
    note = "approved" if decision == "approved" else "denied"
    updated = record_decision(
        config,
        ident,
        status=decision,
        note=note,
        decided_by=from_id,
        expected_chat_id=int(record.get("chat_id") or 0),
    )
    if updated is None:
        answer_callback(token, callback_id, "Already decided")
        return
    answer_callback(token, callback_id, "Approved" if decision == "approved" else "Denied")
    if token and isinstance(message_id, int) and chat_id:
        prev = html_escape_text(str(message.get("text") or "Approval"))
        label = "Approved" if decision == "approved" else "Denied"
        merged = f"{prev}\n\n<b>{label}</b>"
        try:
            parts = split_telegram_html(merged)
            edit_message(
                token,
                chat_id,
                message_id,
                parts[0] if parts else merged,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )
        except Exception:
            log.warning("could not edit approval message")


def html_escape_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def dispatch_update(
    update: dict,
    *,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    registry: JobRegistry | None = None,
) -> None:
    """Handle one Telegram update. Turns run on worker threads; callbacks do not."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        handle_callback_query(callback, config=config)
        return
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat") or {}
    try:
        chat_id = int(chat.get("id") or 0)
    except (TypeError, ValueError):
        return
    if not chat_id or not chat_allowed(chat_id, config):
        return
    text = str(message.get("text") or "").strip()
    command = parse_control_command(text) if text.startswith("/") else None
    reg = registry or get_registry(config)
    if command is not None and not message.get("photo") and not message.get("document"):
        _handle_control(command, chat_id, config, reg)
        return
    preview = text or str(message.get("caption") or "").strip() or "(attachment)"

    def _run(job: Job) -> None:
        process_update(update, config=config, on_turn=on_turn, job=job)

    job = reg.submit(chat_id, preview, _run)
    if job is None and not reg.closed:
        token = config.telegram_bot_token
        if token:
            try:
                send_message(
                    token,
                    chat_id,
                    "Too many messages waiting. Send /jobs or /stop.",
                    parse_mode="",
                )
            except Exception:
                log.warning("failed to send queue-full notice")


def run_approve(
    *,
    chat_id: int,
    timeout: float,
    request: dict,
    config: RelayConfig,
    poll_interval: float = 0.2,
) -> tuple[int, str]:
    """Send Approve/Deny and block until the poll loop records a decision."""
    ident = str(request.get("id") or "").strip()
    if not APPROVAL_ID_RE.match(ident):
        return 2, "invalid approval id"
    if not config.telegram_bot_token:
        return 2, "telegram token not set"
    if not chat_allowed(chat_id, config):
        return 2, "chat not allowlisted"
    existing = read_approval(config, ident)
    if existing and existing.get("status") == "approved":
        return 0, str(existing.get("note") or "approved")
    if existing and existing.get("status") == "denied":
        return 1, str(existing.get("note") or "denied")
    try:
        create_pending(config, chat_id, request)
        send_message(
            config.telegram_bot_token,
            chat_id,
            format_approval_html(request),
            parse_mode="HTML",
            reply_markup=approval_keyboard(ident),
        )
    except Exception:
        log.warning("approval prompt failed for %s", ident)
        try:
            record_decision(
                config,
                ident,
                status="timeout",
                note="telegram send failed",
                decided_by=None,
                expected_chat_id=chat_id,
            )
        except Exception:
            pass
        return 2, "telegram send failed"
    status, note = wait_for_decision(
        config,
        ident,
        float(timeout),
        poll_interval=poll_interval,
    )
    return status_exit_code(status), note or status


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
    if not config.telegram_allowed_chat_ids and not config.allow_anyone:
        log.error(
            "REFUSING Telegram adapter: TELEGRAM_ALLOWED_CHAT_IDS is empty. "
            "Set allowlisted chat ids, or AGENTS_RELAY_ALLOW_ANYONE=1 to opt in."
        )
        return
    if not config.telegram_allowed_chat_ids and config.allow_anyone:
        log.warning(
            "WARNING: AGENTS_RELAY_ALLOW_ANYONE=1 with an empty TELEGRAM_ALLOWED_CHAT_IDS. "
            "Any Telegram user can run turns and approve tools."
        )
    registry = get_registry(config)
    offset = 0
    stop = stop_event or threading.Event()
    while not stop.is_set():
        params = {
            "timeout": config.telegram_poll_timeout,
            "offset": offset,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        url = _api_url(token, "getUpdates") + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=config.telegram_poll_timeout + 10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            log.warning("getUpdates failed")
            if stop.wait(2):
                break
            continue
        for update in data.get("result") or []:
            offset = int(update.get("update_id", offset)) + 1
            try:
                dispatch_update(update, config=config, on_turn=on_turn, registry=registry)
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
