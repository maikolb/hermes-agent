"""A worker launched by another process leaves its quota exit as a receipt.

The Balcao start-approved spawns the worker from a process that exits right
after, so the worker is not a child of the dispatcher: its exit code 75 never
enters the waitpid registry and the 402 of the Concursa board (runs 1716 to
1730, 16/10) was classified as a crash, or reaped as `reclaimed`. The worker
now writes a run-scoped `worker_quota_exit` event before it leaves, and the
tick reads it the way it reads the goal yield receipt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("kanban_executor_catalog")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _launched(conn, worker_pid=70001):
    """A running card whose claim belongs to a dead launcher and whose worker is not our child."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="balcao", assignee="a")
    claimed = kb.claim_task(conn, tid, claimer=f"{host}:1")
    assert claimed is not None
    kb._set_worker_pid(conn, tid, worker_pid)
    return tid, claimed.current_run_id


def test_quota_exit_receipt_makes_a_foreign_worker_rate_limited(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id = _launched(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        assert kb.record_worker_quota_exit() is True
        crashed = kb.detect_crashed_workers(conn)

        assert tid not in crashed
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()["outcome"]
        assert outcome == "rate_limited"


def test_foreign_worker_without_the_receipt_is_still_a_crash(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _launched(conn)

        crashed = kb.detect_crashed_workers(conn)

        assert tid in crashed
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()["outcome"]
        assert outcome == "crashed"


def test_receipt_of_an_older_run_does_not_excuse_the_next_exit(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, first_run = _launched(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(first_run))
        assert kb.record_worker_quota_exit() is True
        kb.detect_crashed_workers(conn)

        claimed = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:1")
        assert claimed is not None and claimed.current_run_id != first_run
        kb._set_worker_pid(conn, tid, 70002)

        crashed = kb.detect_crashed_workers(conn)

        assert tid in crashed


def test_receipt_is_refused_for_a_run_that_is_not_current(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id = _launched(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 1))

        assert kb.record_worker_quota_exit() is False
        assert conn.execute("SELECT 1 FROM task_events WHERE kind='worker_quota_exit'").fetchone() is None
