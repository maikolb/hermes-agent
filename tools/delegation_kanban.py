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
handed to a dispatcher worker. If the whole process dies, delegation
recovery archives the dead attempt's cards on the next boot
(``archive_stale_delegation_cards``) and the principal re-delegates — a
single resume path, so a TTL-expired card never races a re-delegation into
duplicate work (28/08 concursa-ai: both paths ran and tripled the cards). Terminal transitions: child ``completed`` maps to ``done`` (child
summary as result); anything else (``failed``/``interrupted``/``timeout``/
``error``) maps to ``blocked`` with kind ``needs_input`` so a human decides
between re-delegating and letting a dispatcher worker take over.

Behaviour contract mirrors ``delegation_live_log``: strictly best-effort.
Every entry point catches everything; a kanban failure must never break a
delegation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from tools.closeout_guard import (
    closeout_rewrite_enabled,
    looks_like_status_not_closeout,
)

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


def _display_worker_rotation() -> bool:
    """``display.worker_rotation`` from the full config (default False)."""
    try:
        from hermes_cli.config import load_config_readonly

        display = load_config_readonly().get("display") or {}
        value = display.get("worker_rotation", False)
    except Exception:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


def _delegation_flag(key: str) -> Optional[bool]:
    """Read one boolean from the delegation config section, or None if unset."""
    try:
        from tools.delegate_tool import _load_config

        value = _load_config().get(key)
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip().lower()
        if not stripped:
            return None
        return stripped in {"true", "1", "yes", "on"}
    return bool(value)


def route_to_dispatcher_enabled() -> bool:
    """``delegation.route_to_dispatcher``; strictly opt-in (default False).

    When on, delegations from board-bound sessions become ready cards executed
    by isolated dispatcher workers instead of in-process subagents. Inheriting
    display.worker_rotation proved to be a bad implicit coupling: it silently
    changed delegation semantics (results no longer return to the principal's
    turn) on every rotation-enabled profile — TARGET_ARCHITECTURE gap 9.
    """
    explicit = _delegation_flag("route_to_dispatcher")
    return bool(explicit) if explicit is not None else False


def mirror_principal_turns_enabled() -> bool:
    """``delegation.mirror_principal_turns``; unset inherits display.worker_rotation."""
    explicit = _delegation_flag("mirror_principal_turns")
    if explicit is not None:
        return explicit
    return _display_worker_rotation()


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
        if not board:
            # Diagnostic probe (28/08, DOVTest 23:50): ONE observed case of a
            # checkpoint-restored turn delegating with no board env while
            # its topic was board-bound — cards, subs, rotation and traces
            # all silently skipped. Not reproduced on the common resume
            # paths; this warning names the next occurrence instead of
            # letting it pass as "no board".
            chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
            if chat_id:
                logger.warning(
                    "delegation kanban: no HERMES_PROJECT_BOARD in a gateway "
                    "session (chat_id=%s thread=%s) — if this topic is "
                    "board-bound, the turn lost its project env (suspected "
                    "checkpoint-restore path); cards/rotation will be "
                    "skipped for this fan-out",
                    chat_id,
                    (get_session_env("HERMES_SESSION_THREAD_ID", "") or ""),
                )
        return board or None
    except Exception as exc:  # noqa: BLE001 - never break delegation
        logger.debug("delegation kanban: board resolution failed: %s", exc)
        return None


