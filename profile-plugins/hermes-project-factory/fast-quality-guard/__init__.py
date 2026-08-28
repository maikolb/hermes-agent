"""Fast Quality Guard for Hermes Agent.

This plugin turns Maikol's first-delivery and scope-preservation rules into dispatcher-level guidance.
It does not decide whether the requested operation is safe; existing approvals
and project contracts still own that decision. It only prevents unbounded
pre-delivery discovery and forces a result-or-blocker checkpoint.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


SOFT_SECONDS = max(30, int(os.getenv("HERMES_FAST_QUALITY_SOFT_SECONDS", "180")))
HARD_SECONDS = max(SOFT_SECONDS + 30, int(os.getenv("HERMES_FAST_QUALITY_HARD_SECONDS", "270")))
DEFAULT_DISCOVERY_LIMIT = max(1, int(os.getenv("HERMES_FAST_QUALITY_DISCOVERY_LIMIT", "5")))
RESEARCH_DISCOVERY_LIMIT = max(DEFAULT_DISCOVERY_LIMIT, int(os.getenv("HERMES_FAST_QUALITY_RESEARCH_LIMIT", "10")))
MAX_TOTAL_CALLS = max(RESEARCH_DISCOVERY_LIMIT + 1, int(os.getenv("HERMES_FAST_QUALITY_MAX_TOTAL_CALLS", "20")))

_RESEARCH_MARKERS = (
    "pesquis", "research", "audite", "auditoria", "investigue", "investigação",
    "levantamento", "inventário", "compare", "comparação", "análise profunda",
    "analise profundamente", "due diligence", "reconstrua", "diagnóstico completo",
)

# Reads/searches do not constitute first delivery. Housekeeping such as todo,
# memory, and skill edits intentionally stays here too: governance cannot mark
# the user's requested artifact as delivered.
_DISCOVERY_TOOLS = {
    "browser_back", "browser_console", "browser_get_images", "browser_navigate",
    "browser_snapshot", "browser_vision", "honcho_context", "honcho_profile",
    "honcho_reasoning", "honcho_search", "read_file", "search_files",
    "session_search", "skill_view", "skills_list", "vision_analyze", "video_analyze",
    "web_extract", "web_search", "todo", "memory", "skill_manage", "clarify",
    "delegate_task", "browser_scroll", "browser_press", "browser_click", "browser_type",
}

_DIRECT_ACTION_TOOLS = {
    "patch", "write_file", "computer_use", "cronjob", "video_generate",
    "text_to_speech",
}

_MUTATION_MARKERS = (
    "shutil.rmtree", "removedirectoryw", "deletefilew", "terminateprocess",
    ".unlink(", "os.remove(", "os.unlink(", ".write_text(", ".write_bytes(",
    "shutil.move", "shutil.copy", "os.replace(", "write_file(", "patch(",
    "boto3", "send_command(", "subprocess.run(", "subprocess.popen(",
)

_BROAD_ITERATORS = (
    ".rglob(", "os.walk(", ".glob('**/*')", '.glob("**/*")',
)

_OPTIONAL_EXPANSION_MARKERS = (
    "authentication", "authorization", "bearer token", "access token", "login",
    "oauth", "rbac", "role-based", "pairing", "pareamento", "pair code",
    "multi-user", "multiuser", "multiusuário", "multiusuario", "vps", "deploy",
    "deployment", "public url", "internet-facing", "rate limit", "csrf",
)

_OPTIONAL_EXPANSION_REQUEST_MARKERS = (
    "autenticação", "autenticacao", "authentication", "login", "oauth", "token",
    "rbac", "papéis", "papeis", "pareamento", "pairing", "multiusuário",
    "multiusuario", "multi-user", "equipe", "vps", "deploy", "publique",
    "publicar", "produção", "producao", "internet", "segurança", "seguranca",
    "hardening", "csrf", "rate limit",
)

@dataclass
class _TurnState:
    started_at: float
    user_message: str
    allow_broad: bool
    discovery_limit: int
    discovery_calls: int = 0
    action_calls: int = 0
    total_calls: int = 0
    redirect_count: int = 0
    last_redirect_reason: str = ""


_states: Dict[str, _TurnState] = {}
_lock = threading.RLock()


def _key(session_id: str = "", task_id: str = "", **_: Any) -> str:
    return str(session_id or task_id or "__unscoped__")


def _is_research(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _RESEARCH_MARKERS)


def _state_for(session_id: str = "", task_id: str = "", **_: Any) -> _TurnState:
    key = _key(session_id, task_id)
    state = _states.get(key)
    if state is None:
        state = _TurnState(
            started_at=time.monotonic(),
            user_message="",
            allow_broad=False,
            discovery_limit=DEFAULT_DISCOVERY_LIMIT,
        )
        _states[key] = state
    return state


def _payload(args: Any) -> str:
    try:
        return json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(args).lower()


def _looks_like_action(tool_name: str, args: Dict[str, Any]) -> bool:
    if tool_name in _DIRECT_ACTION_TOOLS:
        if tool_name == "computer_use":
            return str(args.get("action", "")).lower() not in {"", "capture", "list_apps", "list_windows"}
        if tool_name == "cronjob":
            return str(args.get("action", "")).lower() not in {"", "list"}
        return True
    if tool_name == "process":
        return str(args.get("action", "")).lower() in {"kill", "write", "submit", "close"}
    if tool_name == "execute_code":
        code = str(args.get("code", "")).lower()
        return any(marker in code for marker in _MUTATION_MARKERS)
    if tool_name == "terminal":
        command = str(args.get("command", "")).lower()
        return any(marker in command for marker in (
            " rm ", "rm -", " mv ", " cp ", "git commit", "git push", "install ",
            "deploy", "delete", "remove", "mkdir", "touch ", ">", "--write",
        ))
    return False


def _looks_broad(tool_name: str, args: Dict[str, Any], state: _TurnState) -> bool:
    if state.action_calls:
        return False
    text = _payload(args)
    # Python-recursive enumeration is always the wrong pre-delivery path: use
    # exact-target reads or the indexed search tool instead, including on
    # research turns. One such rglob cost 126 seconds in the incident run.
    if tool_name == "execute_code":
        if any(marker in text for marker in _BROAD_ITERATORS):
            return True
        if "psutil.process_iter" in text and "open_files" in text:
            return True
    # Explicit research/audit turns may use the indexed search surface broadly,
    # but they still cannot bypass the recursive-Python veto above.
    if state.allow_broad:
        return False
    if tool_name == "search_files":
        path = str(args.get("path", "")).replace("\\", "/").lower().rstrip("/")
        pattern = str(args.get("pattern", ""))
        if path in {"c:/users/maiko", "c:/users/maiko/appdata/local/hermes"} and pattern in {"*", ".*", "**/*"}:
            return True
    return False


def _new_mutation_text(tool_name: str, args: Dict[str, Any]) -> str:
    """Return only text being introduced, so removals and rollbacks remain allowed."""
    if tool_name == "patch":
        if isinstance(args.get("new_string"), str):
            return str(args.get("new_string", "")).lower()
        patch_text = str(args.get("patch", ""))
        return "\n".join(
            line[1:] for line in patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ).lower()
    if tool_name == "write_file":
        return str(args.get("content", "")).lower()
    if tool_name == "execute_code":
        return str(args.get("code", "")).lower()
    return ""


def _is_unrequested_optional_expansion(tool_name: str, args: Dict[str, Any], state: _TurnState) -> bool:
    introduced = _new_mutation_text(tool_name, args)
    if not introduced or not any(marker in introduced for marker in _OPTIONAL_EXPANSION_MARKERS):
        return False
    requested = state.user_message.lower()
    return not any(marker in requested for marker in _OPTIONAL_EXPANSION_REQUEST_MARKERS)


def _redirect(state: _TurnState, message: str) -> None:
    """Record an intelligent redirect without vetoing the current tool call."""
    if state.last_redirect_reason != message:
        state.redirect_count += 1
        state.last_redirect_reason = message


def _pre_llm_call(session_id: str = "", user_message: str = "", **_: Any) -> Dict[str, str]:
    research = _is_research(user_message)
    state = _TurnState(
        started_at=time.monotonic(),
        user_message=user_message or "",
        allow_broad=research,
        discovery_limit=RESEARCH_DISCOVERY_LIMIT if research else DEFAULT_DISCOVERY_LIMIT,
    )
    with _lock:
        _states[_key(session_id=session_id)] = state
    return {
        "context": (
            "[FAST QUALITY RUNTIME REDIRECT ACTIVE] This guard never requires a manual continuation. "
            f"pre-action discovery limit={state.discovery_limit}, soft checkpoint={SOFT_SECONDS}s, "
            f"hard checkpoint={HARD_SECONDS}s. These are redirect checkpoints, not vetoes. Deliver the "
            "smallest safe usable result first. At a checkpoint, finish only the current materially useful "
            "call, then choose the direct canonical action or deliver the exact blocker; never ask the user "
            "to send 'continue' merely to reset this guard. Preserve explicit scope and trust boundaries: do "
            "not add authentication, pairing, RBAC, multi-user, VPS/deploy, hardening, or adjacent architecture "
            "unless the current request explicitly requires it."
        )
    }


def _pre_tool_call(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    session_id: str = "",
    task_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    safe_args = args if isinstance(args, dict) else {}
    with _lock:
        state = _state_for(session_id=session_id, task_id=task_id)
        elapsed = time.monotonic() - state.started_at

        if _is_unrequested_optional_expansion(tool_name, safe_args, state):
            return {
                "action": "block",
                "reason": (
                    "FAST_QUALITY_SCOPE: optional architecture/security expansion was not requested. "
                    "Preserve the user's stated trust and scope boundaries; complete the requested product "
                    "slice without auth, pairing, RBAC, multi-user, VPS/deploy, hardening, or adjacent layers. "
                    "Removal or rollback remains allowed; never ask for 'continue'."
                ),
            }

        redirect_reason = ""
        if elapsed >= HARD_SECONDS:
            redirect_reason = f"hard {HARD_SECONDS}s checkpoint reached; deliver or take the direct action next"
        elif state.total_calls >= MAX_TOTAL_CALLS:
            redirect_reason = f"{MAX_TOTAL_CALLS}-tool checkpoint reached; deliver or take the direct action next"
        elif _looks_broad(tool_name, safe_args, state):
            redirect_reason = "broad discovery detected; use the exact known target on the next step"

        action = _looks_like_action(tool_name, safe_args)
        if not action and not state.action_calls:
            if not redirect_reason and elapsed >= SOFT_SECONDS:
                redirect_reason = f"soft {SOFT_SECONDS}s checkpoint reached; take the smallest direct action next"
            elif not redirect_reason and state.discovery_calls >= state.discovery_limit:
                redirect_reason = f"pre-action discovery checkpoint ({state.discovery_limit}) reached; act next"

        if redirect_reason:
            _redirect(state, redirect_reason)

        state.total_calls += 1
        if action:
            state.action_calls += 1
        else:
            state.discovery_calls += 1
    return None


def _post_llm_call(session_id: str = "", **_: Any) -> None:
    with _lock:
        _states.pop(_key(session_id=session_id), None)


def _on_session_end(session_id: str = "", task_id: str = "", **_: Any) -> None:
    with _lock:
        _states.pop(_key(session_id=session_id, task_id=task_id), None)


def _reset_for_tests() -> None:
    with _lock:
        _states.clear()


def _age_for_tests(session_id: str, seconds: float) -> None:
    with _lock:
        state = _state_for(session_id=session_id)
        state.started_at = time.monotonic() - seconds


def _write_runtime_registration_marker() -> None:
    """Prove which process loaded the plugin; never blocks registration."""
    try:
        marker = Path(__file__).resolve().parent / "runtime-registration.json"
        tmp = marker.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "registered_at_epoch": time.time(),
                    "soft_seconds": SOFT_SECONDS,
                    "hard_seconds": HARD_SECONDS,
                    "discovery_limit": DEFAULT_DISCOVERY_LIMIT,
                    "mode": "intelligent-redirect-with-scope-veto",
                    "blocking": "unrequested-optional-expansion-only",
                    "version": "1.3.0",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, marker)
    except Exception:
        # Runtime blocking remains in-memory. Activation verification will fail
        # closed if this marker cannot be read back from the gateway process.
        pass


def register(ctx: Any) -> None:
    _write_runtime_registration_marker()
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
