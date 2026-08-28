"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sqlite3
import time
import unicodedata
from contextvars import Context
from pathlib import Path
from typing import Any, Callable, Optional

from agent.i18n import t

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")

_WORKER_FOCUS_LOG_TAIL_BYTES = 64 * 1024
# 1600: a structured worker closeout (Scope/Done/Evidence/Limitations —
# gap 6) no longer fits the old 700; the full trace (header + summary +
# link) must stay under Telegram's 4096-char message cap (gap 8 audit).
_WORKER_TRACE_SUMMARY_MAX_CHARS = 1600
_WORKER_FOCUS_MAX_ITEMS = 10
_WORKER_FOCUS_MAX_LINE_CHARS = 280
_WORKER_FOCUS_MAX_REASONING_CHARS = 800
_WORKER_FOCUS_MAX_OUTPUT_CHARS = 2800
_WORKER_FOCUS_ANSI_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)

_PARALLEL_TASK_PREFIX_RE = re.compile(
    r"^(?:por\s+favor\s+)?(?:"
    r"investig(?:a|ar|ue)|implemente?|implementa|implementar|crie?|criar|"
    r"analise?|analisar|pesquise?|pesquisar|revise?|revisar|"
    r"teste?|testar|documente?|documentar|prepare?|preparar|"
    r"corrija?|corrigir|conserte?|consertar|"
    r"investigate|implement|create|analy[sz]e|research|review|"
    r"test|document|prepare|fix|build|add"
    r")\b",
    re.IGNORECASE,
)
_PARALLEL_EXPLICIT_RE = re.compile(
    r"\b(?:nova\s+tarefa|outra\s+tarefa|em\s+paralelo|parallel(?:ly)?|"
    r"separate\s+task|new\s+task)\b",
    re.IGNORECASE,
)
_STEER_REFERENCE_RE = re.compile(
    r"\b(?:isso|isto|esse|essa|esses|essas|aquilo|"
    r"o\s+que\s+voce|que\s+voce|que\s+acabou|acabou\s+de|"
    r"ultima\s+(?:mudanca|alteracao)|mudanca\s+anterior|"
    r"this|that|what\s+you|you\s+just|last\s+change|previous\s+change)\b",
    re.IGNORECASE,
)
_STEER_ACTION_RE = re.compile(
    r"^(?:por\s+favor\s+)?(?:"
    r"reverta?|reverter|desfaca?|desfazer|volte?|voltar|"
    r"ajuste?|ajustar|mude?|mudar|troque?|trocar|"
    r"continue?|continuar|pare?|parar|ignore?|ignorar|"
    r"undo|revert|roll\s+back|change|adjust|continue|stop|ignore"
    r")\b",
    re.IGNORECASE,
)


