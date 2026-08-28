"""Backoff exponencial do re-check do respawn guard (análise 48h 28/08:
um card acumulou 1098 eventos respawn_guarded a ~90s por ~27h)."""

from __future__ import annotations

import time

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def _event(conn, task_id, kind, age_seconds):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, ?, '{}', ?)",
            (task_id, kind, int(time.time() - age_seconds)),
        )


def test_no_guard_history_checks_now(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    assert kb._respawn_guard_backoff_remaining(board, task_id) == 0.0


def test_single_recent_guard_backs_off_90s(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    _event(board, task_id, "respawn_guarded", 10)
    remaining = kb._respawn_guard_backoff_remaining(board, task_id)
    assert 70 <= remaining <= 90


def test_streak_grows_interval_exponentially(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    for age in (400, 300, 200, 100, 10):  # streak de 5, último há 10s
        _event(board, task_id, "respawn_guarded", age)
    remaining = kb._respawn_guard_backoff_remaining(board, task_id)
    # streak 5 → intervalo 90*2^4 = 1440s; restante ~1430s
    assert 1300 <= remaining <= 1440


def test_old_guard_allows_recheck(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    _event(board, task_id, "respawn_guarded", 5000)
    assert kb._respawn_guard_backoff_remaining(board, task_id) == 0.0


def test_non_guard_event_resets_streak(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    _event(board, task_id, "respawn_guarded", 300)
    _event(board, task_id, "claimed", 200)
    assert kb._respawn_guard_backoff_remaining(board, task_id) == 0.0


def test_heartbeats_do_not_break_the_streak(board):
    task_id = kb.create_task(board, title="w", assignee="h")
    _event(board, task_id, "respawn_guarded", 100)
    _event(board, task_id, "heartbeat", 50)
    _event(board, task_id, "respawn_guarded", 20)
    remaining = kb._respawn_guard_backoff_remaining(board, task_id)
    # streak 2 → intervalo 180s; restante ~160s
    assert 140 <= remaining <= 180
