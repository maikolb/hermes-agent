"""Kanban mirror cards for delegated subagents.

``delegate_task`` spawns children in-process, so nothing about that work
reaches the kanban board: the dispatcher, the board CLI/TUI and read-only
mirrors only see dispatcher-owned workers. When the delegating session is
bound to a project board (``HERMES_PROJECT_BOARD`` session env, set by the
gateway project router for bound topics), this module materialises one
kanban card per delegated child so the board stays the authority over ALL
running work, not just dispatcher fan-out.

Lifecycle: ``create_task`` (ready) followed immediately by a directed
``claim_task`` on the same connection, so the card sits in ``running``
claimed by the delegating gateway process. The claim carries a long TTL
(``_CLAIM_TTL_SECONDS``): while the delegation is alive the card cannot be
handed to a dispatcher worker, and if the whole process dies the claim
eventually expires and ``release_stale_claims`` returns the card to
``ready``, where a real dispatcher worker legitimately resumes the orphaned
goal. Terminal transitions: child ``completed`` maps to ``done`` (child
summary as result); anything else (``failed``/``interrupted``/``timeout``/
``error``) maps to ``blocked`` with kind ``needs_input`` so a human decides
between re-delegating and letting a dispatcher worker take over.

Behaviour contract mirrors ``delegation_live_log``: strictly best-effort.
Every entry point catches everything; a kanban failure must never break a
delegation.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TITLE_MAX = 140
_BODY_MAX = 4000
_SUMMARY_MAX = 4000
_REASON_MAX = 500

# Claim TTL for mirror cards. Long enough that a slow multi-hour delegation
# is never reclaimed mid-flight (there is no heartbeat and no live
# worker_pid on a mirror claim, so ``release_stale_claims`` cannot extend
# it), short enough that a crashed gateway frees the card the same day.
_CLAIM_TTL_SECONDS = 6 * 3600


def _author() -> str:
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.environ.get(env)
        if value:
            return value
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "delegation"
    except Exception:
        return "delegation"


def _cards_enabled() -> bool:
    """``delegation.kanban_cards`` config gate (default: enabled)."""
    try:
        from tools.delegate_tool import _load_config

        value = _load_config().get("kanban_cards", True)
    except Exception:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return True


def resolve_delegation_board() -> Optional[str]:
    """Board slug the current session is bound to, or ``None`` (no cards).

    Only sessions the gateway bound to a project board mirror their
    delegations; unbound sessions (personal DMs, management topics, plain
    CLI runs) change nothing.
    """
    try:
        if not _cards_enabled():
            return None
        from gateway.session_context import get_session_env

        board = (get_session_env("HERMES_PROJECT_BOARD", "") or "").strip()
        return board or None
    except Exception as exc:  # noqa: BLE001 - never break delegation
        logger.debug("delegation kanban: board resolution failed: %s", exc)
        return None


def create_delegation_cards(
    task_list: List[Dict[str, Any]],
    delegation_id: Optional[str],
    board: Optional[str],
    live_paths: Optional[List[str]] = None,
) -> Dict[int, str]:
    """Create one claimed running mirror card per delegated task.

    Returns ``{task_index: task_id}`` for every card that was created; on
    any failure the affected index is simply absent.
    """
    if not board or not task_list:
        return {}
    cards: Dict[int, str] = {}
    try:
        from hermes_cli import kanban_db as kb

        author = _author()
        with kb.connect_closing(board=board) as conn:
            for index, task in enumerate(task_list):
                try:
                    goal = str(task.get("goal") or "").strip()
                    if not goal:
                        goal = f"delegated task {index}"
                    context = str(task.get("context") or "").strip()
                    body = goal if not context else f"{goal}\n\n{context}"
                    task_id = kb.create_task(
                        conn,
                        title=goal[:_TITLE_MAX],
                        body=body[:_BODY_MAX],
                        created_by=author,
                        board=board,
                        idempotency_key=(
                            f"{delegation_id}:{index}" if delegation_id else None
                        ),
                    )
                    claimed = kb.claim_task(
                        conn, task_id, ttl_seconds=_CLAIM_TTL_SECONDS
                    )
                    if claimed is None:
                        logger.debug(
                            "delegation kanban: card %s not claimable "
                            "(dispatcher may have won the race)",
                            task_id,
                        )
                    note = [
                        f"Mirror card for in-process delegation "
                        f"{delegation_id or '(sync)'} task {index}, "
                        f"spawned by {author}."
                    ]
                    if live_paths and index < len(live_paths) and live_paths[index]:
                        note.append(f"Live transcript: {live_paths[index]}")
                    kb.add_comment(conn, task_id, author, " ".join(note))
                    cards[index] = task_id
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "delegation kanban: card create failed (task %s): %s",
                        index,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegation kanban: batch create failed: %s", exc)
    return cards


def close_delegation_cards(
    board: Optional[str],
    cards: Dict[int, str],
    results: List[Dict[str, Any]],
) -> None:
    """Terminal transition per mirror card from the aggregated results."""
    if not board or not cards:
        return
    try:
        from hermes_cli import kanban_db as kb

        author = _author()
        with kb.connect_closing(board=board) as conn:
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                index = entry.get("task_index")
                task_id = cards.get(index)
                if not task_id:
                    continue
                try:
                    status = str(entry.get("status") or "")
                    summary = str(entry.get("summary") or "").strip()
                    # Terminal statuses stamped by _execute_and_aggregate:
                    # "completed" | "failed" | "interrupted", plus
                    # "timeout" | "error" from the exceptional path.
                    if status == "completed":
                        kb.complete_task(
                            conn,
                            task_id,
                            result=(
                                summary[:_SUMMARY_MAX]
                                or "delegated subagent finished"
                            ),
                        )
                    else:
                        error = str(
                            entry.get("error")
                            or entry.get("exit_reason")
                            or status
                            or "failed"
                        )
                        kb.block_task(
                            conn,
                            task_id,
                            reason=(
                                f"delegation {status or 'failed'}: {error}"
                            )[:_REASON_MAX],
                            kind="needs_input",
                        )
                        if summary:
                            kb.add_comment(
                                conn, task_id, author, summary[:_SUMMARY_MAX]
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "delegation kanban: card close failed (%s): %s",
                        task_id,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegation kanban: batch close failed: %s", exc)