def _origin_subscription_context() -> Dict[str, str]:
    """Origin chat/scope of the delegating turn, from the session env.

    TARGET_ARCHITECTURE acceptance finding (28/08, DOVTest): mirror cards
    used to carry NO notify subscription and NO project/session scope, so
    the notifier never produced rows for them — worker rotation ("now
    watching"), the focus bubble and the per-worker closeout traces were
    all structurally blind to in-process fan-outs. Empty dict when there is
    no chat origin (CLI runs): cards then behave exactly as before.
    """
    try:
        from gateway.session_context import get_session_env

        platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip()
        chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
        if not platform or not chat_id:
            return {}
        return {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": (get_session_env("HERMES_SESSION_THREAD_ID", "") or "").strip(),
            "user_id": (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip(),
            "chat_type": (get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "").strip(),
            "profile": (get_session_env("HERMES_SESSION_PROFILE", "") or "").strip(),
            "project_id": (get_session_env("HERMES_PROJECT_ID", "") or "").strip(),
            "session_id": (get_session_env("HERMES_SESSION_ID", "") or "").strip(),
        }
    except Exception:  # noqa: BLE001 - never break delegation
        logger.debug("delegation kanban: origin context failed", exc_info=True)
        return {}


def create_delegation_cards(
    task_list: List[Dict[str, Any]],
    delegation_id: Optional[str],
    board: Optional[str],
    live_paths: Optional[List[str]] = None,
) -> Dict[int, str]:
    """Create one claimed running mirror card per delegated task.

    Cards inherit the delegating turn's project/session scope and a notify
    subscription to the originating chat/topic, so the notifier feeds them
    into worker rotation and per-worker closeout traces (see
    ``_origin_subscription_context``).

    Returns ``{task_index: task_id}`` for every card that was created; on
    any failure the affected index is simply absent.
    """
    if not board or not task_list:
        return {}
    cards: Dict[int, str] = {}
    origin = _origin_subscription_context()
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
                        project_id=origin.get("project_id") or None,
                        session_id=origin.get("session_id") or None,
                        idempotency_key=(
                            f"{delegation_id}:{index}" if delegation_id else None
                        ),
                    )
                    if origin:
                        try:
                            kb.add_notify_sub(
                                conn,
                                task_id=task_id,
                                platform=origin["platform"],
                                chat_id=origin["chat_id"],
                                thread_id=origin.get("thread_id") or None,
                                user_id=origin.get("user_id") or None,
                                chat_type=origin.get("chat_type") or None,
                                notifier_profile=origin.get("profile") or None,
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "delegation kanban: notify sub failed (%s)",
                                task_id,
                                exc_info=True,
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


def create_dispatch_cards(
    task_list: List[Dict[str, Any]],
    board: Optional[str],
    delegation_id: Optional[str] = None,
    brief: Optional[str] = None,
) -> Dict[int, str]:
    """Create one READY (unclaimed) card per task for dispatcher pickup.

    The route-to-dispatcher path: no in-process subagent runs, so the card
    must stay claimable — a real dispatcher worker (isolated process, its own
    heartbeat, board log transcript) executes the goal. Same best-effort
    contract as ``create_delegation_cards``.

    G3 (spec T3, 29/08): dispatch cards were the ONE creation path with no
    origin stamp — the worker started blank about the conversation that
    produced its card. Cards now carry the origin ``session_id``/
    ``project_id`` (same ContextVar-backed source the mirror path uses;
    never ``os.environ``, which cross-talks between concurrent turns) and,
    when provided, a sanitized ``brief`` of the live discussion appended
    AFTER the task's explicit context — the explicit context always wins
    the budget; the brief only uses what remains of ``_BODY_MAX``.
    """
    if not board or not task_list:
        return {}
    cards: Dict[int, str] = {}
    try:
        from hermes_cli import kanban_db as kb

        author = _author()
        origin = _origin_subscription_context()
        origin_session = str(origin.get("session_id") or "").strip() or None
        origin_project = str(origin.get("project_id") or "").strip() or None
        with kb.connect_closing(board=board) as conn:
            for index, task in enumerate(task_list):
                try:
                    goal = str(task.get("goal") or "").strip()
                    if not goal:
                        goal = f"delegated task {index}"
                    context = str(task.get("context") or "").strip()
                    body = goal if not context else f"{goal}\n\n{context}"
                    if brief:
                        remaining = _BODY_MAX - len(body) - 2
                        if remaining > 200:
                            body = f"{body}\n\n{brief[:remaining]}"
                    task_id = kb.create_task(
                        conn,
                        title=goal[:_TITLE_MAX],
                        body=body[:_BODY_MAX],
                        created_by=author,
                        board=board,
                        session_id=origin_session,
                        project_id=origin_project,
                        idempotency_key=(
                            f"{delegation_id}:{index}" if delegation_id else None
                        ),
                    )
                    kb.add_comment(
                        conn,
                        task_id,
                        author,
                        f"Routed to dispatcher by {author} "
                        f"(delegation.route_to_dispatcher): an isolated "
                        f"worker will pick this card up from ready.",
                    )
                    cards[index] = task_id
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "delegation kanban: dispatch card create failed "
                        "(task %s): %s",
                        index,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegation kanban: dispatch batch create failed: %s", exc)
    return cards


def archive_stale_delegation_cards(
    board: str,
    card_ids: List[str],
    delegation_id: str,
) -> List[str]:
    """Archive mirror cards left behind by a fan-out whose process died.

    Before this existed, the dead attempt's cards sat ``running`` with a
    stale claim for the full claim TTL (~29h), occupying max_in_progress
    slots (phantom saturation: 28/08 concursa-ai, 4 dead cards starving the
    queue) and triple-counting the same work once the principal re-delegated.
    Recovery calls this for every card of the dead attempt: release the
    claim, archive, and leave an audit trail. Best-effort per card, same
    contract as the rest of this module. Returns the ids actually archived.
    """
    archived: List[str] = []
    if not board or not card_ids:
        return archived
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing(board=board) as conn:
            for task_id in card_ids:
                try:
                    row = conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if row is None or row["status"] not in (
                        "running", "ready", "review",
                    ):
                        continue
                    with kb.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET status='archived', "
                            "claim_lock=NULL, claim_expires=NULL "
                            "WHERE id = ?",
                            (task_id,),
                        )
                        conn.execute(
                            "INSERT INTO task_events"
                            "(task_id, kind, payload, created_at) "
                            "VALUES (?, 'delegation_stale', ?, "
                            "strftime('%s','now'))",
                            (task_id,
                             json.dumps({"delegation_id": delegation_id})),
                        )
                    kb.add_comment(
                        conn, task_id, _author(),
                        "Tentativa interrompida: o processo que hospedava o "
                        f"subagente ({delegation_id}) morreu antes de "
                        "concluir. Card arquivado automaticamente pelo "
                        "recovery; a retomada acontece em um novo fan-out "
                        "com cards próprios.",
                    )
                    archived.append(task_id)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "delegation kanban: stale archive failed (%s)",
                        task_id, exc_info=True,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegation kanban: stale archive batch failed: %s", exc)
    return archived


