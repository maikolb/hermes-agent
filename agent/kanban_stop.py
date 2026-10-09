"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

The Principal (an internal wake for a card) gets the same guard while that
card still has decisions it owns, within a bounded window per wake.
"""

from __future__ import annotations

import os
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2

# PRINCIPAL_TURN_BOUND_20261009: the Principal is one shared session. A wake may
# keep it on its card's pending decisions for this long; after that the
# decisions stay pending and go back to the fair notify queue, instead of
# stalling every other card and paying a full-context call per nudge.
_PRINCIPAL_CONTINUATION_SECONDS = 20 * 60
_principal_wakes: dict[tuple[str, str], float] = {}
_now = time.monotonic


def _principal_window_open(delivery_id, task_id) -> bool:
    now = _now()
    for key, started in list(_principal_wakes.items()):
        if now - started > 2 * _PRINCIPAL_CONTINUATION_SECONDS:
            _principal_wakes.pop(key, None)
    started = _principal_wakes.setdefault((str(delivery_id or ''), task_id), now)
    if now - started < _PRINCIPAL_CONTINUATION_SECONDS:
        return True
    logger.warning('principal continuation released after %ds: task=%s delivery=%s',
                   int(now - started), task_id, delivery_id)
    return False


def _principal_continuation(receipt):
    """A trusted internal wake owns its pending decisions, not just its delivery ACK."""
    from gateway.wake import current_notify_receipt
    receipt = current_notify_receipt.get() or receipt or {}
    task_id = receipt.get('principal_task_id')
    if not task_id or not receipt.get('db_path'):
        return None
    try:
        with sqlite3.connect(Path(receipt['db_path']).resolve().as_uri()+'?mode=ro', uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute('SELECT status,block_kind FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task or task['status'] in ('done','archived'):
                return None
            human_wait = conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND status='human'", (task_id,)).fetchone()
            rows = [dict(row) for row in conn.execute(
                "SELECT id,question,answer,context FROM nfos_decisions WHERE task_id=? AND status='pending' ORDER BY created_at", (task_id,))]
    except (OSError,sqlite3.Error) as exc:
        if not _principal_window_open(receipt.get('delivery_id'), task_id):
            return None
        return f'[NFOS: recovery state could not be read ({type(exc).__name__}). Retry the existing board read before reporting completion.]'
    if not rows:
        return None
    pending = []
    for row in rows:
        try:
            context = json.loads(row['context'] or '{}')
        except (ValueError,TypeError):
            context = {}
        context = context if isinstance(context,dict) else {}
        lab_wait = context.get('lab_wait')
        if isinstance(lab_wait, dict) and lab_wait.get('hold'):
            continue  # LAB_WAIT_20261008: sweep_lab_waits answers it; it is not this turn's work
        if human_wait and not (context.get('owner_guidance') or context.get('human_reply')):
            continue
        pending.append({'id':row['id'],'question':row['question'],'last_response':row['answer'],
                        'recovery':context.get('maintenance_recovery')})
    if not pending or not _principal_window_open(receipt.get('delivery_id'), task_id):
        return None
    return ('[NFOS: this Principal turn still owns unfinished decisions. Continue in this SAME turn/session '
            'and its remaining budget; an explanatory answer is not execution.\n'
            f'Board database: {receipt["db_path"]}; card: {task_id}. Pending: '+json.dumps(pending,ensure_ascii=False)+'\n'
            'Execute the next authorized administrative action now through the existing tools. For a busy lock/exit75, '
            'wait with bounded backoff and retry the unexecuted operation. Inspect existing calls/receipts first; '
            'never duplicate an in-flight or uncertain external effect. Repair the cause, read back the actual result, '
            'then use the native repair/resume route and resolve the decision. Do not restart a held worker to perform '
            'its own administrative repair. If a permission, finite budget grant or access is genuinely indispensable '
            'and unavailable after checking authorized routes, record that concrete human dependency via decide; '
            'never invent a grant, credential, repaired receipt or broaden scope. Do not end with a future-action promise.]')


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    principal_receipt: Optional[dict] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return _principal_continuation(principal_receipt)
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
