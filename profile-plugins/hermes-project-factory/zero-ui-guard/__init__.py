"""Zero external UI pre-dispatch guard for Maikol's Windows profiles."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

_EXPLICIT_UI_MARKERS = (
    "pode abrir", "abra a janela", "abrir a janela", "traga para frente",
    "pode trazer para frente", "foreground autorizado", "ui visível autorizada",
)
_FORBIDDEN_ALWAYS = (
    "hermes-agent/venv/scripts/pythonw.exe",
    "os.startfile(", "shellexecute", "invoke-item", "start-process",
    "explorer.exe", "windows terminal", "wt.exe", "create_new_console",
)
_SUBPROCESS_MARKERS = ("subprocess.popen(", "subprocess.run(")
_NO_WINDOW_MARKERS = ("create_no_window", "windows_hide_flags", "startupinfo")
_approved_sessions: set[str] = set()


def _is_windows() -> bool:
    return os.name == "nt"


def _key(session_id: str = "", task_id: str = "") -> str:
    return str(session_id or task_id or "__unscoped__")


def _payload(value: Any) -> str:
    try:
        text = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text.lower().replace("\\\\", "/").replace("\\", "/")


def _block(message: str) -> Dict[str, str]:
    return {"action": "block", "message": f"ZERO_UI_GUARD: {message}"}


def _pre_llm_call(session_id: str = "", user_message: str = "", **_: Any) -> Optional[Dict[str, str]]:
    if not _is_windows():
        _approved_sessions.discard(_key(session_id=session_id))
        return None

    low = (user_message or "").lower()
    key = _key(session_id=session_id)
    if any(marker in low for marker in _EXPLICIT_UI_MARKERS):
        _approved_sessions.add(key)
    else:
        _approved_sessions.discard(key)
    return {
        "context": (
            "[ZERO UI RUNTIME GUARD ACTIVE] Do not create visible terminal, shell, browser, editor, "
            "gateway, worker, file-association, or external application UI. Process launchers must use "
            "the real base pythonw.exe or CREATE_NO_WINDOW and must verify descendants/windows."
        )
    }


def _pre_tool_call(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    session_id: str = "",
    task_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    if not _is_windows():
        _approved_sessions.discard(_key(session_id, task_id))
        return None

    safe = args if isinstance(args, dict) else {}
    text = _payload(safe)

    if any(marker in text for marker in _FORBIDDEN_ALWAYS):
        return _block(
            "known visible-UI launcher path is forbidden. Use a headless native API or an approved "
            "background control surface; the Hermes venv pythonw shim is never a hidden launcher."
        )

    if tool_name == "execute_code" and any(marker in text for marker in _SUBPROCESS_MARKERS):
        if "shell=true" in text or "shell\": true" in text:
            return _block("shell=True subprocess creation is forbidden on Windows.")
        if not any(marker in text for marker in _NO_WINDOW_MARKERS):
            return _block(
                "subprocess creation without CREATE_NO_WINDOW/STARTUPINFO evidence is forbidden. "
                "Use the canonical no-window runner and verify the descendant window tree."
            )

    if tool_name in {"write_file", "patch"}:
        path = str(safe.get("path", "")).lower().replace("\\", "/")
        if path.endswith((".vbs", ".cmd", ".bat")) and any(
            marker in text for marker in ("wscript.shell", ".run(", "start ", "cmd.exe", "powershell")
        ):
            return _block("new shell/VBS background launchers are forbidden; use a verified native no-window entrypoint.")

    if tool_name == "computer_use":
        visible_request = bool(safe.get("raise_window")) or str(safe.get("delivery_mode", "")).lower() == "foreground"
        if visible_request and _key(session_id, task_id) not in _approved_sessions:
            return _block("foreground or raise-window computer control requires explicit approval in the current user message.")

    return None


def _post_llm_call(session_id: str = "", **_: Any) -> None:
    _approved_sessions.discard(_key(session_id=session_id))


def _on_session_end(session_id: str = "", task_id: str = "", **_: Any) -> None:
    _approved_sessions.discard(_key(session_id, task_id))


def _write_runtime_registration_marker() -> None:
    try:
        marker = Path(__file__).resolve().parent / "runtime-registration.json"
        tmp = marker.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "registered_at_epoch": time.time()}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, marker)
    except Exception:
        pass


def register(ctx: Any) -> None:
    if not _is_windows():
        return
    _write_runtime_registration_marker()
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
