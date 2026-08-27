"""Tests for the delegation → kanban mirror-card side channel."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def kanban_env(tmp_path: Path, monkeypatch):
    """Pin the kanban DB to a fresh temp file (HERMES_KANBAN_DB override)."""
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    # Reset the module-level init cache so each test gets a fresh schema.
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS = set()
    return db


def _get_task(task_id: str):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def test_create_cards_are_running_and_claimed(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards(
        [
            {"goal": "Analisar visual do video", "context": "reels bug"},
            {"goal": "Auditar UI/UX"},
        ],
        "deleg_test01",
        "default",
        live_paths=["/tmp/task-0.log", "/tmp/task-1.log"],
    )

    assert sorted(cards) == [0, 1]
    for index, task_id in cards.items():
        task = _get_task(task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock, "mirror card must hold a claim"

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, cards[0])
    assert any("deleg_test01" in c.body for c in comments)
    assert any("task-0.log" in c.body for c in comments)


def test_create_cards_idempotent_per_delegation(kanban_env):
    from tools import delegation_kanban as dk

    first = dk.create_delegation_cards([{"goal": "G"}], "deleg_same", "default")
    second = dk.create_delegation_cards([{"goal": "G"}], "deleg_same", "default")
    assert first[0] == second[0]


def test_close_cards_completed_becomes_done_with_summary(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_ok", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        # "completed" is the real terminal status stamped by
        # _execute_and_aggregate (regression: the smoke on the dovcrm board
        # blocked both cards because the closer only accepted "ok").
        [{"task_index": 0, "status": "completed", "summary": "entreguei X e Y"}],
    )
    task = _get_task(cards[0])
    assert task.status == "done"


def test_close_cards_failure_blocks_for_human(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_bad", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        [{"task_index": 0, "status": "failed", "error": "boom", "summary": ""}],
    )
    task = _get_task(cards[0])
    assert task.status == "blocked"


def test_close_cards_interrupted_blocks_for_human(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_int", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        [{"task_index": 0, "status": "interrupted", "summary": "parcial"}],
    )
    task = _get_task(cards[0])
    assert task.status == "blocked"


def test_no_board_creates_nothing(kanban_env):
    from tools import delegation_kanban as dk

    assert dk.create_delegation_cards([{"goal": "G"}], "deleg_x", None) == {}
    # Closing with no cards is a no-op rather than an error.
    dk.close_delegation_cards(None, {}, [{"task_index": 0, "status": "completed"}])
