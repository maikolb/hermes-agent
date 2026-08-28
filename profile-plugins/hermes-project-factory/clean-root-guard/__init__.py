"""Clean-root guard for the explicitly rejected Hermes Project Ops implementations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

_FORBIDDEN_ROOTS = (
    "c:/users/maiko/projetos/hermes agent project ops",
    "c:/users/maiko/projetos/hermes project ops",
)
_FORBIDDEN_ARTIFACTS = (
    "project-ops-smoke", "project_ops_test_mode", "project ops test mode",
    "localhost:4312", "127.0.0.1:4312", "port=4312", "listen(4312",
)
_ACTION_TOOLS = {"write_file", "patch", "execute_code", "terminal", "process", "computer_use"}


def _payload(value: Any) -> str:
    try: text = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, default=str)
    except Exception: text = str(value)
    return text.lower().replace("\\\\", "/").replace("\\", "/")


def _block(detail: str) -> Dict[str, str]:
    return {"action": "block", "message": f"CLEAN_ROOT_GUARD: {detail}"}


def _pre_llm_call(**_: Any) -> Dict[str, str]:
    return {"context": "[CLEAN ROOT GUARD ACTIVE] The two rejected Hermes Project Ops implementations, roots, launchers, ports, variables, databases, routes, and protocols are prohibited as implementation dependencies."}


def _pre_tool_call(tool_name: str, args: Optional[Dict[str, Any]] = None, **_: Any) -> Optional[Dict[str, str]]:
    if tool_name not in _ACTION_TOOLS:
        return None
    safe = args if isinstance(args, dict) else {}
    text = _payload(safe)
    if any(root in text for root in _FORBIDDEN_ROOTS):
        return _block("mutation or execution against a rejected project root is forbidden; use a new clean root with an explicit contract.")
    if tool_name in {"execute_code", "terminal", "process", "computer_use"} and any(marker in text for marker in _FORBIDDEN_ARTIFACTS):
        return _block("execution references a rejected Project Ops artifact, port, or variable.")
    if tool_name in {"write_file", "patch"}:
        path = str(safe.get("path", "")).lower()
        if not path.endswith((".md", ".txt")) and any(marker in text for marker in _FORBIDDEN_ARTIFACTS):
            return _block("non-document implementation would recreate a rejected Project Ops artifact, port, or variable.")
    return None


def _write_runtime_registration_marker() -> None:
    try:
        marker = Path(__file__).resolve().parent / "runtime-registration.json"
        tmp = marker.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "registered_at_epoch": time.time()}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, marker)
    except Exception: pass


def register(ctx: Any) -> None:
    _write_runtime_registration_marker()
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
