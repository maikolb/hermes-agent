"""Recovery archives the dead attempt's mirror cards (28/08 concursa-ai).

A gateway restart killed an in-process fan-out twice; each dead generation
kept its mirror cards claimed-running for the full claim TTL, occupying
max_in_progress slots (phantom saturation starved the ready queue) while
the re-delegated generation created fresh cards — the same work tripled on
the board. Now ``recover_abandoned_delegations`` archives the dead
attempt's cards, and the recovery message lists them and instructs exactly
one re-delegation with no manual card creation.
"""

from __future__ import annotations

import queue
import sqlite3
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_kanban as dk


@pytest.fixture()
def env(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: state)
    kdb = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kdb))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS = set()
    return state


def _kill_owner(monkeypatch, state):
    import gateway.status as gs

    monkeypatch.setattr(gs, "_pid_exists", lambda pid: False)
    conn = sqlite3.connect(state)
    try:
        conn.execute(
            "UPDATE async_delegations SET owner_pid = 999999999, "
            "owner_started_at = NULL"
        )
        conn.commit()
    finally:
        conn.close()


def test_foreground_recovery_archives_mirror_cards(env, monkeypatch):
    cards = dk.create_delegation_cards(
        [{"goal": "corrigir tutor"}, {"goal": "verificar edital"}],
        "deleg_dead", "default",
    )
    assert sorted(cards) == [0, 1]
    deleg_id = ad.register_foreground_delegation(
        goals=["corrigir tutor", "verificar edital"],
        session_key="agent:main:telegram:-1:1",
        delegation_id="deleg_dead",
        board="default",
    )
    ad.attach_delegation_cards(deleg_id, "default", cards)
    _kill_owner(monkeypatch, env)

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    evt = q.get_nowait()

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        for task_id in cards.values():
            task = kb.get_task(conn, task_id)
            assert task.status == "archived"
            assert not task.claim_lock
            stale = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? "
                "AND kind = 'delegation_stale'",
                (task_id,),
            ).fetchone()
            assert stale is not None
            assert task_id in evt["error"]
    assert "archived automatically" in evt["error"]
    assert "do NOT create kanban cards manually" in evt["error"]
    # The instruction the older recovery test pins must survive.
    assert "Re-delegate the goals" in evt["error"]


def test_recovery_leaves_finished_cards_alone(env, monkeypatch):
    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_done", "default")
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', claim_lock=NULL "
                "WHERE id = ?",
                (cards[0],),
            )
    deleg_id = ad.register_foreground_delegation(
        goals=["G"],
        session_key="agent:main:telegram:-1:1",
        delegation_id="deleg_done",
        board="default",
    )
    ad.attach_delegation_cards(deleg_id, "default", cards)
    _kill_owner(monkeypatch, env)

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    evt = q.get_nowait()

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, cards[0])
    assert task.status == "done", "finished card must not be re-archived"
    assert "archived automatically" not in evt["error"]


def test_async_batch_recovery_archives_and_notes(env, monkeypatch):
    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_async", "default")
    ad._persist_dispatch({
        "delegation_id": "deleg_async",
        "goal": "G",
        "goals": ["G"],
        "is_batch": True,
        "session_key": "agent:main:telegram:-1:1",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": None,
        "dispatched_at": time.time(),
    })
    ad.attach_delegation_cards("deleg_async", "default", cards)
    _kill_owner(monkeypatch, env)

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    evt = q.get_nowait()

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, cards[0])
    assert task.status == "archived"
    assert "outcome unknown" in evt["error"]
    assert "archived automatically" in evt["error"]