class MirrorHeartbeat:
    """Daemon thread beating ``last_heartbeat_at`` on live mirror cards.

    Mirror cards used to sit claimed with no heartbeat, so read-only views
    (Vigília) rendered every in-process delegated child as dead while it
    worked. Beats every ``interval`` seconds until ``stop()``; a card whose
    beat fails ``_MAX_CARD_FAILURES`` times in a row (closed, reclaimed,
    board gone) is dropped from the rotation so the thread winds down alone.
    """

    _MAX_CARD_FAILURES = 3

    def __init__(self, board: str, task_ids: List[str], interval: float) -> None:
        self._board = board
        self._interval = max(1.0, float(interval))
        self._failures: Dict[str, int] = {task_id: 0 for task_id in task_ids}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="delegation-mirror-heartbeat", daemon=True
        )

    def start(self) -> "MirrorHeartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                from hermes_cli import kanban_db as kb

                with kb.connect_closing(board=self._board) as conn:
                    for task_id in list(self._failures):
                        try:
                            ok = kb.heartbeat_worker(conn, task_id)
                        except Exception:  # noqa: BLE001
                            ok = False
                        if ok:
                            self._failures[task_id] = 0
                        else:
                            self._failures[task_id] += 1
                            if self._failures[task_id] >= self._MAX_CARD_FAILURES:
                                del self._failures[task_id]
            except Exception as exc:  # noqa: BLE001
                logger.debug("delegation kanban: heartbeat pass failed: %s", exc)
            if not self._failures:
                return


def start_mirror_heartbeat(
    board: Optional[str],
    cards: Dict[int, str],
    interval: float = 45.0,
) -> Optional[MirrorHeartbeat]:
    """Start the mirror heartbeat thread, or None when there is nothing to beat."""
    if not board or not cards:
        return None
    try:
        return MirrorHeartbeat(board, list(cards.values()), interval).start()
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegation kanban: heartbeat start failed: %s", exc)
        return None


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
                        final = (
                            summary[:_SUMMARY_MAX]
                            or "delegated subagent finished"
                        )
                        # A status-line summary ("Aguardando...") published
                        # verbatim makes the DONE mirror lie the same way
                        # the principal mirror did (28/08 Central_DEC) —
                        # same honest-note treatment, same escape, and no
                        # unverified claims (reviewer round 2).
                        if closeout_rewrite_enabled() and (
                            looks_like_status_not_closeout(final)
                        ):
                            snippet = " ".join(final.split())[:160]
                            final = (
                                "Subagente delegado encerrou SEM closeout "
                                "de entrega: a última mensagem era um "
                                f"status — “{snippet}”."
                            )
                        kb.complete_task(conn, task_id, result=final)
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
