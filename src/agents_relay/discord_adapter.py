"""Discord real-bot adapter (Gateway + REST)."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from agents_workspace import Host

from . import __version__
from .approvals import (
    APPROVAL_ID_RE,
    create_pending,
    decision_matches_request,
    parse_callback_data,
    read_approval,
    record_decision,
    status_exit_code,
    wait_for_decision,
)
from .attachments import MAX_ATTACHMENT_BYTES, apply_notes, read_capped, safe_filename
from .chat_control import jobs_text, parse_control_command
from .config import RelayConfig
from .discord_format import DISCORD_MESSAGE_LIMIT, split_discord_markdown
from .jobs import Job, JobRegistry, get_registry
from .loop_client import LoopTurnResult, run_loop_turn
from .state import inbox_dir, read_json, write_json_atomic
from .telegram_format import extract_traces, humanize_status, visible_reply

log = logging.getLogger("agents_relay.discord")

API_BASE = "https://discord.com/api/v10"
GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
_THINKING = "_thinking._"
_THINKING_ANIMATION_SEC = 2.5


def _prefs_path() -> Path:
    return Host.from_env().clients / "discord.json"


def _load_all_prefs() -> dict:
    path = _prefs_path()
    if path.is_file():
        return read_json(path) or {}
    return {}


def _chat_prefs(chat_id: int) -> dict:
    row = _load_all_prefs().get(str(chat_id))
    if isinstance(row, str):
        return {"persona": row}
    return dict(row) if isinstance(row, dict) else {}


def _set_chat_pref(chat_id: int, key: str, value: str) -> None:
    path = _prefs_path()
    data = dict(_load_all_prefs())
    row = data.get(str(chat_id))
    if not isinstance(row, dict):
        row = {"persona": row} if isinstance(row, str) else {}
        data[str(chat_id)] = row
    if value:
        row[key] = value
    else:
        row.pop(key, None)
    if not row:
        data.pop(str(chat_id), None)
    write_json_atomic(path, data)


def _chat_persona(chat_id: int) -> str:
    return str(_chat_prefs(chat_id).get("persona") or "")


def _chat_ws(chat_id: int) -> str:
    return str(_chat_prefs(chat_id).get("ws") or "")


def _chat_model(chat_id: int) -> tuple[str, str]:
    raw = str(_chat_prefs(chat_id).get("model") or "")
    provider, _, model = raw.partition(":")
    return provider.strip(), model.strip()


def _resolve_ws(chat_id: int):
    from .workspaces import get_workspace, select_workspace

    ws_id = _chat_ws(chat_id)
    if ws_id:
        return get_workspace(ws_id)
    return select_workspace()


def _api_request(
    token: str,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    auth: bool = True,
    timeout: float = 30.0,
    retries: int = 2,
) -> dict:
    url = API_BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        headers = {
            "Accept": "application/json",
            "User-Agent": f"agents-relay (https://github.com/Lolaplex/agents-relay, {__version__})",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bot {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                retry_after = 1.0
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                    retry_after = float(body.get("retry_after") or 1.0)
                except Exception:
                    pass
                time.sleep(max(0.1, min(5.0, retry_after)))
                continue
            raise
    return {}


def gateway_bot_info(token: str) -> dict:
    return _api_request(token, "GET", "/gateway/bot")


def send_message(
    token: str,
    channel_id: int,
    text: str,
    *,
    components: list[dict] | None = None,
) -> dict:
    body: dict = {
        "content": text[:DISCORD_MESSAGE_LIMIT] or "(empty)",
        "allowed_mentions": {"parse": []},
    }
    if components is not None:
        body["components"] = components
    return _api_request(token, "POST", f"/channels/{channel_id}/messages", body)


def edit_message(
    token: str,
    channel_id: int,
    message_id: int | str,
    text: str,
    *,
    components: list[dict] | None = None,
) -> dict:
    body: dict = {
        "content": text[:DISCORD_MESSAGE_LIMIT] or "(empty)",
        "allowed_mentions": {"parse": []},
    }
    if components is not None:
        body["components"] = components
    try:
        return _api_request(token, "PATCH", f"/channels/{channel_id}/messages/{message_id}", body)
    except urllib.error.HTTPError as exc:
        try:
            err = exc.read().decode("utf-8").lower()
        except Exception:
            err = ""
        if "not modified" in err or "same content" in err:
            return {"id": str(message_id)}
        raise


def delete_message(token: str, channel_id: int, message_id: int | str) -> dict:
    try:
        return _api_request(token, "DELETE", f"/channels/{channel_id}/messages/{message_id}")
    except Exception:
        return {}


def deliver_markdown(
    token: str,
    channel_id: int,
    text: str,
    *,
    thinking_id: int | str | None = None,
    components: list[dict] | None = None,
) -> None:
    parts = split_discord_markdown(text or "")
    if not parts:
        parts = ["(empty)"]
    first, *rest = parts
    sent_first = False
    if thinking_id:
        try:
            edit_message(token, channel_id, thinking_id, first, components=components)
            sent_first = True
        except Exception:
            sent_first = False
    if not sent_first:
        send_message(token, channel_id, first, components=components)
    for part in rest:
        send_message(token, channel_id, part)


def channel_allowed(channel_id: int, config: RelayConfig) -> bool:
    try:
        numeric = int(channel_id)
    except (TypeError, ValueError):
        return False
    if numeric in config.discord_allowed_channel_ids:
        return True
    if not config.discord_allowed_channel_ids and config.allow_anyone:
        return True
    return False


def user_allowed(user_id: int, config: RelayConfig) -> bool:
    if not config.discord_allowed_user_ids:
        return bool(config.allow_anyone)
    try:
        return int(user_id) in config.discord_allowed_user_ids
    except (TypeError, ValueError):
        return False


def send_to_user(
    *,
    token: str,
    channel_id: int,
    text: str,
    allowed: tuple[int, ...],
    allow_anyone: bool = False,
) -> dict:
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is not set")
    if not channel_id:
        raise ValueError("channel_id is required")
    permitted = channel_id in allowed or (not allowed and allow_anyone)
    if not permitted:
        raise PermissionError("channel_id not in DISCORD_ALLOWED_CHANNEL_IDS")
    last: dict = {}
    for part in split_discord_markdown(text) or ["(empty)"]:
        last = send_message(token, channel_id, part)
    if (text or "").strip():
        try:
            from agents_traces.assemble import record_message
            from agents_traces.identity import IdentityStore

            resolved = IdentityStore().resolve(channel="discord", user=str(channel_id), max_idle_hours=0)
            record_message(
                resolved.session.id,
                "assistant",
                str(text).strip(),
                channel="discord",
                user=str(channel_id),
                user_id=resolved.user.id,
                workspace=resolved.session.workspace,
                persona=resolved.session.persona,
            )
        except Exception:
            pass
    return last


def _download_discord_attachment(attachment: dict, dest_dir: Path) -> tuple[Path | None, str]:
    name = safe_filename(str(attachment.get("filename") or "attachment"))
    try:
        size = int(attachment.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_ATTACHMENT_BYTES:
        return None, f"Attachment dropped: {name} is larger than {MAX_ATTACHMENT_BYTES} bytes."
    url = str(attachment.get("url") or attachment.get("proxy_url") or "")
    if not url.startswith("https://"):
        return None, f"Attachment dropped: {name} has no downloadable HTTPS URL."
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{uuid.uuid4().hex[:8]}_{name}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            blob = read_capped(resp, MAX_ATTACHMENT_BYTES)
        if blob is None:
            return None, f"Attachment dropped: {name} is larger than {MAX_ATTACHMENT_BYTES} bytes."
        target.write_bytes(blob)
        return target, ""
    except Exception:
        log.warning("Failed to download discord attachment")
        return None, f"Attachment dropped: could not save {name}."


def _collect_attachments(message: dict, config: RelayConfig) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    notes: list[str] = []
    rows = message.get("attachments") or []
    if not isinstance(rows, list):
        return paths, notes
    for row in rows:
        if not isinstance(row, dict):
            continue
        saved, note = _download_discord_attachment(row, inbox_dir(config))
        if saved is not None:
            paths.append(str(saved))
        if note:
            notes.append(note)
    return paths, notes


def inbound_payload(result: LoopTurnResult) -> dict:
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
    channel_id: int,
    text: str,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    user: str | None = None,
    attachments: list[str] | None = None,
    on_pid: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    attachment_notes: list[str] | None = None,
    interrupt_file: str = "",
) -> LoopTurnResult:
    token = config.discord_bot_token
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is not set")
    loop_user = (user or str(channel_id)).strip() or str(channel_id)

    thinking_msg = send_message(token, channel_id, _THINKING)
    thinking_id = thinking_msg.get("id") if isinstance(thinking_msg, dict) else None

    anim_done = threading.Event()
    phase = [random.randint(0, 2)]

    def _animate() -> None:
        while not anim_done.wait(_THINKING_ANIMATION_SEC):
            if not thinking_id:
                continue
            phase[0] = (phase[0] + 1) % 3
            dots = "." * (phase[0] + 1)
            try:
                edit_message(token, channel_id, thinking_id, f"_thinking{dots}_")
            except Exception:
                pass

    if thinking_id:
        threading.Thread(target=_animate, daemon=True).start()

    def handle_status(status_text: str) -> None:
        return

    raw_lower = (text or "").strip().lower()
    is_new = raw_lower in ("/new", "/reset") or raw_lower.startswith(("/new ", "/reset "))
    turn_text = text
    if is_new:
        turn_text = re.sub(r"^/(new|reset)\s*", "", text, flags=re.IGNORECASE).strip()
        if not turn_text:
            turn_text = "Hallo"

    runner = on_turn or run_loop_turn
    persona = _chat_persona(channel_id)
    ws_pref = _chat_ws(channel_id)
    result = LoopTurnResult(reply="", session="", user_id="", alias="", returncode=1, stderr="")
    try:
        runner_kwargs = {
            "channel": "discord",
            "user": loop_user,
            "message": turn_text,
            "new_session": is_new,
            "on_status": handle_status,
            "attachments": list(attachments or ()),
            "on_pid": on_pid,
        }
        if interrupt_file:
            runner_kwargs["interrupt_file"] = interrupt_file
        prov_pref, model_pref = _chat_model(channel_id)
        if prov_pref:
            runner_kwargs["provider"] = prov_pref
        if model_pref:
            runner_kwargs["model"] = model_pref
        if persona:
            runner_kwargs["persona"] = persona
        if ws_pref:
            runner_kwargs["workspace"] = ws_pref
        try:
            result = runner(**runner_kwargs)
        except TypeError:
            slim = {
                key: value
                for key, value in runner_kwargs.items()
                if key not in ("attachments", "on_pid", "persona", "workspace", "interrupt_file", "provider", "model")
            }
            try:
                result = runner(**slim)
            except TypeError:
                slim.pop("on_status", None)
                try:
                    result = runner(**slim)
                except TypeError:
                    result = runner(channel="discord", user=loop_user, message=turn_text)
        if attachment_notes:
            result.reply = apply_notes(result.reply, attachment_notes)
        _, err_traces = extract_traces(result.stderr or "")
        if cancelled and cancelled():
            reply_text = "Stopped."
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
            reply_text = f"Turn failed ({err_msg})."
        else:
            reply_text = visible_reply(result.reply, err_traces)
    except Exception as exc:
        log.exception("Loop turn error")
        line = str(exc).splitlines()[0].strip() if str(exc).strip() else exc.__class__.__name__
        if len(line) > 200:
            line = line[:200] + "..."
        reply_text = f"Turn error: {line}"
        result = LoopTurnResult(
            reply=reply_text,
            session="",
            user_id="",
            alias="",
            returncode=1,
            stderr="",
        )

    anim_done.set()
    if thinking_id:
        delete_message(token, channel_id, thinking_id)
    deliver_markdown(token, channel_id, reply_text)
    return result


def inject_text(
    *,
    channel_id: int,
    text: str,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    user: str | None = None,
) -> LoopTurnResult:
    if not channel_id:
        raise ValueError("channel_id is required")
    if not channel_allowed(channel_id, config):
        raise PermissionError("channel_id not in DISCORD_ALLOWED_CHANNEL_IDS")
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("text is required")
    return handle_inbound_text(
        channel_id=channel_id,
        text=stripped,
        config=config,
        on_turn=on_turn,
        user=user or str(channel_id),
    )


def process_message(
    message: dict,
    *,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    job: Job | None = None,
) -> None:
    try:
        channel_id = int(message.get("channel_id") or 0)
    except (TypeError, ValueError):
        return
    if not channel_id or not channel_allowed(channel_id, config):
        return
    author = message.get("author") or {}
    try:
        author_id = int(author.get("id") or 0)
    except (TypeError, ValueError):
        author_id = 0
    if not user_allowed(author_id, config):
        return
    text = str(message.get("content") or "").strip()
    attachments, attachment_notes = _collect_attachments(message, config)
    if not text and not attachments and not attachment_notes:
        return
    if not text and not attachments:
        send_message(config.discord_bot_token, channel_id, apply_notes("", attachment_notes))
        return
    if not text:
        text = "(attachment)"
    if attachment_notes:
        text = apply_notes(text, attachment_notes)
    handle_inbound_text(
        channel_id=channel_id,
        text=text,
        config=config,
        on_turn=on_turn,
        user=str(channel_id),
        attachments=attachments,
        on_pid=job.bind_pid if job is not None else None,
        cancelled=job.cancel.is_set if job is not None else None,
        interrupt_file=job.interrupt_path if job is not None else "",
        attachment_notes=attachment_notes,
    )


def _persona_text(chat_id: int, arg: str, config: RelayConfig) -> str:
    del config
    from .workspaces import get_persona, list_personas

    try:
        ws = _resolve_ws(chat_id)
    except Exception:
        return "No workspace resolved."
    personas = list_personas(ws)
    names = [str(p.get("id") or "") for p in personas]
    current = _chat_persona(chat_id) or ws.default_persona or "default"
    if not arg:
        listing = ", ".join(names) if names else "none"
        return f"persona: {current}\navailable: {listing}\nworkspace: {ws.id}"
    if arg == "default":
        _set_chat_pref(chat_id, "persona", "")
        return "persona: default"
    if arg == current:
        return f"persona: {arg}"
    if get_persona(ws, arg) is None:
        listing = ", ".join(names) if names else "none"
        return f"unknown persona: {arg}\navailable: {listing}"
    _set_chat_pref(chat_id, "persona", arg)
    return f"persona: {arg}"


def _model_text(chat_id: int, arg: str) -> str:
    try:
        from runner.providers import find_provider, list_providers
    except Exception:
        return "provider registry unavailable."
    names = sorted(str(p.get("name") or "") for p in list_providers() if p.get("name"))
    prov_pref, model_pref = _chat_model(chat_id)
    current = prov_pref + ((":" + model_pref) if model_pref else "") if prov_pref else ""
    if not arg:
        ws_pick = ""
        try:
            from runner.model import selected

            pick = selected()
            ws_pick = pick.get("provider", "") + ((":" + pick["model"]) if pick.get("model") else "")
        except Exception:
            pass
        listing = ", ".join(names) if names else "none"
        shown = current or ws_pick or "default"
        return f"model: {shown}\navailable: {listing}"
    if arg in ("clear", "default"):
        _set_chat_pref(chat_id, "model", "")
        return "model: default"
    prov, _, mdl = arg.partition(":")
    prov = prov.strip()
    mdl = mdl.strip()
    if find_provider(prov) is None:
        listing = ", ".join(names) if names else "none"
        return f"unknown provider: {prov}\navailable: {listing}"
    _set_chat_pref(chat_id, "model", f"{prov}:{mdl}" if mdl else prov)
    return f"model: {prov}:{mdl}" if mdl else f"model: {prov}"


def _workspace_text(chat_id: int, arg: str) -> str:
    from .workspaces import create_workspace, get_workspace, list_workspaces

    try:
        ws = _resolve_ws(chat_id)
    except Exception:
        return "No workspace resolved."
    ids = [w.id for w in list_workspaces() if w.id]
    listing = ", ".join(ids) if ids else "none"
    if not arg:
        return f"workspace: {ws.id}\navailable: {listing}"
    head, _, label = arg.partition(" ")
    if head in ("rm", "delete"):
        target = label.strip()
        if not target:
            return f"usage: /workspace rm <id>\navailable: {listing}"
        if _chat_ws(chat_id) == target:
            return "cannot remove the workspace this chat is in — /workspace <other> first"
        from agents_workspace import Host

        try:
            out = Host.from_env().delete(target)
        except ValueError as exc:
            return str(exc)
        return f"workspace {target} archived ({out.name})"
    if head == "new":
        new_id, _, new_label = label.partition(" ")
        try:
            created = create_workspace(label=new_label.strip(), ws_id=new_id.strip())
        except Exception as exc:
            return f"create failed: {exc}"
        _set_chat_pref(chat_id, "ws", created.id)
        _set_chat_pref(chat_id, "persona", "")
        return f"workspace: {created.id} (created)\npersona: {created.default_persona or 'default'}"
    if head == "serve":
        _set_chat_pref(chat_id, "ws", "")
        _set_chat_pref(chat_id, "persona", "")
        return "workspace: serve default"
    try:
        target = get_workspace(head)
    except Exception:
        return f"unknown workspace: {head}\navailable: {listing}"
    _set_chat_pref(chat_id, "ws", target.id)
    persona = _chat_persona(chat_id)
    note = ""
    if persona:
        from .workspaces import get_persona

        if get_persona(target, persona) is None:
            _set_chat_pref(chat_id, "persona", "")
            note = f"\npersona: {target.default_persona or 'default'} (reset — {persona} not in this ws)"
    return f"workspace: {target.id}{note}"


def _handle_control(
    command: tuple[str, str],
    channel_id: int,
    config: RelayConfig,
    registry: JobRegistry,
) -> None:
    token = config.discord_bot_token
    if not token:
        return
    name, rest = command
    if name == "workspace":
        deliver_markdown(token, channel_id, _workspace_text(channel_id, rest))
        return
    if name == "persona":
        deliver_markdown(token, channel_id, _persona_text(channel_id, rest, config))
        return
    if name == "model":
        deliver_markdown(token, channel_id, _model_text(channel_id, rest))
        return
    if name == "jobs":
        deliver_markdown(token, channel_id, jobs_text(registry.list_chat(channel_id)))
        return
    job_id = rest.split()[0] if rest else None
    stopped = registry.stop(channel_id, job_id)
    if not stopped:
        body = "No matching job." if job_id else "No running jobs."
    elif job_id:
        body = f"Stopped {stopped[0]}."
    else:
        body = "Stopped: " + ", ".join(stopped) + "."
    deliver_markdown(token, channel_id, body)


def _mentions_bot(content: str, bot_user_id: int) -> bool:
    if not bot_user_id:
        return False
    return f"<@{bot_user_id}>" in content or f"<@!{bot_user_id}>" in content


def _replies_to_bot(message: dict, bot_user_id: int) -> bool:
    ref = message.get("referenced_message")
    if not isinstance(ref, dict) or not bot_user_id:
        return False
    try:
        return int((ref.get("author") or {}).get("id") or 0) == int(bot_user_id)
    except (TypeError, ValueError):
        return False


def _strip_bot_mentions(content: str, bot_user_id: int) -> str:
    if not bot_user_id:
        return content
    return content.replace(f"<@!{bot_user_id}>", "").replace(f"<@{bot_user_id}>", "").strip()


def dispatch_message(
    message: dict,
    *,
    config: RelayConfig,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    registry: JobRegistry | None = None,
    bot_user_id: int = 0,
) -> None:
    try:
        channel_id = int(message.get("channel_id") or 0)
    except (TypeError, ValueError):
        return
    if not channel_id or not channel_allowed(channel_id, config):
        return
    author = message.get("author") or {}
    if author.get("bot") is True:
        return
    try:
        author_id = int(author.get("id") or 0)
    except (TypeError, ValueError):
        return
    if not user_allowed(author_id, config):
        return
    msg_type = int(message.get("type") or 0)
    if msg_type not in (0, 19):
        return
    raw_content = str(message.get("content") or "")
    is_dm = not message.get("guild_id")
    addressed = is_dm or _mentions_bot(raw_content, bot_user_id) or _replies_to_bot(message, bot_user_id)
    if config.discord_require_mention and not addressed:
        return
    content = _strip_bot_mentions(raw_content, bot_user_id).strip()
    command = parse_control_command(content) if content.startswith("/") else None
    reg = registry or get_registry(config)
    if command is not None and not message.get("attachments"):
        _handle_control(command, channel_id, config, reg)
        return
    preview = content or "(attachment)"
    work_message = dict(message)
    work_message["content"] = content

    def _run(job: Job) -> None:
        process_message(work_message, config=config, on_turn=on_turn, job=job)

    chat_jobs = reg.list_chat(channel_id)
    if len(chat_jobs) == 1 and chat_jobs[0].running:
        reg.stop(channel_id)
    job = reg.submit(channel_id, preview, _run)
    if job is None and not reg.closed:
        token = config.discord_bot_token
        if token:
            try:
                send_message(token, channel_id, "Too many messages waiting. Send /jobs or /stop.")
            except Exception:
                log.warning("failed to send queue-full notice")


def approval_components(ident: str) -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": "Approve", "custom_id": f"ap:{ident}:1"},
                {"type": 2, "style": 4, "label": "Deny", "custom_id": f"ap:{ident}:0"},
            ],
        }
    ]


def _code(text: object) -> str:
    return str(text or "").replace("`", "\\`")


def format_approval_markdown(request: dict) -> str:
    tool = _code(request.get("tool") or "?")
    ident = _code(request.get("id") or "")
    session = _code(request.get("session") or "")
    lines = ["**Approval needed**", f"Tool: `{tool}`"]
    summary = str(request.get("summary") or "").strip()
    if len(summary) > 700:
        summary = summary[:700] + "…"
    if summary:
        lines.append(summary)
    if session:
        lines.append(f"Session: `{session}`")
    args = request.get("args")
    if isinstance(args, dict) and args:
        blob = json.dumps(args, ensure_ascii=False, default=str)
        if len(blob) > 400:
            blob = blob[:400] + "…"
        lines.append(f"```json\n{blob}\n```")
    lines.append(f"ID: `{ident}`")
    return "\n".join(lines)


def respond_interaction(interaction_id: str, interaction_token: str, response: dict) -> dict:
    path = f"/interactions/{interaction_id}/{urllib.parse.quote(interaction_token, safe='')}/callback"
    return _api_request("", "POST", path, response, auth=False, timeout=10, retries=0)


def _interaction_user_id(interaction: dict) -> int:
    user = interaction.get("user") or ((interaction.get("member") or {}).get("user") or {})
    try:
        return int(user.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _interaction_ephemeral(text: str) -> dict:
    return {"type": 4, "data": {"content": text[:DISCORD_MESSAGE_LIMIT], "flags": 64}}


def handle_interaction(interaction: dict, *, config: RelayConfig) -> None:
    if int(interaction.get("type") or 0) != 3:
        return
    interaction_id = str(interaction.get("id") or "")
    interaction_token = str(interaction.get("token") or "")
    data = interaction.get("data") or {}
    parsed = parse_callback_data(str(data.get("custom_id") or ""))
    if parsed is None:
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Unknown action"))
        return
    ident, decision = parsed
    try:
        channel_id = int(interaction.get("channel_id") or (interaction.get("channel") or {}).get("id") or 0)
    except (TypeError, ValueError):
        channel_id = 0
    user_id = _interaction_user_id(interaction)
    if not channel_id or not channel_allowed(channel_id, config) or not user_allowed(user_id, config):
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Not allowed"))
        return
    record = read_approval(config, ident)
    if not record:
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Expired"))
        return
    if int(record.get("chat_id") or 0) != channel_id:
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Not allowed"))
        return
    if record.get("status") != "pending":
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Already decided"))
        return
    note = "approved" if decision == "approved" else "denied"
    updated = record_decision(
        config,
        ident,
        status=decision,
        note=note,
        decided_by=user_id,
        expected_chat_id=channel_id,
    )
    if updated is None:
        respond_interaction(interaction_id, interaction_token, _interaction_ephemeral("Already decided"))
        return
    label = "Approved" if decision == "approved" else "Denied"
    prior = str((interaction.get("message") or {}).get("content") or "Approval")
    merged = f"{prior}\n\n**{label}**"
    respond_interaction(
        interaction_id,
        interaction_token,
        {"type": 7, "data": {"content": merged[:DISCORD_MESSAGE_LIMIT], "components": []}},
    )


def run_approve(
    *,
    channel_id: int,
    timeout: float,
    request: dict,
    config: RelayConfig,
    poll_interval: float = 0.2,
) -> tuple[int, str]:
    ident = str(request.get("id") or "").strip()
    if not APPROVAL_ID_RE.match(ident):
        return 2, "invalid approval id"
    if not config.discord_bot_token:
        return 2, "discord token not set"
    if not channel_allowed(channel_id, config):
        return 2, "channel not allowlisted"
    existing = read_approval(config, ident)
    if existing and existing.get("status") in {"approved", "denied"}:
        if not decision_matches_request(existing, request):
            return 2, "approval request changed"
        if existing.get("status") == "approved":
            return 0, str(existing.get("note") or "approved")
        return 1, str(existing.get("note") or "denied")
    try:
        create_pending(config, channel_id, request)
        send_message(
            config.discord_bot_token,
            channel_id,
            format_approval_markdown(request),
            components=approval_components(ident),
        )
    except Exception:
        log.warning("approval prompt failed for %s", ident)
        try:
            record_decision(
                config,
                ident,
                status="timeout",
                note="discord send failed",
                decided_by=None,
                expected_chat_id=channel_id,
            )
        except Exception:
            pass
        return 2, "discord send failed"
    status, note = wait_for_decision(
        config,
        ident,
        float(timeout),
        poll_interval=poll_interval,
    )
    return status_exit_code(status), note or status


class DiscordEventRouter:
    def __init__(
        self,
        config: RelayConfig,
        *,
        on_turn: Callable[..., LoopTurnResult] | None = None,
        registry: JobRegistry | None = None,
    ) -> None:
        self.config = config
        self.on_turn = on_turn
        self.registry = registry
        self.bot_user_id = 0

    def dispatch(self, event_type: str, data: dict) -> None:
        try:
            self._dispatch(event_type, data)
        except Exception:
            log.exception("discord %s event failed", event_type)

    def _dispatch(self, event_type: str, data: dict) -> None:
        if event_type == "READY":
            try:
                self.bot_user_id = int((data.get("user") or {}).get("id") or 0)
            except (TypeError, ValueError):
                self.bot_user_id = 0
            return
        if event_type in {"MESSAGE_CREATE", "MESSAGE_UPDATE"} and isinstance(data, dict):
            if event_type == "MESSAGE_UPDATE" and not data.get("edited_timestamp"):
                return
            dispatch_message(
                data,
                config=self.config,
                on_turn=self.on_turn,
                registry=self.registry,
                bot_user_id=self.bot_user_id,
            )
            return
        if event_type == "INTERACTION_CREATE" and isinstance(data, dict):
            handle_interaction(data, config=self.config)


def gateway_ws_url(base: str) -> str:
    return base.rstrip("/") + "/?v=10&encoding=json"


def identify_payload(token: str, intents: int = GATEWAY_INTENTS) -> dict:
    return {
        "op": 2,
        "d": {
            "token": token,
            "properties": {
                "os": "windows" if os.name == "nt" else "posix",
                "browser": "agents-relay",
                "device": "agents-relay",
            },
            "intents": intents,
        },
    }


def resume_payload(token: str, session_id: str, seq: int | None) -> dict:
    return {"op": 6, "d": {"token": token, "session_id": session_id, "seq": seq}}


class DiscordGatewayClient:
    def __init__(
        self,
        token: str,
        *,
        on_dispatch: Callable[[str, dict], None],
        connect: Callable[..., object] | None = None,
        intents: int = GATEWAY_INTENTS,
    ) -> None:
        self.token = token
        self.on_dispatch = on_dispatch
        self.connect = connect or self._default_connect
        self.intents = intents
        self.session_id = ""
        self.resume_url = ""
        self.seq: int | None = None

    @staticmethod
    def _default_connect(url: str, timeout: float = 30.0):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "Discord gateway requires websocket-client; install agents-relay[discord]"
            ) from exc
        return websocket.create_connection(url, timeout=timeout, enable_multithread=True)

    @staticmethod
    def _send_json(ws, payload: dict) -> None:
        ws.send(json.dumps(payload, separators=(",", ":")))

    def _clear_session(self) -> None:
        self.session_id = ""
        self.resume_url = ""
        self.seq = None

    def _handle_payload(self, payload: dict, ws, heartbeat_ack: list[bool]) -> str:
        seq = payload.get("s")
        if isinstance(seq, int):
            self.seq = seq
        op = payload.get("op")
        data = payload.get("d")
        if op == 0:
            event_type = str(payload.get("t") or "")
            if event_type == "READY" and isinstance(data, dict):
                self.session_id = str(data.get("session_id") or "")
                self.resume_url = str(data.get("resume_gateway_url") or self.resume_url)
            if isinstance(data, dict):
                self.on_dispatch(event_type, data)
            return ""
        if op == 1:
            self._send_json(ws, {"op": 1, "d": self.seq})
            return ""
        if op == 7:
            return "reconnect"
        if op == 9:
            if data is not True:
                self._clear_session()
            return "reconnect"
        if op == 11:
            heartbeat_ack[0] = True
            return ""
        return ""

    def _run_connection(self, ws, stop_event: threading.Event) -> None:
        first = json.loads(ws.recv())
        if int(first.get("op") or 0) != 10:
            raise ConnectionError("discord gateway did not send Hello")
        interval_ms = float((first.get("d") or {}).get("heartbeat_interval") or 45000)
        interval = max(1.0, interval_ms / 1000.0)
        settimeout = getattr(ws, "settimeout", None)
        if callable(settimeout):
            settimeout(max(10.0, interval * 1.5))
        heartbeat_ack = [True]
        heartbeat_stop = threading.Event()

        def _heartbeat() -> None:
            if heartbeat_stop.wait(interval * random.random()):
                return
            while not heartbeat_stop.is_set() and not stop_event.is_set():
                if not heartbeat_ack[0]:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                heartbeat_ack[0] = False
                try:
                    self._send_json(ws, {"op": 1, "d": self.seq})
                except Exception:
                    return
                heartbeat_stop.wait(interval)

        heartbeat = threading.Thread(target=_heartbeat, name="discord-heartbeat", daemon=True)
        heartbeat.start()
        try:
            if self.session_id:
                self._send_json(ws, resume_payload(self.token, self.session_id, self.seq))
            else:
                self._send_json(ws, identify_payload(self.token, self.intents))
            while not stop_event.is_set():
                raw = ws.recv()
                if raw in (None, ""):
                    raise ConnectionError("discord gateway closed")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                payload = json.loads(raw)
                action = self._handle_payload(payload, ws, heartbeat_ack)
                if action == "reconnect":
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
        finally:
            heartbeat_stop.set()
            try:
                ws.close()
            except Exception:
                pass
            heartbeat.join(timeout=1)

    def run(self, stop_event: threading.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            ws = None
            try:
                if self.session_id and self.resume_url:
                    base = self.resume_url
                else:
                    info = gateway_bot_info(self.token)
                    base = str(info.get("url") or "")
                if not base:
                    raise ConnectionError("discord gateway url missing")
                ws = self.connect(gateway_ws_url(base), timeout=30)
                self._run_connection(ws, stop_event)
                backoff = 1.0
            except Exception as exc:
                if stop_event.is_set():
                    break
                log.warning("discord gateway disconnected: %s", exc)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            delay = backoff + random.random()
            backoff = min(30.0, backoff * 2)
            if stop_event.wait(delay):
                break


def discord_gateway_loop(
    config: RelayConfig,
    *,
    stop_event: threading.Event | None = None,
    on_turn: Callable[..., LoopTurnResult] | None = None,
    connect: Callable[..., object] | None = None,
) -> None:
    token = config.discord_bot_token
    if not token:
        log.warning("DISCORD_BOT_TOKEN not set; discord gateway disabled")
        return
    if not config.discord_allowed_channel_ids and not config.allow_anyone:
        log.error(
            "REFUSING Discord adapter: DISCORD_ALLOWED_CHANNEL_IDS is empty. "
            "Set allowlisted channel ids, or AGENTS_RELAY_ALLOW_ANYONE=1 to opt in."
        )
        return
    if not config.discord_allowed_user_ids and not config.allow_anyone:
        log.error(
            "REFUSING Discord adapter: DISCORD_ALLOWED_USER_IDS is empty. "
            "Discord channels are multi-user; set allowlisted user ids, or "
            "AGENTS_RELAY_ALLOW_ANYONE=1 to opt in."
        )
        return
    if not config.discord_allowed_channel_ids and config.allow_anyone:
        log.warning(
            "WARNING: AGENTS_RELAY_ALLOW_ANYONE=1 with an empty DISCORD_ALLOWED_CHANNEL_IDS. "
            "Any Discord user who can reach the bot can run turns and approve tools."
        )
    if not config.discord_allowed_user_ids and config.allow_anyone:
        log.warning(
            "WARNING: AGENTS_RELAY_ALLOW_ANYONE=1 with an empty DISCORD_ALLOWED_USER_IDS. "
            "Any Discord user who can reach an allowlisted channel can run turns and approve tools."
        )
    if connect is None:
        try:
            import websocket  # noqa: F401
        except ImportError:
            log.error("Discord adapter disabled: install agents-relay[discord] for websocket-client")
            return
    registry = get_registry(config)
    router = DiscordEventRouter(config, on_turn=on_turn, registry=registry)
    client = DiscordGatewayClient(token, on_dispatch=router.dispatch, connect=connect)
    client.run(stop_event or threading.Event())


def start_discord_thread(
    config: RelayConfig,
    *,
    on_turn: Callable[..., LoopTurnResult] | None = None,
) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=discord_gateway_loop,
        kwargs={"config": config, "stop_event": stop_event, "on_turn": on_turn},
        name="discord-gateway",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