def _fold_parallel_intake_text(value: Any) -> str:
    """Normalize accents/case without changing the user text persisted to Kanban."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip()


def _classify_parallel_intake_message(text: Any) -> str:
    """Classify a busy follow-up as ``new_task`` or ``steer``.

    The classifier is deliberately deterministic and conservative: explicit
    parallel/new-task language wins, deictic corrections stay attached to the
    current run, and otherwise only action-oriented task requests fan out.
    Unknown conversational follow-ups retain the established steer behavior.
    """
    folded = _fold_parallel_intake_text(text)
    if not folded or folded.startswith("/"):
        return "steer"
    if _PARALLEL_EXPLICIT_RE.search(folded):
        return "new_task"
    if _STEER_ACTION_RE.search(folded) and _STEER_REFERENCE_RE.search(folded):
        return "steer"
    if _STEER_REFERENCE_RE.search(folded) and re.match(
        r"^(?:corrija?|corrigir|fix|conserte?|consertar)\b",
        folded,
        re.IGNORECASE,
    ):
        return "steer"
    return "new_task" if _PARALLEL_TASK_PREFIX_RE.search(folded) else "steer"


def _resolve_parallel_by_default(config: Any, platform_key: str = "telegram") -> bool:
    """Resolve the profile gate, preserving an explicit false.

    Profiles that already opted into Part 1 worker rotation inherit the Part 2
    default until they explicitly set ``dispatch.parallel_by_default``. This
    makes the NF/PF rollout cohesive while every other profile keeps the old
    busy-input behavior.
    """
    cfg = config if isinstance(config, dict) else {}
    dispatch_cfg = cfg.get("dispatch")
    if isinstance(dispatch_cfg, dict) and "parallel_by_default" in dispatch_cfg:
        return dispatch_cfg.get("parallel_by_default") is True
    return _resolve_worker_focus_handoff(lambda: cfg, platform_key)


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """Resolve the live (enabled, per_tick) auto-decompose settings.

    Read fresh from config on every dispatcher tick (#49638) so that flipping
    ``kanban.auto_decompose: false`` to STOP runaway fan-out takes effect on the
    next tick instead of requiring a gateway restart. Auto-decompose is a
    safety toggle — a user who sees it create and launch tasks they didn't
    intend reaches for this flag to halt it, and a stale boot-captured value
    silently ignoring that change is the bug reported in #49638.

    Fails **safe**: if the config read raises, return ``(False, 3)`` — a
    transient read error must never re-enable a feature the user turned off,
    nor fall back to the burst-prone default-on behaviour. ``per_tick`` is
    clamped to ``>= 1``.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _resolve_agent_wake_on_events(load_config: Callable[[], Any]) -> bool:
    """Return whether Kanban lifecycle events may synthesize agent turns.

    User-facing notification and agent execution are separate authorities.
    The safe default is passive notification only; a config read failure or
    any value other than the literal boolean ``True`` therefore fails closed.
    """
    try:
        cfg = load_config()
    except Exception:
        return False
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    return kcfg.get("agent_wake_on_events") is True


def _resolve_worker_focus_handoff(
    load_config: Callable[[], Any],
    platform_key: str = "telegram",
) -> bool:
    """Compatibility wrapper for the profile/platform worker-rotation gate."""
    try:
        cfg = load_config()
    except Exception:
        return False
    try:
        from gateway.run import _resolve_activity_indicator_settings

        return _resolve_activity_indicator_settings(
            cfg if isinstance(cfg, dict) else {},
            platform_key,
            180.0,
        ).worker_rotation
    except Exception:
        return False


def _load_worker_focus_config(
    profile_name: Optional[str],
    load_default: Callable[[], Any],
) -> dict:
    """Load raw display config for the subscription's owning profile."""
    profile = str(profile_name or "").strip()
    if profile:
        try:
            from gateway.run import _load_gateway_config
            from hermes_cli.profiles import get_profile_dir

            config_path = get_profile_dir(profile) / "config.yaml"
            if config_path.exists():
                config = _load_gateway_config(config_path)
                return config if isinstance(config, dict) else {}
        except Exception:
            logger.debug(
                "kanban worker rotation: profile config read failed for %s",
                profile,
                exc_info=True,
            )
        # A multiplexed subscription must never inherit another profile's
        # display authority when its own config is absent or unreadable.
        return {}
    else:
        # ``load_config()`` includes DEFAULT_CONFIG.  Reading the raw gateway
        # config first preserves the distinction between an explicit
        # ``display.worker_rotation: false`` and an absent key that should
        # still honor the legacy ``kanban.worker_focus_handoff: true`` gate.
        try:
            from gateway.run import _load_gateway_config

            config = _load_gateway_config()
            if isinstance(config, dict) and config:
                return config
        except Exception:
            logger.debug(
                "kanban worker rotation: default raw config read failed",
                exc_info=True,
            )
    try:
        config = load_default()
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


def _kanban_worker_focus_key(board: str, sub: dict) -> tuple[str, str, str, str, str]:
    return (
        str(board or ""),
        str(sub.get("platform") or "").lower(),
        str(sub.get("chat_id") or ""),
        str(sub.get("thread_id") or ""),
        str(sub.get("notifier_profile") or ""),
    )


def _kanban_worker_spawn_order(task: Any) -> tuple[int, int, float, str]:
    """Order current attempts by their canonical run/spawn id."""
    try:
        run_id = int(getattr(task, "current_run_id", 0) or 0)
    except (TypeError, ValueError):
        run_id = 0
    try:
        started_at = float(getattr(task, "started_at", 0) or 0)
    except (TypeError, ValueError):
        started_at = 0.0
    if run_id > 0:
        return (0, run_id, started_at, str(getattr(task, "id", "") or ""))
    return (1, 0, started_at, str(getattr(task, "id", "") or ""))


def _kanban_worker_matches_scope(
    task: Any,
    *,
    project_id: str = "",
    session_id: str = "",
    session_only: bool = False,
) -> bool:
    """Match a worker to proven project/session ownership, failing closed."""
    wanted_project = str(project_id or "")
    wanted_session = str(session_id or "")
    task_project = str(getattr(task, "project_id", "") or "")
    task_session = str(getattr(task, "session_id", "") or "")
    if wanted_project:
        if task_project:
            return task_project == wanted_project
        return bool(wanted_session and task_session == wanted_session)
    if session_only:
        return bool(wanted_session and task_session == wanted_session)
    return True


def _read_worker_trace_summary(board: str, task_id: str, kind: str) -> str:
    """Fetch the worker's own closeout text for a completion/blocked trace.

    Completed cards carry the worker's final summary in ``result``; blocked
    cards carry it in the last card comment (close_delegation_cards stores
    the summary as a comment on the block path). Best-effort: any failure
    returns an empty string and the trace stays short.
    """
    if not (board and task_id):
        return ""
    try:
        from hermes_cli import kanban_db as _kb

        with _kb.connect_closing(board=board) as conn:
            if kind == "completed":
                task = _kb.get_task(conn, task_id)
                return str(getattr(task, "result", "") or "").strip()
            comments = _kb.list_comments(conn, task_id)
            if comments:
                return str(getattr(comments[-1], "body", "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("worker trace summary read failed", exc_info=True)
    return ""


def _render_worker_trace_content(
    *,
    kind: str,
    title: str,
    board: str,
    task_id: str,
    run_id: Any,
    summary: str,
    trace_url_template: str,
) -> str:
    """Render one worker's terminal trace (its closeout, as shown in chat).

    TARGET_ARCHITECTURE gap 5: every finished worker publishes ITS OWN
    closeout — per worker, not per display lane — so two workers finishing
    together produce two traces, and a blocked worker reports its reason
    instead of disappearing silently.
    """
    first_line = (
        "✅ Worker concluído" if kind == "completed" else "⛔ Worker bloqueado"
    )
    if title:
        first_line += f": {title}"
    second_line = f"Kanban {task_id}" if task_id else ""
    if run_id is not None:
        second_line += f" · run {run_id}"
    if board and second_line:
        second_line = f"[{board}] {second_line}"
    if summary:
        summary = re.sub(r"\n{3,}", "\n\n", summary)
        if len(summary) > _WORKER_TRACE_SUMMARY_MAX_CHARS:
            summary = (
                summary[: _WORKER_TRACE_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
            )
    lines = [first_line]
    if summary:
        lines.append("")
        lines.append(summary)
        lines.append("")
    if second_line:
        lines.append(second_line)
    template = str(trace_url_template or "").strip()
    if template and task_id:
        try:
            link = template.format(
                board=board,
                task_id=task_id,
                run_id=run_id if run_id is not None else "",
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.debug("worker trace url template invalid: %s", exc)
            link = ""
        if link:
            lines.append(f"📋 Log completo: {link}")
    return "\n".join(lines)


def _render_kanban_worker_focus(task: Any, *, board: str, active_count: int) -> str:
    count = f" 1/{active_count}" if active_count > 1 else ""
    title = str(getattr(task, "title", "") or getattr(task, "id", "worker"))[:120]
    assignee = str(getattr(task, "assignee", "") or "").strip()
    owner = f"@{assignee} — " if assignee else ""
    board_tag = f"[{board}] " if board else ""
    run_id = getattr(task, "current_run_id", None)
    run = f" · run {run_id}" if run_id is not None else ""
    return (
        f"▶ Now following worker{count}\n"
        f"{board_tag}{owner}{title}\n"
        f"Kanban {getattr(task, 'id', '')}{run}"
    )


# ── FNAT bubble re-rendering (worker log dialects → principal's surface) ──
#
# The focus bubble reads whichever log the worker produced: the in-process
# tee ("┊ {icon} {name}  {args}  {dur}s"), the live-transcript fallback
# ("┊ Tool: name(args)" / "┊ name ok 0.1s: {result json}"), or a dispatcher
# CLI stdout ("┊ 💻 $  cmd  0.1s"). All three are log dialects; the
# principal's chat surface is semantic ("💻 Running git status"). Rather
# than passing log lines through verbatim (raw result JSON included —
# operator regression report 28/08), each line is parsed back to
# (tool, args) and re-rendered with the same emoji + friendly-verb tables
# the principal uses. Success results are dropped (the principal never
# shows them); errors keep a short extracted message.

# Live-transcript fallback: "Tool: name(args)" (args may be truncated).
_FOCUS_TOOL_CALL_RE = re.compile(
    r"^Tool: (?P<name>[\w.-]+)\((?P<args>.*)\)$"
)
# Live-transcript fallback result: "name ok|ERROR 0.1s: {...}".
_FOCUS_RESULT_RE = re.compile(
    r"^(?P<name>[\w.-]+) (?P<status>ok|ERROR)(?: [\d.]+s)?: ?(?P<rest>.*)$"
)
# Native tee / CLI stdout: "{icon} {label}  {args}  {dur}s" — the icon is
# any non-word glyph run; a trailing duration is optional.
_FOCUS_NATIVE_RE = re.compile(
    r"^(?P<icon>[^\w\s]+)\s+(?P<label>\S+)\s*(?P<rest>.*?)(?:\s+[\d.]+s)?$"
)
_FOCUS_ERROR_FIELD_RE = re.compile(r'"error"\s*:\s*"(?P<msg>[^"]+)"')

# Dispatcher CLI stdout labels are display aliases, not tool names
# (agent/display.py _render_tool_line); map the common ones back so the
# friendly-verb lookup works. Unknown labels render as-is with the default
# gear emoji — degraded but never raw.
_FOCUS_CLI_LABEL_TOOLS = {
    "$": "terminal",
    "plan": "todo",
    "read": "read_file",
    "write": "write_file",
    "patch": "patch",
    "search": "search_files",
    "fetch": "web_extract",
    "navigate": "browser_navigate",
}

_FOCUS_ARGS_MAX_CHARS = 100
_FOCUS_ERROR_MAX_CHARS = 160

# Core-tool emojis for when the tool registry is not loaded in this
# process (tests, standalone renders). The live gateway resolves through
# agent.display.get_tool_emoji → registry, same as the principal.
_FOCUS_FALLBACK_EMOJIS = {
    "terminal": "💻",
    "read_file": "📖",
    "write_file": "✍️",
    "patch": "🔧",
    "search_files": "🔎",
    "web_search": "🔍",
    "web_extract": "📄",
    "browser_navigate": "🌐",
    "skill_view": "📚",
    "todo": "📋",
    "delegate_task": "🤝",
    "memory": "🧠",
}


def _focus_rich_tool_line(name: str, args_text: str) -> str:
    """Render one tool call exactly the way the principal's chat surface does."""
    from agent.display import (
        get_tool_emoji,
        get_tool_verb,
        tool_verb_connector,
        verb_drops_preview,
    )

    args_text = " ".join(str(args_text or "").split())
    if len(args_text) > _FOCUS_ARGS_MAX_CHARS:
        args_text = args_text[:_FOCUS_ARGS_MAX_CHARS - 3] + "..."
    emoji = get_tool_emoji(name, default="") or _FOCUS_FALLBACK_EMOJIS.get(
        name, "⚙️"
    )
    verb = get_tool_verb(name)
    if verb and (verb_drops_preview(name) or not args_text):
        body = verb
    elif verb:
        body = f"{verb}{tool_verb_connector(name)}{args_text}"
    elif args_text:
        body = f"{name} {args_text}"
    else:
        body = name
    return f"{emoji} {body}"


_FOCUS_OUTPUT_FIELD_RE = re.compile(r'"output"\s*:\s*"(?P<msg>[^"]+)"')


def _focus_error_line(name: str, detail: str) -> str:
    """Short error line: prefer the payload's error/output text over raw JSON."""
    detail = str(detail or "").strip()
    field = _FOCUS_ERROR_FIELD_RE.search(detail) or _FOCUS_OUTPUT_FIELD_RE.search(
        detail
    )
    if field:
        detail = field.group("msg")
    detail = " ".join(detail.split())
    if len(detail) > _FOCUS_ERROR_MAX_CHARS:
        detail = detail[:_FOCUS_ERROR_MAX_CHARS - 3] + "..."
    line = f"⚠ {name}" if name else "⚠"
    return f"{line}: {detail}" if detail else line


def _render_kanban_worker_focus_output(
    raw_log: Any,
    *,
    task_id: str,
    include_tool_progress: bool = True,
    include_reasoning: bool = True,
) -> str:
    """Project the latest worker attempt into one bounded, redacted message.

    Log lines are re-rendered into the principal's semantic surface (emoji +
    friendly verb + short args); raw tool results never reach the bubble.
    """
    if not raw_log:
        return ""
    text = _WORKER_FOCUS_ANSI_RE.sub("", str(raw_log))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    attempt_marker = f"Query: work kanban task {task_id}"
    marker_at = text.rfind(attempt_marker)
    if marker_at >= 0:
        text = text[marker_at + len(attempt_marker):]

    items: list[str] = []
    reasoning_lines: list[str] = []
    in_reasoning = False

    def _flush_reasoning() -> None:
        nonlocal reasoning_lines
        if not reasoning_lines:
            return
        reasoning = " ".join(reasoning_lines).strip()
        if len(reasoning) > _WORKER_FOCUS_MAX_REASONING_CHARS:
            reasoning = reasoning[:_WORKER_FOCUS_MAX_REASONING_CHARS].rstrip() + "..."
        if include_reasoning:
            items.append(f"💭 {reasoning}")
        reasoning_lines = []

    def _append_tool_item(body: str) -> None:
        stripped = body.strip()
        if not stripped:
            return
        call = _FOCUS_TOOL_CALL_RE.match(stripped)
        if call:
            items.append(_focus_rich_tool_line(call.group("name"), call.group("args")))
            return
        if stripped.startswith("Tool: "):
            items.append(_focus_rich_tool_line(stripped[len("Tool: "):], ""))
            return
        result = _FOCUS_RESULT_RE.match(stripped)
        if result:
            if result.group("status") == "ERROR":
                items.append(
                    _focus_error_line(result.group("name"), result.group("rest"))
                )
            # Success results never reach the bubble — the principal's
            # surface doesn't show them either.
            return
        native = _FOCUS_NATIVE_RE.match(stripped)
        if native:
            label = native.group("label")
            name = _FOCUS_CLI_LABEL_TOOLS.get(label, label)
            items.append(_focus_rich_tool_line(name, native.group("rest")))
            return
        # Unknown dialect: keep a hard-capped plain line rather than hiding
        # activity — but this is the degraded path, not the design.
        if len(stripped) > _WORKER_FOCUS_MAX_LINE_CHARS:
            stripped = stripped[:_WORKER_FOCUS_MAX_LINE_CHARS].rstrip() + "..."
        items.append(stripped)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if (line.startswith("┌") and "Reasoning" in line) or line == "Reasoning":
            _flush_reasoning()
            in_reasoning = True
            continue
        if in_reasoning:
            if line.startswith("└"):
                _flush_reasoning()
                in_reasoning = False
                continue
            cleaned = line.strip(" │┌┐└┘─")
            if cleaned and cleaned != "Reasoning":
                reasoning_lines.append(cleaned)
            continue
        if not include_tool_progress:
            continue
        if line.startswith("┊"):
            _append_tool_item(line.lstrip("┊").strip())
        elif line.startswith("⚠"):
            body = line.lstrip("⚠").strip()
            result = _FOCUS_RESULT_RE.match(body)
            native = None if result else _FOCUS_NATIVE_RE.match(body)
            if result:
                items.append(
                    _focus_error_line(result.group("name"), result.group("rest"))
                )
            elif native:
                # Native tee error line: "⚠ {icon} {name}  {args}  {dur}s" —
                # re-render rich and keep the warning prefix.
                label = native.group("label")
                name = _FOCUS_CLI_LABEL_TOOLS.get(label, label)
                items.append(
                    "⚠ " + _focus_rich_tool_line(name, native.group("rest"))
                )
            else:
                items.append(_focus_error_line("", body))
        elif line.startswith("──"):
            marker = " ".join(line.lstrip("─").split())
            if marker:
                if len(marker) > _WORKER_FOCUS_MAX_LINE_CHARS:
                    marker = marker[:_WORKER_FOCUS_MAX_LINE_CHARS].rstrip() + "..."
                items.append(f"── {marker}")
    _flush_reasoning()

    if not items:
        return ""
    output = "\n".join(items[-_WORKER_FOCUS_MAX_ITEMS:])
    try:
        from agent.redact import redact_sensitive_text

        output = redact_sensitive_text(
            output,
            force=True,
            redact_url_credentials=True,
        )
    except Exception:
        logger.warning("kanban worker focus redaction failed; suppressing worker output")
        return ""
    if len(output) > _WORKER_FOCUS_MAX_OUTPUT_CHARS:
        output = output[-_WORKER_FOCUS_MAX_OUTPUT_CHARS:]
        first_newline = output.find("\n")
        if first_newline >= 0:
            output = output[first_newline + 1:]
    return output.strip()


_LIVE_LOG_LINE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} (?P<role>\w+)\s*\| (?P<text>.*)$"
)


def _live_transcript_to_worker_log(text: str) -> str:
    """Convert a delegation live transcript into board-worker-log dialect.

    Operator feedback (28/08, DOVTest): the FNAT focus bubble showed no
    reasoning/tool activity for in-process workers — they stream to the
    delegation live transcript, not to the board worker log the bubble
    reads. Rather than teaching the renderer a second dialect, the live
    lines (``HH:MM:SS role | text``) are mapped onto the exact shapes
    ``_render_kanban_worker_focus_output`` already parses: ``think`` lines
    become Reasoning blocks, ``tool``/``result`` lines become ``┊`` items.
    """
    out: list[str] = []
    for raw_line in (text or "").splitlines():
        match = _LIVE_LOG_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        role = match.group("role")
        body = match.group("text").strip()
        if not body:
            continue
        if role == "think":
            out.append("┌─ Reasoning")
            out.append(f"│ {body}")
            out.append("└")
        elif role == "tool":
            out.append(f"┊ Tool: {body.lstrip('-> ')}")
        elif role == "result":
            out.append(f"┊ {body}")
    return "\n".join(out)


def _read_mirror_live_transcript(board: str, task_id: str,
                                 tail_bytes: int) -> str:
    """Tail the live transcript a mirror card's comment points at.

    ``create_delegation_cards`` records ``Live transcript: <path>`` in the
    card's first comment. Best-effort: any failure returns "" and the
    bubble simply has no activity block (the old behavior).
    """
    if not task_id:
        return ""
    try:
        from hermes_cli import kanban_db as _kb

        with _kb.connect_closing(board=board or None) as conn:
            comments = _kb.list_comments(conn, task_id)
        path_str = ""
        for comment in comments:
            body = str(getattr(comment, "body", "") or "")
            marker = "Live transcript: "
            at = body.find(marker)
            if at >= 0:
                path_str = body[at + len(marker):].split()[0].strip()
                break
        if not path_str:
            return ""
        from pathlib import Path as _Path

        path = _Path(path_str)
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
        return _live_transcript_to_worker_log(
            data.decode("utf-8", errors="replace")
        )
    except Exception:  # noqa: BLE001
        logger.debug("mirror live transcript read failed", exc_info=True)
        return ""


def _kanban_dispatch_allowed() -> bool:
    """Return False while the global emergency stop (`hermes pause`) is engaged.

    Checked every dispatcher tick BEFORE spawning new workers so a pause takes
    effect on the next tick without a gateway restart. In-flight workers are
    never touched — this only stops NEW spawns. Fails open: if the estop
    module is unimportable, dispatch proceeds (the sentinel gate must not
    become a new crash surface for the dispatcher).
    """
    try:
        from agent.estop import check_paused
    except ImportError:
        return True
    return not check_paused("kanban", logger)


def _run_in_fresh_context(func: Callable[..., Any], /, *args: Any) -> Any:
    """Run *func* in an empty ``Context`` so request-local ContextVars stay behind.

    ``asyncio.to_thread`` copies the calling task's context onto the worker
    thread. Supervised Kanban ticks are process-owned writers; if that copy
    still carries a ``delegate_task`` child marker, ``write_txn``
    false-trips. Since watchers spawn from a fresh ``Context``
    (``_spawn_supervised``), this offload-boundary scrub is defense in
    depth: it covers non-supervised spawn paths and any task context frozen
    before spawn isolation shipped. An empty Context keeps the DB guard
    intact for real children without exempting dispatcher writes.
    """
    return Context().run(func, *args)


async def _to_thread_process_service(func: Callable[..., Any], /, *args: Any) -> Any:
    """Offload blocking process-service work (dispatcher + notifier writers)
    without inheriting request-local ContextVars."""
    return await asyncio.to_thread(_run_in_fresh_context, func, *args)


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take an exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway process machine-wide may run the embedded kanban
    dispatcher: concurrent dispatchers double the reclaim frequency (each
    runs its own ``release_stale_claims`` → promote → dispatch loop), double
    claim-attempt events in the event log, and — with ``wal_autocheckpoint=0`` —
    concurrent manual WAL checkpoints can corrupt index pages. The
    ``dispatch_in_gateway`` config flag is the primary control; this lock is the
    backstop that survives config drift and same-profile restart races.

    Delegates to :func:`gateway.status._try_acquire_file_lock` (``fcntl`` on
    POSIX, ``msvcrt`` on Windows) so the guard is cross-platform.

    Returns ``(handle, "held")`` on success — the caller keeps the file handle
    for the process lifetime and **must** release it via
    :func:`_release_singleton_lock` when done. ``(None, "contended")`` when
    another process holds the lock (caller must NOT dispatch). ``(None,
    "unavailable")`` when locking cannot be performed (non-POSIX filesystem
    without flock, or the status.py helpers are unimportable) — caller falls
    back to config-only control.
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a dispatcher singleton lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``SessionSource.scope_id`` on platforms
    where one bot serves several isolated tenants, so a wake source must carry
    the same scope as inbound messages from that chat to resolve to the same
    session.

    The subscription's persisted ``delivery_metadata`` wins over the adapter's
    live chat → scope mapping, because it records the scope the subscription was
    created from; the mapping only covers rows that stored no metadata. ``None``
    means the chat has no scope, which is what an unscoped platform's key
    contains.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if callable(resolver):
        try:
            resolved = resolver(str(sub.get("chat_id") or ""))
        except Exception as exc:
            # An adapter-side lookup failure yields no scope, never an error.
            logger.debug(
                "kanban notifier: scope lookup failed for chat %s: %s",
                sub.get("chat_id"),
                exc,
                exc_info=True,
            )
            return None
        if resolved:
            return str(resolved)
    return None


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        """Return whether this gateway currently owns the singleton lock."""
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    def _kanban_parallel_dispatch_config(self, source: Any) -> dict:
        """Load the raw config owned by the inbound message's profile."""
        profile = str(getattr(source, "profile", "") or "").strip() or None
        try:
            from gateway.run import _load_gateway_config

            return _load_worker_focus_config(profile, _load_gateway_config)
        except Exception:
            logger.debug(
                "parallel intake: profile config read failed",
                exc_info=True,
            )
            return {}

    def _kanban_parallel_dispatch_assignee(
        self,
        source: Any,
        config: dict,
    ) -> str:
        """Resolve the existing dispatcher route for a new parallel card."""
        kanban_cfg = config.get("kanban") if isinstance(config, dict) else None
        if isinstance(kanban_cfg, dict):
            configured = str(kanban_cfg.get("default_assignee") or "").strip()
            if configured:
                return configured
        routed = str(getattr(source, "profile", "") or "").strip()
        if routed:
            return routed
        try:
            return str(self._active_profile_name() or "default")
        except Exception:
            return "default"

    @staticmethod
    def _kanban_parallel_queue_state(
        board: str,
        task_id: str,
        assignee: str,
        config: dict,
    ) -> tuple[bool, int]:
        """Return ``(capacity_reached, ready_position)`` for one new card."""
        from hermes_cli import kanban_db as _kb

        kcfg = config.get("kanban") if isinstance(config, dict) else None
        kcfg = kcfg if isinstance(kcfg, dict) else {}

        def _positive_int(value: Any) -> Optional[int]:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        conn = _kb.connect(board=board or None)
        try:
            ready = conn.execute(
                "SELECT id FROM tasks WHERE status = 'ready' "
                "AND claim_lock IS NULL ORDER BY priority DESC, created_at ASC"
            ).fetchall()
            position = next(
                (index for index, row in enumerate(ready, start=1) if row["id"] == task_id),
                0,
            )
            running = _kb.count_running_tasks(conn)
            at_capacity = False
            max_spawn = _positive_int(kcfg.get("max_spawn"))
            if max_spawn is not None and running >= max_spawn:
                at_capacity = True

            configured_max = _positive_int(kcfg.get("max_in_progress"))
            effective_max = _kb.resolve_max_in_progress(configured_max)
            if effective_max is not None:
                host_running = running + _kb.count_running_tasks_other_boards(
                    board or None
                )
                if host_running >= effective_max:
                    at_capacity = True

            per_profile = _positive_int(kcfg.get("max_in_progress_per_profile"))
            if per_profile is not None and assignee:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks "
                    "WHERE status = 'running' AND assignee = ?",
                    (assignee,),
                ).fetchone()
                if int(row["n"] if row else 0) >= per_profile:
                    at_capacity = True
            return at_capacity, position
        finally:
            conn.close()

    async def _kanban_parallel_dispatch_busy_message(
        self,
        event: Any,
        session_key: str,
    ) -> bool:
        """Turn an independent busy-topic request into a subscribed Kanban card."""
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            return False
        if not str(getattr(source, "thread_id", "") or "").strip():
            return False
        text = str(getattr(event, "text", "") or "").strip()
        if _classify_parallel_intake_message(text) != "new_task":
            return False

        config = self._kanban_parallel_dispatch_config(source)
        platform = getattr(getattr(source, "platform", None), "value", None)
        platform_key = str(platform or getattr(source, "platform", "") or "").lower()
        if not _resolve_parallel_by_default(config, platform_key):
            return False

        project_context, project_denial = await asyncio.to_thread(
            self._resolve_project_context_for_message,
            event,
            source,
        )
        if (
            project_denial is not None
            or project_context is None
            or bool(getattr(project_context, "is_management", False))
        ):
            return False
        board = str(getattr(project_context, "board_slug", "") or "").strip()
        if not board:
            return False

        assignee = self._kanban_parallel_dispatch_assignee(source, config)
        args = [
            "kanban",
            "create",
            text,
            "--assignee",
            assignee,
            "--created-by",
            "gateway-parallel-intake",
        ]
        original_text = event.text
        try:
            event.text = "/" + shlex.join(args)
            output = await self._handle_kanban_command(event)
        except Exception:
            logger.warning(
                "parallel intake: Kanban create failed for session %s",
                session_key,
                exc_info=True,
            )
            return False
        finally:
            event.text = original_text

        match = re.search(r"Created\s+(t_[0-9a-f]+)\b", str(output or ""))
        if match is None:
            logger.warning(
                "parallel intake: Kanban create returned no task id for session %s",
                session_key,
            )
            return False
        task_id = match.group(1)
        try:
            at_capacity, position = await asyncio.to_thread(
                self._kanban_parallel_queue_state,
                board,
                task_id,
                assignee,
                config,
            )
        except Exception:
            logger.debug("parallel intake: queue-state read failed", exc_info=True)
            at_capacity, position = False, 0

        if at_capacity:
            suffix = f" Ready queue position: {position}." if position else ""
            message = (
                f"🧩 Parallel task created: {task_id}. Worker capacity is full; "
                f"the card remains ready.{suffix}"
            )
        else:
            message = (
                f"🧩 Parallel task created: {task_id}. "
                "The dispatcher will start it on its next tick."
            )

        adapter = self._adapter_for_source(source)
        if adapter is not None:
            reply_anchor = self._reply_anchor_for_event(event)
            try:
                await adapter._send_with_retry(
                    chat_id=source.chat_id,
                    content=message,
                    reply_to=(
                        reply_anchor
                        if platform_key == "telegram"
                        and getattr(source, "chat_type", "") == "dm"
                        and getattr(source, "thread_id", None)
                        else (
                            None
                            if platform_key == "telegram"
                            and getattr(source, "thread_id", None)
                            else getattr(event, "message_id", None)
                        )
                    ),
                    metadata=self._thread_metadata_for_source(source, reply_anchor),
                )
            except Exception:
                # The card is already durable. Never fall back to steering the
                # same request merely because its confirmation could not send.
                logger.warning(
                    "parallel intake: confirmation failed for %s",
                    task_id,
                    exc_info=True,
                )
        logger.info(
            "Parallel intake created %s on board %s for session %s (queued=%s position=%s)",
            task_id,
            board,
            session_key,
            at_capacity,
            position,
        )
        return True

    def _kanban_worker_display_lane(self, source: Any) -> tuple[str, str, str, str]:
        """Return the profile-aware presentation lane for one chat source."""
        try:
            active_profile = str(self._active_profile_name() or "default")
        except Exception:
            active_profile = "default"
        platform_value = getattr(getattr(source, "platform", None), "value", None)
        if platform_value is None:
            platform_value = getattr(source, "platform", "")
        return (
            str(platform_value or "").lower(),
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
            str(getattr(source, "profile", "") or active_profile),
        )

    def _kanban_worker_display_available(
        self,
        lane: tuple[str, str, str, str],
        claim_sequence: int,
    ) -> bool:
        """Return whether one captured claim may currently project a worker."""
        scopes: dict[tuple, dict] = getattr(
            self, "_kanban_worker_display_scopes", {}
        )
        scope = scopes.get(lane)
        if not scope:
            return False
        if int(scope.get("claim_sequence") or 0) != int(claim_sequence or 0):
            return False
        if scope.get("synthetic"):
            # Router-binding scope: no principal turn has run on this lane in
            # this process, so there is no session to defer to. The moment a
            # real turn claims the lane, its scope replaces this one and the
            # sequence check above fences this render out.
            return True
        principal_session_key = str(
            scope.get("principal_session_key") or ""
        ).strip()
        if not principal_session_key:
            return False
        try:
            return not self._is_session_running(principal_session_key)
        except Exception:
            logger.debug("kanban worker focus session probe failed", exc_info=True)
            return False

    def _kanban_ensure_rotation_scope(
        self,
        lane: tuple[str, str, str, str],
        board: str,
    ) -> Optional[dict]:
        """Return the lane's scope, synthesizing one from router bindings.

        The rotation used to render only after a principal turn registered a
        scope for the lane — after a gateway restart nothing renders until
        someone talks in the topic, which in practice meant the rotation
        never engaged (27/08). The project router persists exactly the
        lane↔board pairing needed to project workers safely without a
        principal, so synthesize a scope from it. A real principal claim
        replaces the synthetic scope and immediately fences it out via the
        claim-sequence check.
        """
        scopes: dict[tuple, dict] = getattr(
            self, "_kanban_worker_display_scopes", {}
        )
        self._kanban_worker_display_scopes = scopes
        scope = scopes.get(lane)
        if scope is not None:
            return scope
        board_slug = str(board or "").strip()
        if not board_slug:
            return None
        now = time.monotonic()
        cache = getattr(self, "_kanban_rotation_targets_cache", None)
        if not cache or now - cache[0] > 60.0:
            try:
                targets = self._kanban_board_display_targets({lane[3]})
            except Exception:  # noqa: BLE001
                targets = {}
            cache = (now, targets)
            self._kanban_rotation_targets_cache = cache
        board_targets = cache[1].get(board_slug) or []
        for target in board_targets:
            if (
                str(target.get("platform") or "").lower() == lane[0]
                and str(target.get("chat_id") or "") == lane[1]
                and str(target.get("thread_id") or "") == lane[2]
                and str(target.get("notifier_profile") or "") == lane[3]
            ):
                claim_sequence = int(
                    getattr(self, "_kanban_worker_display_claim_sequence", 0)
                    or 0
                ) + 1
                self._kanban_worker_display_claim_sequence = claim_sequence
                scope = {
                    "board": board_slug,
                    "project_id": "",
                    "session_id": "",
                    "principal_session_key": "",
                    "run_generation": None,
                    "claim_sequence": claim_sequence,
                    "synthetic": True,
                }
                scopes[lane] = scope
                logger.info(
                    "kanban worker rotation: synthesized display scope for "
                    "board %s from router bindings (no principal turn yet)",
                    board_slug,
                )
                return scope
        return None

    async def _kanban_finalize_worker_focus_state(
        self,
        state_key: tuple,
        adapter: Any,
        trace_url_template: str = "",
        exit_kind: str = "completed",
    ) -> None:
        """Edit one finished worker bubble into a short terminal trace.

        ``trace_url_template`` (display.worker_rotation_trace_url) may carry
        ``{board}``/``{task_id}``/``{run_id}`` placeholders; when set, the
        trace gains a link line so the full worker log (reasoning + tool
        activity) is one tap away in the read-only dashboard.

        The trace body is the worker's own closeout (card result for
        completed, last card comment for blocked) — a trace with just a
        title reads as "nothing to see here" (user feedback 27/08).

        Retryable edit failures keep the state so the next teardown pass
        retries; success and permanent failures drop it. The message itself
        is never deleted on this path.
        """
        states: dict[tuple, dict] = getattr(self, "_kanban_worker_focus_states", {})
        state = states.get(state_key)
        if not state or not state.get("message_id"):
            states.pop(state_key, None)
            return
        sub = state.get("sub") or {}
        task_id = str(state.get("task_id") or "").strip()
        title = str(state.get("task_title") or "").strip()
        board = str(state.get("board") or "").strip()
        attempt = state.get("attempt_id")
        run_id = None
        if isinstance(attempt, (tuple, list)) and len(attempt) > 1:
            run_id = attempt[1]
        summary = await asyncio.to_thread(
            _read_worker_trace_summary, board, task_id, exit_kind
        )
        content = _render_worker_trace_content(
            kind=exit_kind,
            title=title,
            board=board,
            task_id=task_id,
            run_id=run_id,
            summary=summary,
            trace_url_template=trace_url_template,
        )
        try:
            result = await adapter.edit_message(
                sub.get("chat_id"), str(state["message_id"]), content
            )
        except Exception:
            logger.debug("kanban worker focus trace edit failed", exc_info=True)
            return
        if result is None or getattr(result, "retryable", False):
            return
        states.pop(state_key, None)

    async def _kanban_discard_worker_focus_state(
        self,
        state_key: tuple,
        adapter: Any,
        sub: dict,
    ) -> bool:
        """Delete one focus bubble, retaining state when deletion may retry."""
        states: dict[tuple, dict] = getattr(self, "_kanban_worker_focus_states", {})
        state = states.get(state_key)
        if not state or not state.get("message_id"):
            states.pop(state_key, None)
            return True
        try:
            result = await adapter.delete_message(
                sub.get("chat_id") or state.get("sub", {}).get("chat_id"),
                str(state["message_id"]),
            )
        except Exception:
            logger.debug("kanban worker focus suspension failed", exc_info=True)
            return False
        if result is None or result is True or getattr(result, "success", False):
            states.pop(state_key, None)
            return True
        return False

    async def _kanban_claim_worker_display(
        self,
        source: Any,
        *,
        board: str = "",
        project_id: str = "",
        session_id: str = "",
        principal_session_key: Optional[str] = None,
        run_generation: Optional[int] = None,
    ) -> None:
        """Give a new principal turn ownership and pin its project scope."""
        lane = self._kanban_worker_display_lane(source)
        if principal_session_key is None:
            try:
                principal_session_key = self._session_key_for_source(source)
            except Exception:
                logger.debug(
                    "kanban worker rotation: principal session key failed",
                    exc_info=True,
                )
                principal_session_key = ""
        scopes: dict[tuple, dict] = getattr(
            self, "_kanban_worker_display_scopes", {}
        )
        self._kanban_worker_display_scopes = scopes
        claim_sequence = int(
            getattr(self, "_kanban_worker_display_claim_sequence", 0) or 0
        ) + 1
        self._kanban_worker_display_claim_sequence = claim_sequence
        scopes[lane] = {
            "board": str(board or ""),
            "project_id": str(project_id or ""),
            "session_id": str(session_id or ""),
            "principal_session_key": str(principal_session_key or ""),
            "run_generation": run_generation,
            "claim_sequence": claim_sequence,
        }

        # Do not wait for the notifier poll to notice a short principal turn.
        # Delete any worker bubble now; a transient failure retains state and
        # the watcher retries before it can resume that worker.
        states: dict[tuple, dict] = getattr(self, "_kanban_worker_focus_states", {})
        if not states:
            return
        adapter = self._adapter_for_source(source)
        if adapter is None:
            return
        for state_key, state in list(states.items()):
            normalized_key = state_key[1:] if len(state_key) == 5 else state_key
            if normalized_key != lane:
                continue
            await self._kanban_discard_worker_focus_state(
                state_key,
                adapter,
                state.get("sub") or {},
            )

    def _kanban_active_worker_count(
        self,
        source: Any,
        *,
        board: str = "",
        project_id: str = "",
        session_id: str = "",
    ) -> int:
        """Count active workers owned by this chat/project presentation lane."""
        active: dict[tuple, dict[str, dict]] = getattr(
            self, "_kanban_worker_focus_active", {}
        )
        if not active:
            return 0
        try:
            active_profile = str(self._active_profile_name() or "default")
        except Exception:
            active_profile = "default"
        platform_value = getattr(getattr(source, "platform", None), "value", None)
        if platform_value is None:
            platform_value = getattr(source, "platform", "")
        destination = (
            str(platform_value or "").lower(),
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
            str(getattr(source, "profile", "") or active_profile),
        )
        wanted_board = str(board or "")
        wanted_project = str(project_id or "")
        wanted_session = str(session_id or "")
        use_session_scope = bool(wanted_session and not wanted_board and not wanted_project)
        counted: set[tuple[str, str]] = set()
        for key, bucket in active.items():
            if len(key) != 5:
                continue
            key_board, key_platform, key_chat, key_thread, key_profile = key
            normalized_key_destination = (
                str(key_platform or "").lower(),
                str(key_chat or ""),
                str(key_thread or ""),
                str(key_profile or active_profile),
            )
            if normalized_key_destination != destination:
                continue
            if wanted_board and str(key_board or "") != wanted_board:
                continue
            for task_id, row in bucket.items():
                task = row.get("task") if isinstance(row, dict) else None
                if not _kanban_worker_matches_scope(
                    task,
                    project_id=wanted_project,
                    session_id=wanted_session,
                    session_only=use_session_scope,
                ):
                    continue
                counted.add((str(key_board or ""), str(task_id)))
        return len(counted)

    def _kanban_board_display_targets(
        self, notifier_profiles: set
    ) -> dict[str, list[dict]]:
        """``board_slug → display targets`` from persisted topic bindings.

        Worker-rotation focus used to be fed exclusively by task-level notify
        subscriptions, so a board whose cards were created outside the
        subscribe path (CLI, dispatcher, reconciler) never rotated its
        workers into the topic display even while they ran (27/08 DOVCRM
        incident — the zero-subscription early exit made it permanent). The
        project router DB persists exactly the binding the focus key needs:
        (platform, chat_id, thread_id) per board, per profile.
        """
        targets: dict[str, list[dict]] = {}
        try:
            from hermes_cli.profiles import get_profile_dir

            router_config = getattr(
                getattr(self, "config", None), "project_router", None
            )
            if not getattr(router_config, "enabled", False):
                return targets
            configured = getattr(router_config, "db_path", None)
            for profile in sorted(str(p or "").strip() for p in notifier_profiles):
                if not profile:
                    continue
                try:
                    base = Path(get_profile_dir(profile))
                except Exception:
                    continue
                candidate = (
                    Path(configured) if configured else Path("project_router.db")
                )
                if not candidate.is_absolute():
                    candidate = base / candidate
                if not candidate.is_file():
                    continue
                try:
                    conn = sqlite3.connect(
                        f"file:{candidate.as_posix()}?mode=ro",
                        uri=True,
                        timeout=2,
                    )
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute(
                            "SELECT b.platform, b.chat_id, b.thread_id, "
                            "       p.board_slug "
                            "FROM topic_bindings AS b "
                            "JOIN projects AS p "
                            "  ON p.profile = b.profile "
                            " AND p.project_id = b.project_id "
                            "WHERE b.profile = ? AND b.is_management = 0 "
                            "  AND b.is_closed = 0",
                            (profile,),
                        ).fetchall()
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    logger.debug(
                        "kanban notifier: router bindings unreadable for "
                        "profile %s: %s",
                        profile,
                        exc,
                    )
                    continue
                for row in rows:
                    board = str(row["board_slug"] or "").strip()
                    if not board:
                        continue
                    targets.setdefault(board, []).append({
                        "platform": str(row["platform"] or "").lower(),
                        "chat_id": str(row["chat_id"] or ""),
                        "thread_id": str(row["thread_id"] or ""),
                        "notifier_profile": profile,
                    })
        except Exception as exc:  # noqa: BLE001 - display feed is best-effort
            logger.debug(
                "kanban notifier: board display targets unavailable: %s", exc
            )
        return targets

    def _kanban_focus_apply_rows(self, rows: list[dict]) -> None:
        """Apply one-time bootstrap rows and lifecycle deltas to local counters."""
        active: dict[tuple, dict[str, dict]] = getattr(
            self, "_kanban_worker_focus_active", {}
        )
        self._kanban_worker_focus_active = active
        # Why each worker left its bucket, per lane. The teardown's trace
        # path may only say "concluído" for a worker that actually
        # completed; a retry/reclaim/crash exit must keep the silent delete
        # (the worker will be back, or failed — either way no false trace).
        exits: dict[tuple, dict[str, str]] = getattr(
            self, "_kanban_worker_focus_exits", {}
        )
        self._kanban_worker_focus_exits = exits
        terminal_kinds = {
            "completed", "blocked", "gave_up", "review_requested",
            "changes_requested", "archived", "timed_out", "crashed",
            "rate_limited", "stale", "reclaimed", "block_loop_detected",
        }
        rows = sorted(
            rows,
            key=lambda row: (
                0 if row.get("bootstrap") else 1,
                _kanban_worker_spawn_order(row.get("task")),
            ),
        )
        for row in rows:
            task = row.get("task")
            sub = row.get("sub") or {}
            if task is None:
                continue
            key = _kanban_worker_focus_key(row.get("board") or "", sub)
            bucket = active.setdefault(key, {})
            has_current_attempt = (
                task.status == "running"
                and getattr(task, "current_run_id", None) is not None
            )
            if row.get("bootstrap") and has_current_attempt:
                bucket[task.id] = row
            for event in row.get("events") or []:
                if event.kind == "claimed":
                    if has_current_attempt:
                        bucket[task.id] = row
                    else:
                        bucket.pop(task.id, None)
                elif event.kind in terminal_kinds:
                    bucket.pop(task.id, None)
                    # Carry enough context to publish this worker's closeout
                    # even when it never held the focus bubble (gap 5: the
                    # trace is per WORKER, not per display lane).
                    exits.setdefault(key, {})[str(task.id)] = {
                        "kind": str(event.kind),
                        "sub": dict(sub) if isinstance(sub, dict) else {},
                        "title": str(getattr(task, "title", "") or "")[:96],
                        "run_id": getattr(task, "current_run_id", None),
                    }
                elif event.kind in {"status", "unblocked"}:
                    # Dashboard/direct status changes do not emit a dedicated
                    # terminal event.  Converge from the fetched task state so
                    # running -> ready/other cannot leave a ghost worker. A
                    # direct ready -> running move has no worker/run, so it may
                    # refresh existing membership but never create it.
                    if has_current_attempt and task.id in bucket:
                        bucket[task.id] = row
                    else:
                        bucket.pop(task.id, None)
            if not bucket:
                active.pop(key, None)

    async def _kanban_refresh_worker_focus(self) -> None:
        """Render live worker state; with count zero this is an immediate no-op."""
        from gateway.config import Platform as _Platform
        from gateway.platforms.base import BasePlatformAdapter
        from gateway.display_config import resolve_display_setting
        from gateway.run import (
            _render_activity_indicator_template,
            _resolve_activity_indicator_settings,
        )
        from hermes_cli import kanban_db as _kb
        from hermes_cli.config import load_config as _load_config

        active: dict[tuple, dict[str, dict]] = getattr(
            self, "_kanban_worker_focus_active", {}
        )
        states: dict[tuple, dict] = getattr(self, "_kanban_worker_focus_states", {})
        self._kanban_worker_focus_states = states
        scopes: dict[tuple, dict] = getattr(
            self, "_kanban_worker_display_scopes", {}
        )
        self._kanban_worker_display_scopes = scopes
        exits: dict[tuple, dict[str, str]] = getattr(
            self, "_kanban_worker_focus_exits", {}
        )
        self._kanban_worker_focus_exits = exits
        # exits participates in the gate: a worker can finish after its lane
        # emptied (no active rows, no bubble) and its closeout is still owed.
        if not active and not states and not exits:
            return

        config_cache: dict[str, dict] = {}

        def _config_for_profile(profile: Optional[str]) -> dict:
            cache_key = str(profile or "")
            if cache_key not in config_cache:
                config_cache[cache_key] = _load_worker_focus_config(
                    profile,
                    _load_config,
                )
            return config_cache[cache_key]

        try:
            active_profile = str(self._active_profile_name() or "default")
        except Exception:
            active_profile = "default"
        all_active_lanes: set[tuple[str, str, str, str]] = set()
        lane_candidates: dict[tuple[str, str, str, str], list[dict]] = {}
        for key, bucket in list(active.items()):
            if not bucket:
                active.pop(key, None)
                continue
            if len(key) != 5:
                continue
            key_board, key_platform, key_chat, key_thread, key_profile = key
            lane = (
                str(key_platform or "").lower(),
                str(key_chat or ""),
                str(key_thread or ""),
                str(key_profile or active_profile),
            )
            all_active_lanes.add(lane)
            # Active rows can be reconstructed after a gateway restart, but
            # the chat/project ownership claim is process-local. Without a
            # principal claim, fall back to the router's persisted lane↔board
            # binding (synthetic scope); only lanes with no binding at all
            # stay fail-closed.
            scope = self._kanban_ensure_rotation_scope(lane, key_board)
            if scope is None:
                continue
            scope_board = str(scope.get("board") or "")
            scope_project = str(scope.get("project_id") or "")
            scope_session = str(scope.get("session_id") or "")
            use_session_scope = bool(
                scope_session and not scope_board and not scope_project
            )
            for row in bucket.values():
                task = row.get("task") if isinstance(row, dict) else None
                if task is None:
                    continue
                row_board = str(row.get("board") or key_board or "")
                if scope_board and row_board != scope_board:
                    continue
                if not _kanban_worker_matches_scope(
                    task,
                    project_id=scope_project,
                    session_id=scope_session,
                    session_only=use_session_scope,
                ):
                    continue
                lane_candidates.setdefault(lane, []).append(row)

        for lane, candidates in lane_candidates.items():
            candidates.sort(
                key=lambda row: _kanban_worker_spawn_order(row.get("task"))
            )
            chosen = candidates[0]
            task = chosen["task"]
            sub = chosen["sub"]
            board = chosen.get("board") or ""
            try:
                platform = _Platform(str(sub.get("platform") or "").lower())
            except ValueError:
                continue
            profile = str(sub.get("notifier_profile") or "").strip() or None
            user_config = _config_for_profile(profile)
            indicator = _resolve_activity_indicator_settings(
                user_config,
                platform.value,
                180.0,
            )
            adapter = self._authorization_adapter(platform, profile)
            if adapter is None:
                continue
            if not indicator.worker_rotation:
                await self._kanban_discard_worker_focus_state(lane, adapter, sub)
                continue
            state = states.get(lane)
            claim_sequence = int(
                (scopes.get(lane) or {}).get("claim_sequence") or 0
            )
            if state and int(state.get("claim_sequence") or 0) != claim_sequence:
                await self._kanban_discard_worker_focus_state(lane, adapter, sub)
                continue
            adapter_edit = getattr(type(adapter), "edit_message", None)
            if adapter_edit is None or adapter_edit is BasePlatformAdapter.edit_message:
                continue
            metadata = (
                dict(sub.get("delivery_metadata"))
                if isinstance(sub.get("delivery_metadata"), dict)
                else {}
            )
            if sub.get("thread_id") and not metadata.get("thread_id"):
                metadata["thread_id"] = sub["thread_id"]
            if not self._kanban_worker_display_available(lane, claim_sequence):
                # A fresh user turn owns the presentation lane immediately.
                # Keep the active worker queue, but remove its stale focus
                # bubble so the principal heartbeat/progress is unambiguous.
                await self._kanban_discard_worker_focus_state(lane, adapter, sub)
                continue

            state = states.get(lane)
            attempt_id = (task.id, getattr(task, "current_run_id", None))
            attempt_changed = not state or state.get("attempt_id") != attempt_id
            now = time.time()
            started_at = float(getattr(task, "started_at", None) or now)
            elapsed_seconds = max(0.0, now - started_at)
            activity_text = (
                str(state.get("activity_text") or "")
                if state and not attempt_changed
                else ""
            )
            activity_updated_at = (
                float(state.get("activity_updated_at") or 0.0)
                if state and not attempt_changed
                else 0.0
            )
            if attempt_changed or not activity_text:
                activity_text = (
                    _render_activity_indicator_template(
                        indicator,
                        first_update=True,
                        elapsed_seconds=elapsed_seconds,
                    )
                    or f"⏳ Working — {int(elapsed_seconds // 60)} min"
                )
                activity_updated_at = now
            elif now - activity_updated_at >= indicator.update_interval_seconds:
                activity_text = (
                    _render_activity_indicator_template(
                        indicator,
                        first_update=False,
                        elapsed_seconds=elapsed_seconds,
                    )
                    or f"⏳ Working — {int(elapsed_seconds // 60)} min"
                )
                activity_updated_at = now

            try:
                raw_log = await asyncio.to_thread(
                    _kb.read_worker_log,
                    task.id,
                    tail_bytes=_WORKER_FOCUS_LOG_TAIL_BYTES,
                    board=board,
                )
            except Exception:
                logger.debug("kanban worker focus log read failed", exc_info=True)
                raw_log = None
            if not raw_log:
                # In-process (delegate_task) workers never write the board
                # worker log — their activity streams to the delegation live
                # transcript referenced by the mirror card's comment. Fall
                # back to it so the focus bubble shows reasoning/tool
                # activity for them too (operator feedback 28/08).
                raw_log = await asyncio.to_thread(
                    _read_mirror_live_transcript,
                    board,
                    task.id,
                    _WORKER_FOCUS_LOG_TAIL_BYTES,
                )
            if not self._kanban_worker_display_available(lane, claim_sequence):
                await self._kanban_discard_worker_focus_state(lane, adapter, sub)
                continue
            worker_output = _render_kanban_worker_focus_output(
                raw_log,
                task_id=task.id,
                include_tool_progress=(
                    resolve_display_setting(
                        user_config,
                        platform.value,
                        "tool_progress",
                        "off",
                    )
                    not in {"off", "log"}
                ),
                include_reasoning=bool(
                    resolve_display_setting(
                        user_config,
                        platform.value,
                        "show_reasoning",
                        False,
                    )
                ),
            )
            content_parts = [
                _render_kanban_worker_focus(
                    task, board=board, active_count=len(candidates)
                ),
                activity_text,
            ]
            if worker_output:
                content_parts.append(worker_output)
            content = "\n\n".join(content_parts)
            if state and state.get("content") == content:
                state.update(
                    activity_text=activity_text,
                    activity_updated_at=activity_updated_at,
                    attempt_id=attempt_id,
                )
                continue
            last_sent_at = float(state.get("last_sent_at") or 0.0) if state else 0.0
            if (
                state
                and now - last_sent_at < indicator.update_interval_seconds
            ):
                # Polling the canonical worker log stays cheap and frequent,
                # but every Telegram edit, including worker rotation/retry,
                # keeps the activity-indicator cadence.
                continue
            message_id = state.get("message_id") if state else None
            if message_id:
                try:
                    result = await adapter.edit_message(
                        sub["chat_id"], str(message_id), content
                    )
                except Exception:
                    logger.debug("kanban worker focus edit failed", exc_info=True)
                    continue
                if not self._kanban_worker_display_available(lane, claim_sequence):
                    # The principal may have reclaimed the lane while the edit
                    # was in flight.  Re-register the edited bubble so the
                    # retryable cleanup path can remove it deterministically.
                    states[lane] = state
                    await self._kanban_discard_worker_focus_state(
                        lane, adapter, sub
                    )
                    continue
                if getattr(result, "success", False):
                    state.update(
                        content=content,
                        task_id=task.id,
                        task_title=str(getattr(task, "title", "") or "")[:96],
                        board=board,
                        attempt_id=attempt_id,
                        activity_text=activity_text,
                        activity_updated_at=activity_updated_at,
                        last_sent_at=now,
                        claim_sequence=claim_sequence,
                    )
                    continue
                if result is None or getattr(result, "retryable", False):
                    continue
            try:
                result = await adapter.send(sub["chat_id"], content, metadata=metadata)
            except Exception:
                logger.debug("kanban worker focus send failed", exc_info=True)
                continue
            if getattr(result, "success", False) and getattr(result, "message_id", None):
                states[lane] = {
                    "message_id": str(result.message_id),
                    "content": content,
                    "task_id": task.id,
                    "task_title": str(getattr(task, "title", "") or "")[:96],
                    "board": board,
                    "attempt_id": attempt_id,
                    "activity_text": activity_text,
                    "activity_updated_at": activity_updated_at,
                    "last_sent_at": now,
                    "claim_sequence": claim_sequence,
                    "sub": sub,
                }
                if not self._kanban_worker_display_available(lane, claim_sequence):
                    # Claim may arrive while Telegram is creating the bubble,
                    # before there is state for the claim path to delete.  The
                    # post-send fence closes that window and retains failures
                    # for the normal cleanup retry.
                    await self._kanban_discard_worker_focus_state(
                        lane, adapter, sub
                    )

        for key in list(states):
            if key in lane_candidates:
                continue
            state = states.get(key) or {}
            sub = state.get("sub") or {}
            if not state.get("message_id"):
                states.pop(key, None)
                continue
            try:
                platform = _Platform(str(sub.get("platform") or "").lower())
            except ValueError:
                states.pop(key, None)
                continue
            profile = str(sub.get("notifier_profile") or "").strip() or None
            adapter = self._authorization_adapter(platform, profile)
            if adapter is None:
                continue
            # Worker finished terminally (lane still presentable): leave a
            # trace with its closeout instead of erasing every sign of work —
            # a topic opened later used to look like nothing ever ran.
            # Completed AND blocked both trace (spec: a blocked worker
            # publishes its closeout with the reason); retry/reclaim/crash
            # keep the silent delete. Only THIS bubble's exit is consumed —
            # other workers' exits stay for the per-worker trace pass below.
            exit_key = (str(state.get("board") or ""), *key)
            lane_exits = exits.get(exit_key) or {}
            exit_info = lane_exits.pop(str(state.get("task_id") or ""), None)
            if not lane_exits:
                exits.pop(exit_key, None)
            if isinstance(exit_info, dict):
                exit_kind = str(exit_info.get("kind") or "")
            else:
                exit_kind = str(exit_info or "")
            trace_enabled = bool(
                resolve_display_setting(
                    _config_for_profile(profile),
                    str(getattr(platform, "value", platform)).lower(),
                    "worker_rotation_trace",
                    True,
                )
            )
            state_sequence = int(state.get("claim_sequence") or 0)
            traceable = trace_enabled and exit_kind in {"completed", "blocked"}
            if traceable and self._kanban_worker_display_available(
                key, state_sequence
            ):
                trace_url_template = str(
                    resolve_display_setting(
                        _config_for_profile(profile),
                        str(getattr(platform, "value", platform)).lower(),
                        "worker_rotation_trace_url",
                        "",
                    )
                    or ""
                )
                await self._kanban_finalize_worker_focus_state(
                    key,
                    adapter,
                    trace_url_template=trace_url_template,
                    exit_kind=exit_kind,
                )
            else:
                await self._kanban_discard_worker_focus_state(key, adapter, sub)
                if traceable and isinstance(exit_info, dict):
                    # Principal reclaimed the lane while this worker was
                    # finishing: the bubble goes away, but the closeout is
                    # still owed — hand the exit to the per-worker trace
                    # pass so it is published as its own message.
                    exits.setdefault(exit_key, {})[
                        str(state.get("task_id") or "")
                    ] = exit_info

        # Per-worker closeout traces (TARGET_ARCHITECTURE gap 5). Exits not
        # consumed by the bubble teardown above belong to workers that
        # finished WITHOUT holding the focus bubble — the second of two
        # simultaneous finishers, a worker that ended while another held the
        # focus, or one that ended after the principal reclaimed the lane.
        # Each completed/blocked one publishes its own closeout message;
        # other exit kinds (retry/reclaim/crash/stale) stay silent, matching
        # the bubble path. Best-effort: a failed send drops the exit rather
        # than retrying forever.
        for exit_key in list(exits):
            pending = exits.get(exit_key) or {}
            if len(exit_key) != 5:
                exits.pop(exit_key, None)
                continue
            exit_board = str(exit_key[0] or "")
            for exit_task_id in list(pending):
                info = pending.pop(exit_task_id, None)
                if not isinstance(info, dict):
                    continue
                kind = str(info.get("kind") or "")
                if kind not in {"completed", "blocked"}:
                    continue
                sub = info.get("sub") or {}
                try:
                    platform = _Platform(
                        str(sub.get("platform") or exit_key[1] or "").lower()
                    )
                except ValueError:
                    continue
                profile = (
                    str(sub.get("notifier_profile") or "").strip() or None
                )
                user_config = _config_for_profile(profile)
                indicator = _resolve_activity_indicator_settings(
                    user_config, platform.value, 180.0
                )
                if not indicator.worker_rotation:
                    continue
                if not bool(
                    resolve_display_setting(
                        user_config,
                        platform.value,
                        "worker_rotation_trace",
                        True,
                    )
                ):
                    continue
                adapter = self._authorization_adapter(platform, profile)
                if adapter is None or not sub.get("chat_id"):
                    continue
                trace_url_template = str(
                    resolve_display_setting(
                        user_config,
                        platform.value,
                        "worker_rotation_trace_url",
                        "",
                    )
                    or ""
                )
                summary = await asyncio.to_thread(
                    _read_worker_trace_summary, exit_board, exit_task_id, kind
                )
                content = _render_worker_trace_content(
                    kind=kind,
                    title=str(info.get("title") or ""),
                    board=exit_board,
                    task_id=exit_task_id,
                    run_id=info.get("run_id"),
                    summary=summary,
                    trace_url_template=trace_url_template,
                )
                metadata = (
                    dict(sub.get("delivery_metadata"))
                    if isinstance(sub.get("delivery_metadata"), dict)
                    else {}
                )
                if sub.get("thread_id") and not metadata.get("thread_id"):
                    metadata["thread_id"] = sub["thread_id"]
                try:
                    await adapter.send(
                        sub["chat_id"], content, metadata=metadata
                    )
                except Exception:
                    logger.debug(
                        "per-worker closeout trace send failed", exc_info=True
                    )
            if not pending:
                exits.pop(exit_key, None)

        for lane in list(scopes):
            if lane not in all_active_lanes and lane not in states:
                scopes.pop(lane, None)

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver material lifecycle events.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the user-facing notification set. Multiple
        internal transitions for the same task are converged to the latest
        material state before delivery; retries/timeouts/crashes remain in the
        event log but are not chat notifications. Sends at most one message per
        task per poll to ``(platform, chat_id, thread_id)``,
        then advances the cursor. The subscription is removed only when the
        task is ``archived``. A ``done`` task can be reopened for review or
        continuation, so its subscription and origin-session ownership must
        survive completion. Cursor advancement prevents old events replaying
        when that happens.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Each gateway polls only subscriptions owned by profiles whose
        adapters it hosts. The dispatch-owning gateway also handles legacy
        subscriptions without a profile stamp.
        """
        # Dispatch and delivery have separate ownership. A deployment may run
        # one dispatcher while each profile has its own gateway credentials;
        # those adapter-owning gateways must still poll and deliver their own
        # subscriptions. Legacy rows without a notifier_profile are visible
        # only while this process holds the actual singleton dispatcher lock.
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: dependencies not importable; notifier disabled")
            return

        # "status" covers dashboard drag-drop and `_set_status_direct()`
        # writes — surface those transitions to subscribers too.
        # ``review_requested`` wakes the origin subscriber like a block does,
        # but is not a block (see kanban_db.request_review); the task is not
        # archived, so the subscription stays alive and later review
        # cycles keep notifying.
        # ``claimed`` is the proof boundary between "queued" and material
        # execution.  Without it a long-running headless worker stays silent
        # until completion, which makes an automatically started task look
        # stalled and forces the operator to ask for status.  Heartbeats remain
        # intentionally excluded to avoid one notification per minute.
        NOTIFY_KINDS = (
            "claimed", "completed", "blocked", "gave_up", "status",
            "block_loop_detected", "review_requested",
        )
        # Focus accounting consumes worker-run boundaries too, but these
        # internal retry/recovery events must never become chat messages.
        FOCUS_KINDS = (
            "claimed", "completed", "blocked", "gave_up",
            "block_loop_detected", "review_requested", "changes_requested",
            "archived", "unblocked", "timed_out", "crashed", "rate_limited",
            "stale", "reclaimed", "status",
        )
        CLAIM_KINDS = tuple(dict.fromkeys((*NOTIFY_KINDS, *FOCUS_KINDS)))
        # Subscriptions are removed only when the task reaches the irreversible
        # archived status. ``done`` is reversible in review/controller flows,
        # so removing its subscription would silence a later reopen. We used
        # to also unsubscribe on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is archived lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        # Raised from 3 to 12 (~60s at the 5s tick cadence): now that a
        # reported SendResult(success=False) also lands here (see the
        # delivery loop below), a transient Telegram/API outage of a few
        # ticks must NOT permanently unsubscribe a live review-gate channel.
        # A genuinely dead chat still drops, just ~60s later — a fine trade
        # for an unattended gate where a false drop means silent work pileup.
        MAX_SEND_FAILURES = 12
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        # Board-level focus cursors (per slug, in-memory): the display feed
        # below reads task_events independently of notify subscriptions.
        focus_cursors: dict[str, int] = getattr(
            self, "_kanban_focus_event_cursors", {}
        )
        self._kanban_focus_event_cursors = focus_cursors
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC cadence. Subscriptions survive ``done`` (it is
        # reversible), so boards that never archive would otherwise
        # accumulate rows scanned on every 5s tick forever. The sweep is a
        # single DELETE per board, gated to once per watcher startup and at
        # most once per hour thereafter — cheap relative to the tick's own
        # per-sub claims. Retention is kanban.done_sub_retention_days in
        # config.yaml (default 30; 0 disables), re-read at each sweep so a
        # config change applies without a restart.
        _GC_INTERVAL_SECONDS = 3600.0
        _gc_next_at = 0.0  # 0 → sweep on the first tick after startup

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _gc_retention_days = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    try:
                        _kanban_cfg = (_load_config() or {}).get("kanban") or {}
                        _gc_retention_days = int(
                            _kanban_cfg.get("done_sub_retention_days", 30)
                        )
                    except Exception:
                        # Fail safe on the shipped default; the sweep itself
                        # treats <= 0 as disabled.
                        _gc_retention_days = 30
                agent_wake_on_events = _resolve_agent_wake_on_events(_load_config)
                # Track worker lifecycle regardless of the display gate. This
                # is the existing notifier poll, not a second DB reader; keeping
                # the in-memory queue warm lets profile/platform config toggle
                # without losing workers that were already running.
                rehydrate_worker_focus = not bool(
                    getattr(self, "_kanban_worker_focus_rehydrated", False)
                )
                gateway_started_at = float(
                    getattr(self, "_gateway_started_at", 0.0) or 0.0
                )

                def _collect():
                    deliveries: list[dict] = []
                    focus_rows: list[dict] = []
                    rehydrate_complete = True
                    include_unowned = self._owns_kanban_dispatcher_lock()
                    notifier_profiles = {notifier_profile}
                    notifier_profiles.update(
                        str(profile).strip()
                        for profile in getattr(self, "_profile_adapters", {})
                        if str(profile).strip()
                    )
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    # Widen to every platform any secondary profile has live,
                    # not just the default profile's. This is only a coarse
                    # pre-filter to skip claiming events for subs nobody can
                    # possibly deliver — the precise per-profile check (via
                    # gateway/authz_mixin.py::_authorization_adapter, which
                    # forbids default-profile fallback) still runs at delivery
                    # time below, rewinding the claim if it resolves to None.
                    # Without this, a subscription owned by a secondary
                    # profile on a platform the DEFAULT profile never
                    # connected (e.g. beta owns discord, default doesn't) was
                    # dropped here before ever being claimed — no rewind
                    # applies to an unclaimed event, so it silently never
                    # retries.
                    for _profile_adapter_map in getattr(self, "_profile_adapters", {}).values():
                        active_platforms.update(
                            getattr(platform, "value", str(platform)).lower()
                            for platform in _profile_adapter_map.keys()
                        )
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries, focus_rows, False
                    display_targets = self._kanban_board_display_targets(
                        notifier_profiles
                    )

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        if rehydrate_worker_focus:
                            rehydrate_complete = False
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        # Board-level focus feed: worker rotation must see
                        # lifecycle on every topic-bound board even with zero
                        # notify subscriptions — the skip below only silences
                        # CHAT delivery. Cheap read-only probe per tick; a
                        # writable task fetch happens only when new focus
                        # events actually exist.
                        board_targets = display_targets.get(slug) or []
                        if board_targets:
                            try:
                                focus_cursor = focus_cursors.get(slug)
                                new_events_by_task: dict[str, list] = {}
                                bootstrap_feed = focus_cursor is None
                                ro_conn = sqlite3.connect(
                                    f"file:{Path(resolved_db_path).as_posix()}?mode=ro",
                                    uri=True,
                                    timeout=2,
                                )
                                ro_conn.row_factory = sqlite3.Row
                                try:
                                    max_row = ro_conn.execute(
                                        "SELECT COALESCE(MAX(id), 0) AS id "
                                        "FROM task_events"
                                    ).fetchone()
                                    latest_focus_event_id = int(max_row["id"] or 0)
                                    if bootstrap_feed:
                                        running_rows = ro_conn.execute(
                                            "SELECT id FROM tasks "
                                            "WHERE status = 'running' "
                                            "  AND current_run_id IS NOT NULL"
                                        ).fetchall()
                                        for task_row in running_rows:
                                            new_events_by_task[str(task_row["id"])] = []
                                    elif latest_focus_event_id > focus_cursor:
                                        placeholders = ",".join("?" * len(FOCUS_KINDS))
                                        event_rows = ro_conn.execute(
                                            "SELECT id, task_id, kind "
                                            "FROM task_events WHERE id > ? "
                                            f"AND kind IN ({placeholders}) "
                                            "ORDER BY id",
                                            (focus_cursor, *FOCUS_KINDS),
                                        ).fetchall()
                                        for event_row in event_rows:
                                            new_events_by_task.setdefault(
                                                str(event_row["task_id"]), []
                                            ).append(event_row)
                                finally:
                                    ro_conn.close()
                                focus_cursors[slug] = latest_focus_event_id
                                if new_events_by_task:
                                    from types import SimpleNamespace as _NS

                                    with _kb.connect_closing(board=slug) as focus_conn:
                                        for task_id, task_events in new_events_by_task.items():
                                            focus_task = _kb.get_task(focus_conn, task_id)
                                            if focus_task is None:
                                                continue
                                            feed_events = [
                                                _NS(
                                                    kind=str(ev["kind"]),
                                                    id=int(ev["id"]),
                                                    created_at=0.0,
                                                )
                                                for ev in task_events
                                            ]
                                            for target in board_targets:
                                                focus_rows.append({
                                                    "sub": dict(target),
                                                    "task": focus_task,
                                                    "board": slug,
                                                    "bootstrap": bootstrap_feed,
                                                    "events": feed_events,
                                                })
                            except Exception as focus_exc:  # noqa: BLE001
                                logger.debug(
                                    "kanban notifier: board focus feed failed "
                                    "for %s: %s",
                                    slug,
                                    focus_exc,
                                )
                        # Zero-subscription early exit: probe the board with a
                        # cheap read-only connection BEFORE the writable
                        # `connect()`. A board with no subscriptions has
                        # nothing to notify, and the writable open (schema
                        # init/migration on first open, WAL/-shm sidecars,
                        # checkpoint traffic) is exactly the per-tick cost
                        # this skip avoids.
                        try:
                            if _kb.count_notify_subs(
                                board=slug,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            ) == 0:
                                logger.debug(
                                    "kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                                    slug, sorted(notifier_profiles),
                                )
                                continue
                        except Exception as exc:
                            logger.debug(
                                "kanban notifier: read-only subscription probe failed "
                                "for board %s (%s); falling back to writable open",
                                slug, exc,
                            )
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            if rehydrate_worker_focus:
                                rehydrate_complete = False
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            if _gc_due:
                                # Hourly (plus once at startup) stale-sub GC:
                                # drop subscriptions for tasks that have been
                                # ``done`` untouched past the retention
                                # window. Best-effort — a failed sweep never
                                # blocks delivery; the next hourly gate
                                # retries it.
                                try:
                                    _purged = _kb.purge_stale_done_notify_subs(
                                        conn,
                                        max_age_days=_gc_retention_days,
                                    )
                                    if _purged:
                                        logger.info(
                                            "kanban notifier: purged %d stale done-task subscription(s) on board %s (retention %dd)",
                                            _purged, slug, _gc_retention_days,
                                        )
                                except Exception as _gc_exc:
                                    logger.debug(
                                        "kanban notifier: stale-sub GC failed for board %s: %s",
                                        slug, _gc_exc,
                                    )
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            )
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                try:
                                    owner_profile = sub.get("notifier_profile") or None
                                    if owner_profile and owner_profile != notifier_profile:
                                        _owner_adapters = getattr(self, "_profile_adapters", {}).get(owner_profile)
                                        if not _owner_adapters:
                                            logger.debug(
                                                "kanban notifier: subscription for %s owned by profile %s; current profile %s has no adapter for it, skipping",
                                                sub.get("task_id"), owner_profile, notifier_profile,
                                            )
                                            continue
                                    platform = (sub.get("platform") or "").lower()
                                    if platform not in active_platforms:
                                        logger.debug(
                                            "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                            sub.get("task_id"), platform or "<missing>",
                                        )
                                        continue
                                    task = None
                                    if rehydrate_worker_focus:
                                        task = _kb.get_task(conn, sub["task_id"])
                                        if task and task.status == "running":
                                            focus_rows.append({
                                                "sub": sub,
                                                "task": task,
                                                "board": slug,
                                                "bootstrap": True,
                                                "events": [],
                                            })
                                    old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=CLAIM_KINDS,
                                    )
                                    if not events:
                                        continue
                                    # A gateway boot must never replay historical
                                    # lifecycle events into chat.  The atomic claim
                                    # above already advances the cursor, so filtering
                                    # the claimed range here safely converges stale
                                    # subscriptions without sending or waking.
                                    subscription_created_at = float(
                                        sub.get("created_at") or 0.0
                                    )
                                    delivery_cutoff = max(
                                        gateway_started_at,
                                        subscription_created_at,
                                    )
                                    current_events = [
                                        ev for ev in events
                                        if float(ev.created_at or 0.0) >= delivery_cutoff
                                    ]
                                    if not current_events:
                                        logger.info(
                                            "kanban notifier: suppressed %d pre-start "
                                            "event(s) for one subscription on board %s",
                                            len(events), slug,
                                        )
                                        continue
                                    events = current_events
                                    if task is None:
                                        task = _kb.get_task(conn, sub["task_id"])
                                    focus_events = [
                                        ev for ev in events if ev.kind in FOCUS_KINDS
                                    ]
                                    if focus_events:
                                        focus_rows.append({
                                            "sub": sub,
                                            "task": task,
                                            "board": slug,
                                            "bootstrap": False,
                                            "events": focus_events,
                                        })

                                    # Chat notification is a state convergence,
                                    # never an event-log replay. Counter-only retry
                                    # boundaries above stay invisible to the user.
                                    notify_events = [
                                        ev for ev in events if ev.kind in NOTIFY_KINDS
                                    ]
                                    archived_events = [
                                        ev for ev in events if ev.kind == "archived"
                                    ]
                                    # Archive is silent but authoritative for
                                    # subscription cleanup. When it is the only
                                    # material event, pass it through the
                                    # delivery pipeline so the cursor advances
                                    # and the subscription is removed. If a
                                    # completion and archive share one claim,
                                    # preserve the completion notification and
                                    # let the task's archived state drive the
                                    # cleanup after delivery.
                                    if not notify_events and archived_events:
                                        notify_events = archived_events[-1:]
                                    if not notify_events:
                                        continue
                                    first_claim_row = conn.execute(
                                        "SELECT MIN(id) AS id FROM task_events "
                                        "WHERE task_id = ? AND kind = 'claimed'",
                                        (sub["task_id"],),
                                    ).fetchone()
                                    first_claim_id = (
                                        int(first_claim_row["id"])
                                        if first_claim_row and first_claim_row["id"] is not None
                                        else None
                                    )
                                    material_events = [
                                        ev for ev in notify_events
                                        if ev.kind != "claimed" or ev.id == first_claim_id
                                    ]
                                    events = material_events[-1:]
                                    if not events:
                                        continue
                                    logger.debug(
                                        "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                        len(events), sub["task_id"], slug, old_cursor, cursor,
                                    )
                                    deliveries.append({
                                        "sub": sub,
                                        "old_cursor": old_cursor,
                                        "cursor": cursor,
                                        "events": events,
                                        "task": task,
                                        "board": slug,
                                    })
                                except Exception as sub_exc:
                                    # Isolate per-subscription failures so one
                                    # bad subscription cannot block delivery for
                                    # all other subscriptions in this tick.
                                    if rehydrate_worker_focus:
                                        rehydrate_complete = False
                                    logger.warning(
                                        "kanban notifier: subscription for %s on board %s failed: %s",
                                        sub.get("task_id"), slug, sub_exc,
                                    )
                        finally:
                            conn.close()
                    return deliveries, focus_rows, rehydrate_complete

                deliveries, focus_rows, rehydrate_complete = await asyncio.to_thread(
                    _collect
                )
                if rehydrate_worker_focus and rehydrate_complete:
                    self._kanban_worker_focus_rehydrated = True
                self._kanban_focus_apply_rows(focus_rows)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = self._authorization_adapter(plat, sub_profile or None)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    # Per-subscription failure-counter key. Hoisted out of the
                    # event loop: the wake self-post path (in the loop's
                    # ``else`` clause) needs it even when every event in the
                    # claim was skipped before reaching the send site.
                    sub_key = (
                        sub["task_id"], sub["platform"],
                        sub["chat_id"], sub.get("thread_id") or "",
                    )
                    mode = sub.get("delivery_mode") or "notify"
                    wake_agent = mode in ("notify+wake", "wake")
                    send_passive = mode != "wake"
                    # Worker handoff carried into the synthetic wake turn below
                    # (#70752): without it the woken creator only sees
                    # "Task X completed" and re-decomposes work that already
                    # exists on the board.
                    wake_handoff = ""
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "claimed":
                            msg = (
                                f"▶ {board_tag}{tag}Kanban {sub['task_id']} started"
                                f" — {title}"
                            )
                        elif kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                                wake_handoff = h
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                                wake_handoff = r
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {board_tag}{tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        elif kind == "status":
                            new_status = ""
                            if ev.payload and ev.payload.get("status"):
                                new_status = str(ev.payload["status"])
                            msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
                        elif kind == "review_requested":
                            # Implementation complete; task moved to the
                            # first-class review lane. Wake the origin thread.
                            handoff = ""
                            if ev.payload and ev.payload.get("summary"):
                                handoff = f"\n{str(ev.payload['summary'])[:200]}"
                            msg = (
                                f"👀 {board_tag}{tag}Kanban {sub['task_id']} ready for review"
                                f" — {title}{handoff}"
                            )
                        elif kind == "block_loop_detected":
                            # A task re-blocked for the same cause past the
                            # recurrence limit and was routed to `triage` for a
                            # human decision. This is the ONE transition that
                            # exists to force human attention, yet it emits no
                            # `blocked`/`status` event — so before adding it to
                            # TERMINAL_KINDS it produced zero notification and
                            # the task stalled in triage silently. Ping loudly.
                            reason = ""
                            recurrences = None
                            if ev.payload:
                                if ev.payload.get("reason"):
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                                recurrences = ev.payload.get("recurrences")
                            rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
                            msg = (
                                f"🛑 {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
                                f" — needs a human decision{rc}{reason}"
                            )
                        else:
                            # archived / unblocked are claimed by TERMINAL_KINDS
                            # (so the cursor advances past them and they can't
                            # wedge a later completed/blocked event behind an
                            # unclaimed row) but are intentionally SILENT: an
                            # archive needs no user ping, and unblocked is an
                            # internal transition. They are also excluded from
                            # _WAKE_KINDS below, so they never wake the creator.
                            continue
                        delivery_metadata = sub.get("delivery_metadata")
                        metadata: dict[str, Any] = (
                            dict(delivery_metadata)
                            if isinstance(delivery_metadata, dict)
                            else {}
                        )

                        if sub.get("thread_id") and not metadata.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        # Adapters with no push channel (the API server —
                        # ``supports_async_delivery = False``) can NEVER
                        # satisfy a text-send: ``send()`` always reports
                        # SendResult(success=False) by design (see
                        # ApiServerAdapter.send()). Treating that as a
                        # delivery failure would rewind/drop the subscription
                        # forever and — because the wake dispatch below lives
                        # in this loop's ``else`` clause — would also make the
                        # wake-on-completion path (the actual fix for the
                        # api_server wrong-session bug) unreachable. So for
                        # non-push adapters, skip the doomed send attempt
                        # entirely: there is nothing to text-notify, the
                        # creator is woken via the self-post below instead.
                        from gateway.wake import adapter_supports_push

                        if not adapter_supports_push(adapter) and wake_agent:
                            logger.debug(
                                "kanban notifier: adapter %s has no push "
                                "channel; skipping text ping for %s, relying "
                                "on wake self-post instead",
                                platform_str, sub["task_id"],
                            )
                            # Do NOT reset the failure counter here: on this
                            # path the wake self-post below IS the delivery,
                            # so the counter is resolved (reset or bumped) by
                            # the self-post outcome, not by skipping the send.
                            continue
                        if not send_passive:
                            # Wake-only subscriptions intentionally skip the
                            # visible platform message. The retained wake path
                            # below is the sole delivery — the failure counter
                            # is resolved (reset or bumped) by the wake
                            # outcome there, not by skipping the send here.
                            continue
                        try:
                            _send_res = await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            # A SendResult(success=False) without an exception
                            # (returned by push-capable adapters on a genuine
                            # transient failure) must count as a FAILED
                            # delivery — otherwise the cursor advances and the
                            # event is permanently lost. Adapters returning
                            # None (or anything non-SendResult shaped) keep
                            # the legacy "no exception == delivered" contract.
                            if getattr(_send_res, "success", True) is False:
                                raise RuntimeError(
                                    "adapter send() reported failure: "
                                    f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                                )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await _to_thread_process_service(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All text pings delivered (or intentionally skipped
                        # for non-push adapters, whose delivery is the wake
                        # self-post below). Whether the cursor may advance now
                        # depends on the adapter class:
                        #
                        # * push-capable: the text send WAS the delivery, so
                        #   advance immediately (pre-existing behavior); the
                        #   wake injection below stays best-effort.
                        # * non-push (api_server): the wake self-post IS the
                        #   delivery. Advancing first would let a failed /
                        #   retry-exhausted self-post (swallowed by the
                        #   best-effort except) permanently lose the event.
                        #   So the self-post runs FIRST and the cursor only
                        #   advances after it succeeds — a failure rewinds the
                        #   claim exactly like a failed send() above, so the
                        #   next tick retries.
                        task_terminal = task and task.status == "archived"
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = (
                            {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                            if wake_agent and agent_wake_on_events
                            else set()
                        )
                        from gateway.wake import adapter_supports_push as _adapter_push_ok

                        _is_push_adapter = _adapter_push_ok(adapter)
                        _session_key = ""
                        _synth = ""
                        if _wake_kinds:
                            if _is_push_adapter:
                                _session_key = getattr(task, "session_id", None) or ""
                            else:
                                # Non-push (api_server) wakes go to the
                                # subscription's delivery destination —
                                # sub["chat_id"] IS the raw session id the
                                # subscriber registered with. task.session_id
                                # is worker/creator provenance and may point
                                # at a WORKER session for child tasks with
                                # inherited subscriptions; falling back to it
                                # only when chat_id is empty (legacy rows).
                                _session_key = (
                                    sub["chat_id"]
                                    or getattr(task, "session_id", None)
                                    or ""
                                )
                        if _wake_kinds:
                            _title = (task.title if task else sub["task_id"])[:120]
                            _assignee = task.assignee if task else ""
                            _parts = []
                            if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                            if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                            if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                            if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                            if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                            _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                            _synth = t(
                                "gateway.kanban.wake.message",
                                task_id=sub["task_id"],
                                status=_status,
                                title=_title,
                                assignee=_assignee,
                                board=board_slug,
                            )
                            # Graph-safe wake turn (#70752): carry the worker's
                            # completion handoff into the synthetic turn and
                            # label it as an automatic notification so the woken
                            # creator inspects the board instead of
                            # re-decomposing work that already exists.
                            if wake_handoff:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.handoff",
                                    summary=wake_handoff,
                                )
                            _synth += "\n\n" + t(
                                "gateway.kanban.wake.guidance"
                            )

                        if not _is_push_adapter and _wake_kinds and _session_key:
                            # Wake self-post IS the delivery on this path —
                            # it must succeed BEFORE the cursor advances.
                            from gateway.wake import deliver_wake

                            try:
                                await deliver_wake(
                                    adapter,
                                    text=_synth,
                                    session_id=_session_key,
                                )
                                logger.info(
                                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                )
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake self-post failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the self-post — the event
                                    # is NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        async def _push_wake() -> None:
                            """Wake the creator session behind a push adapter.

                            Shared by the wake-only (pre-advance, delivery)
                            and notify+wake (post-advance, best-effort)
                            branches below; raises on failure so the caller
                            decides whether to rewind or merely log.
                            """
                            from gateway.session import SessionSource
                            from gateway.wake import deliver_wake
                            # Rebuild the creator's real session scope from
                            # the chat_type persisted on the subscription
                            # row (#56580). build_session_key() keys DMs
                            # (":dm:<chat_id>") on a wholly different shape
                            # from group/thread, so the old hardcoded
                            # "group" mis-routed DM/thread creators into a
                            # fresh session. Legacy rows written before the
                            # column existed may still carry chat_type in
                            # delivery_metadata (#60600 rows) — fall back
                            # to that, then to "group" (the historical
                            # default that suits the dashboard/group flows).
                            # handle_message() get_or_create_session's the
                            # target, so a mismatch only ever degrades to a
                            # fresh session, never an exception.
                            _chat_type = str(sub.get("chat_type") or "").strip()
                            if not _chat_type:
                                _delivery_meta = sub.get("delivery_metadata")
                                if isinstance(_delivery_meta, dict):
                                    _chat_type = str(
                                        _delivery_meta.get("chat_type") or ""
                                    ).strip()
                            _chat_type = _chat_type or "group"
                            _source = SessionSource(
                                platform=plat,
                                chat_id=sub["chat_id"],
                                chat_type=_chat_type,
                                thread_id=sub.get("thread_id") or None,
                                user_id=sub.get("user_id"),
                                user_id_alt=sub.get("user_id_alt"),
                                profile=sub_profile or None,
                                scope_id=_wake_scope_id(adapter, sub),
                            )
                            # deliver_wake preserves the synthetic
                            # MessageEvent/handle_message path for
                            # push-capable adapters (the non-push /
                            # self-post branch is handled BEFORE the
                            # cursor advance above).
                            await deliver_wake(
                                adapter,
                                text=_synth,
                                session_id=_session_key,
                                source=_source,
                            )
                            logger.info(
                                "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                            )

                        if _is_push_adapter and not send_passive and _wake_kinds:
                            # Wake-only (delivery_mode='wake') push sub: the
                            # text ping was intentionally skipped above, so
                            # the wake IS the sole delivery. It must succeed
                            # BEFORE the cursor advances — advancing first
                            # would let a failed wake (previously swallowed
                            # by the best-effort except below) permanently
                            # lose the event. Mirrors the non-push
                            # (api_server) self-post ordering above.
                            try:
                                await _push_wake()
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake-only delivery failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the wake — the event is
                                    # NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        # Delivery complete (text ping for push adapters, wake
                        # self-post for non-push, wake injection for wake-only
                        # push subs): advance cursor. The cursor is the dedup
                        # mechanism — it prevents re-delivery of the same
                        # event on subsequent ticks.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        if not _is_push_adapter:
                            # Nothing left to deliver on this path (the wake,
                            # if any, already succeeded above).
                            sub_fail_counts.pop(sub_key, None)
                        # Unsubscribe only on archive. Completion (``done``)
                        # remains reversible: controllers reopen completed
                        # work for review corrections and continuation. The
                        # retained cursor prevents replay while preserving the
                        # original delivery and wake ownership for that cycle.
                        if _is_push_adapter and send_passive and _wake_kinds:
                            # notify+wake: the text ping above was the
                            # delivery and the cursor has advanced; the wake
                            # injection stays best-effort.
                            try:
                                await _push_wake()
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await _to_thread_process_service(
                                self._kanban_unsub, sub, board_slug,
                            )
                await self._kanban_refresh_worker_focus()
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info("kanban dispatcher: max_in_progress=%s", max_in_progress)
        # When the operator never set kanban.max_in_progress, fall back to a
        # memory-derived default (OOF-30/OOF-77): unbounded fan-out on small
        # hosted VMs has repeatedly swap-thrashed the whole machine. Explicit
        # config always wins; None stays None on hosts where total memory
        # can't be read (macOS/Windows dev machines).
        effective_max_in_progress = _kb.resolve_max_in_progress(max_in_progress)
        if max_in_progress is None and effective_max_in_progress is not None:
            logger.info(
                "kanban dispatcher: kanban.max_in_progress unset; using "
                "memory-derived default max_in_progress=%d "
                "(set kanban.max_in_progress in config.yaml to override)",
                effective_max_in_progress,
            )
        max_in_progress = effective_max_in_progress

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # kanban.reconcile_orphans (config.yaml, default true): each tick,
        # requeue 'running' cards whose claim bookkeeping is broken (no
        # valid claim, dead/gone worker) — the zombie-card reconciliation
        # pass. Set false to keep orphans frozen for manual forensics.
        reconcile_orphans = bool(kanban_cfg.get("reconcile_orphans", True))

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                    reconcile_orphans=reconcile_orphans,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            # Only probe the review column when autonomous review dispatch is
            # actually on. With ``review_dispatch`` off (the default — no
            # sdlc-review agent), a task parked in 'review' is "correctly idle"
            # waiting for a human, not a stuck dispatcher; probing it here would
            # fire a false "dispatcher stuck" warning that never clears. Shares
            # the exact gate the dispatcher uses so the two can't drift.
            _review_probe = _kb.review_dispatch_enabled()
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _review_probe and _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await _to_thread_process_service(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Global emergency stop (`hermes pause`): skip auto-decompose
                # and dispatch entirely — no new workers while paused. Running
                # workers finish naturally; zombie reaping above still runs.
                if not _kanban_dispatch_allowed():
                    ready_pending = False
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live each tick so a user
                    # flipping kanban.auto_decompose=false to STOP runaway fan-out
                    # takes effect on the next tick, not on gateway restart (#49638).
                    _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                    if _ad_enabled:
                        await _to_thread_process_service(_auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(_tick_once)
                    any_spawned = False
                    for slug, res in (results or []):
                        if res is not None and getattr(res, "spawned", None):
                            any_spawned = True
                            # Quiet by default — only log when something actually
                            # happened, so an idle gateway stays silent.
                            logger.info(
                                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                                slug,
                                len(res.spawned),
                                res.reclaimed,
                                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                                res.promoted,
                                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                            )
                    # Health telemetry (aggregate across boards)
                    ready_pending = await _to_thread_process_service(_ready_nonempty)
                    if ready_pending and not any_spawned:
                        bad_ticks += 1
                    else:
                        bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        self._release_kanban_dispatcher_lock()
