"""Kanban mirror card for long principal turns.

Delegated children get mirror cards (``delegation_kanban``) and dispatcher
workers own real cards, but the principal's own inline work on a
board-bound topic was invisible: the Telegram display showed a turn hard at
work while the board showed nothing running, so read-only views (Vigília)
and the chat told different stories (27/08 DOVCRM resume incident).

One mirror card per qualifying turn: created once the turn proves
non-trivial (``_START_THRESHOLD_SECONDS`` of wall clock, i.e. from the
second activity-indicator wake onward), heartbeat on every subsequent wake,
completed when the turn tears down. A gateway crash leaves the card claimed
with the delegation TTL, the same orphan semantics as delegated mirrors.

Behaviour contract mirrors ``delegation_kanban``: strictly best-effort.
Every entry point catches everything; a mirror failure must never break the
turn. All SQLite writes run off the event loop (``asyncio.to_thread`` /
daemon thread) so the mirror never contributes to the gateway congestion it
exists to make visible.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from tools.closeout_guard import (
    closeout_rewrite_enabled,
    looks_like_status_not_closeout,
)
from tools.delegation_kanban import (
    _BODY_MAX,
    _CLAIM_TTL_SECONDS,
    _SUMMARY_MAX,
    _TITLE_MAX,
    _author,
    mirror_principal_turns_enabled,
    resolve_delegation_board,
)

logger = logging.getLogger(__name__)

_START_THRESHOLD_SECONDS = 60.0


def _last_assistant_message(session_id: Optional[str]) -> str:
    """Best-effort: the turn's final assistant message from the profile state.

    The mirror closes in turn teardown, after the final response was
    persisted; that text is the principal's own AOF closeout. Any failure
    (no session id, schema drift, empty turn) returns "" and the caller
    falls back to the bare duration line.
    """
    if not session_id:
        return ""
    try:
        import sqlite3

        from hermes_constants import get_hermes_home

        conn = sqlite3.connect(str(get_hermes_home() / "state.db"), timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? "
                "AND role = 'assistant' AND content IS NOT NULL "
                "AND length(content) > 0 "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return str(row["content"]).strip() if row else ""
    except Exception:  # noqa: BLE001
        return ""


def _compose_final_result(summary: str, minutes: int) -> str:
    """The mirror card's DONE record for this turn.

    The turn's own final message is the principal's AOF closeout — unless
    it is a status line ("**Em execução.**", 28/08 Central_DEC): publishing
    that as a done result makes the board lie and reads as a stall. The
    replacement states only what this mirror KNOWS: the turn ended without
    a delivery closeout; it does not claim continuation cards exist
    (reviewer round 2: the earlier wording asserted unverified cards).
    """
    if (
        summary
        and closeout_rewrite_enabled()
        and looks_like_status_not_closeout(summary)
    ):
        snippet = " ".join(summary.split())[:160]
        return (
            f"Turno principal encerrou (~{minutes} min) SEM closeout de "
            f"entrega: a última mensagem era um status — “{snippet}”. "
            f"Continuação não verificada por este mirror."
        )
    if summary:
        return (
            f"{summary}\n\n(Turno principal concluído em ~{minutes} min.)"
        )
    return f"Turno principal concluído em ~{minutes} min."


def _clean_title_hint(raw: str) -> str:
    """Distill a card title from the raw turn prompt.

    Raw prompts arrive wrapped in metadata the board must not display —
    sender tags (``[Jhonatan|7550030839]``), system notes, image markers,
    forwarded-message timestamps. 28/08 concursa-ai: mirror cards titled
    with the full bracket soup reached the closeout trace in the topic
    verbatim. Keep the first meaningful line, drop leading bracket blocks,
    and cut at the first inline bracket.
    """
    text = (raw or "").strip()
    while text.startswith("["):
        end = text.find("]")
        if end < 0:
            return ""
        text = text[end + 1:].lstrip(" |\n\t-:")
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    cut = text.find("[")
    if cut > 0:
        text = text[:cut].rstrip(" |-:")
    return text[:90].strip()


class PrincipalTurnMirror:
    """Mirror card lifecycle for one principal turn (create/beat/finish).

    ``idempotency_key`` (derived from the platform message id, which is
    stable across a crash and its native auto-resume) makes the resumed
    turn land on the SAME mirror card the interrupted turn left claimed:
    the orphan is reclaimed and re-claimed by this process instead of a
    second card appearing — the resumed work keeps its traceable thread
    (27/08: the resume did audit work with no card at all). Resuming N
    times converges on the same card, so there is no card-creation loop.
    """

    def __init__(
        self,
        board: str,
        title_hint: str,
        idempotency_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self._board = board
        self._title_hint = (title_hint or "").strip()
        self._idempotency_key = (idempotency_key or "").strip() or None
        self._session_id = (session_id or "").strip() or None
        self._task_id: Optional[str] = None
        self._run_id: Optional[int] = None
        self._started = False
        self._finished = False
        self.resumed = False

    async def tick(self, elapsed_seconds: float) -> None:
        """Called from each activity-indicator wake while the turn runs."""
        if self._finished:
            return
        try:
            if not self._started:
                if elapsed_seconds < _START_THRESHOLD_SECONDS:
                    return
                self._started = True
                await asyncio.to_thread(self._start_sync)
            elif self._task_id:
                await asyncio.to_thread(self._beat_sync)
        except Exception as exc:  # noqa: BLE001 - never break the turn
            logger.debug("principal mirror: tick failed: %s", exc)

    def finish(self, elapsed_seconds: float) -> None:
        """Complete the mirror card without blocking turn teardown."""
        if self._finished:
            return
        self._finished = True
        if not self._task_id:
            return
        task_id = self._task_id
        board = self._board
        session_id = self._session_id
        minutes = max(1, int(elapsed_seconds // 60))

        def _close() -> None:
            try:
                from hermes_cli import kanban_db as kb

                # Spec (TARGET_ARCHITECTURE, 27/08): "Fim do trabalho
                # principal: closeout AOF com o trabalho feito." The turn's
                # own final message IS that closeout — carry it as the card
                # result so the completion trace publishes substance, not a
                # bare duration line.
                result = _compose_final_result(
                    _last_assistant_message(session_id), minutes
                )
                with kb.connect_closing(board=board) as conn:
                    kb.complete_task(
                        conn, task_id, result=result[:_SUMMARY_MAX],
                        expected_run_id=self._run_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("principal mirror: close failed: %s", exc)

        try:
            threading.Thread(
                target=_close, name="principal-mirror-close", daemon=True
            ).start()
        except Exception as exc:  # noqa: BLE001
            logger.debug("principal mirror: close spawn failed: %s", exc)

    def _start_sync(self) -> None:
        try:
            from hermes_cli import kanban_db as kb

            author = _author()
            hint = _clean_title_hint(self._title_hint) or time.strftime(
                "%H:%M"
            )
            title = f"Principal: {hint}"[:_TITLE_MAX]
            body = (
                "Mirror card for the principal's own inline turn on this "
                "board-bound topic. Created automatically once the turn "
                "crossed the non-trivial threshold "
                f"({int(_START_THRESHOLD_SECONDS)}s)."
            )
            if self._title_hint:
                body += f"\n\nTurn prompt (truncated):\n{self._title_hint}"
            with kb.connect_closing(board=self._board) as conn:
                from hermes_cli.profiles import get_active_profile_name

                with kb.write_txn(conn):
                    task_id = kb.create_task(
                        conn,
                        title=title,
                        task_role="activity",
                        requires_repo=False,
                        body=body[:_BODY_MAX],
                        created_by=author,
                        assignee=get_active_profile_name(),
                        board=self._board,
                        idempotency_key=self._idempotency_key,
                    )
                    existing = kb.get_task(conn, task_id)
                    claimed = None
                    if existing is not None and existing.status == "ready":
                        claimed = kb.claim_task(
                            conn, task_id, ttl_seconds=_CLAIM_TTL_SECONDS, allow_activity=True
                        )
                if (
                    existing is not None
                    and existing.status == "running"
                    and existing.claim_lock
                ):
                    # Same idempotency key, already running: the interrupted
                    # turn's orphan. Reclaim it from the dead process and
                    # re-claim it here so the resumed turn continues the SAME
                    # traceable card.
                    # Never steal another live turn's ownership.
                    if not kb.recover_interrupted_task(
                        conn, task_id, expected_run_id=existing.current_run_id,
                        expected_claim=existing.claim_lock,
                        expected_heartbeat=existing.last_heartbeat_at,
                    ):
                        return
                    self.resumed = True
                if claimed is None:
                    claimed = kb.claim_task(
                        conn, task_id, ttl_seconds=_CLAIM_TTL_SECONDS, allow_activity=True
                    )
                if claimed is None:
                    logger.debug(
                        "principal mirror: card %s not claimable", task_id
                    )
                kb.add_comment(
                    conn,
                    task_id,
                    author,
                    (
                        f"Principal turn RESUMED by {author} after an "
                        "interruption; continuing on the same mirror card."
                        if self.resumed
                        else f"Principal inline turn mirror, spawned by "
                        f"{author} (delegation.mirror_principal_turns)."
                    ),
                )
            if claimed is not None:
                self._task_id = task_id
                self._run_id = claimed.current_run_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("principal mirror: start failed: %s", exc)

    def _beat_sync(self) -> None:
        try:
            from hermes_cli import kanban_db as kb

            with kb.connect_closing(board=self._board) as conn:
                kb.heartbeat_worker(conn, self._task_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("principal mirror: beat failed: %s", exc)


def create_principal_turn_mirror(
    title_hint: str,
    idempotency_key: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[PrincipalTurnMirror]:
    """Mirror for the current session's turn, or None when not applicable."""
    try:
        if not mirror_principal_turns_enabled():
            return None
        board = resolve_delegation_board()
        if not board:
            return None
        return PrincipalTurnMirror(
            board,
            (title_hint or "")[:200],
            idempotency_key=idempotency_key,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("principal mirror: create failed: %s", exc)
        return None
