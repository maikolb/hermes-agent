"""A worker launched by another process leaves its exit code as a receipt.

The Balcao start-approved spawns the worker from a process that exits right after, so the worker is not a child of the
dispatcher: its exit code never enters the waitpid registry. The 402 of the Concursa board (runs 1716 to 1730, 16/10) was
classified as a crash, or reaped as `reclaimed`; with only the quota door read, a clean exit after a refused completion (the
normal path of CLOSURE_RECOVERY) counted a failure on the unified counter, and the Concursa runs with `failure_limit: 2`.
The worker now writes a run-scoped `worker_exit` event with its exit code and pid, and the tick reads it for any exit.
"""

from __future__ import annotations

import os
import time
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


LAUNCHER_PID = 4242


def _launched(conn, monkeypatch, worker_pid=None, launcher_alive=False, tid=None):
    """A running card whose claim belongs to a launcher (dead unless `launcher_alive`) and whose worker (this test process) is not a child of the tick."""
    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: bool(launcher_alive) and pid == LAUNCHER_PID)
    tid = tid or kb.create_task(conn, title="balcao", assignee="a")
    claimed = kb.claim_task(conn, tid, claimer=f"{host}:{LAUNCHER_PID}")
    assert claimed is not None
    kb._set_worker_pid(conn, tid, worker_pid or os.getpid())
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return tid, claimed.current_run_id


def _relaunch(conn, tid, monkeypatch):
    return _launched(conn, monkeypatch, tid=tid)


def _outcome(conn, run_id):
    return conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()["outcome"]


def test_quota_exit_receipt_makes_a_foreign_worker_rate_limited(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)

        assert kb.record_worker_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE) is True
        crashed = kb.detect_crashed_workers(conn)

        assert tid not in crashed
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 0)
        assert _outcome(conn, run_id) == "rate_limited"


def test_clean_exit_after_a_refused_completion_counts_no_failure(kanban_home, monkeypatch):
    """Case A of the review: the worker's closing was refused and it left with rc 0, as CLOSURE_RECOVERY expects."""
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)
        conn.execute("UPDATE tasks SET worker_started_at=? WHERE id=?", (float(time.time() - 60), tid))
        kb._append_event(conn, tid, "nfos_completion_refused", {"pending": ["C1"]}, run_id=run_id)
        conn.commit()

        assert kb.record_worker_exit(0) is True
        crashed = kb.detect_crashed_workers(conn)

        assert tid not in crashed
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 0)
        assert _outcome(conn, run_id) == "refused_exit"


def test_clean_exit_without_closing_is_the_bounded_protocol_violation(kanban_home, monkeypatch):
    """rc 0 with no terminal call: the same protocol violation as a child of the dispatcher, on its own counter."""
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)

        assert kb.record_worker_exit(0) is True
        kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0, "the violation has its own bounded streak, not the unified counter"
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "protocol_violation" in kinds and "crashed" not in kinds


def test_non_zero_exit_is_a_crash_with_its_code(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)

        assert kb.record_worker_exit(2) is True
        crashed = kb.detect_crashed_workers(conn)

        assert tid in crashed
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='crashed'", (tid,)).fetchone()
        assert '"exit_code": 2' in event["payload"] or '"exit_code":2' in event["payload"]


def test_a_gateway_restart_interrupt_keeps_its_own_reading(kanban_home, monkeypatch):
    """A worker killed by the cgroup SIGINT of a gateway restart leaves 130: it must not turn into a counted crash."""
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)
        monkeypatch.setattr(kb, "_gateway_process_start", lambda: time.time())
        conn.execute("UPDATE tasks SET worker_started_at=? WHERE id=?", (float(time.time() - 3600), tid))
        conn.commit()

        assert kb.record_worker_exit(130) is True
        crashed = kb.detect_crashed_workers(conn)

        assert tid not in crashed
        assert kb.get_task(conn, tid).consecutive_failures == 0
        assert [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? AND kind='interrupted'", (tid,))] == ["interrupted"]


def test_worker_gone_without_a_receipt_after_its_launcher_is_requeued_without_a_failure(kanban_home, monkeypatch):
    """Review finding 5: only suporte.service restarted, killing the worker by signal (no receipt). As before the PR, twice in a row
    leaves the card ready with 0 failures; it must not reach the breaker (`failure_limit: 2` on the Concursa board)."""
    with kb.connect() as conn:
        for attempt in (1, 2):
            tid, run_id = _launched(conn, monkeypatch) if attempt == 1 else _relaunch(conn, tid, monkeypatch)

            crashed = kb.detect_crashed_workers(conn)

            assert tid not in crashed
            task = kb.get_task(conn, tid)
            assert (task.status, task.consecutive_failures) == ("ready", 0), f"attempt {attempt}"
            assert _outcome(conn, run_id) == "reclaimed"
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert kinds.count("worker_lost") == 2 and "crashed" not in kinds and "gave_up" not in kinds


def test_worker_gone_without_a_receipt_under_a_live_dispatcher_is_still_a_crash(kanban_home, monkeypatch):
    """The dispatcher's own child that vanished between the reap and the liveness check keeps the old reading."""
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch, launcher_alive=True)

        crashed = kb.detect_crashed_workers(conn)

        assert tid in crashed
        assert _outcome(conn, run_id) == "crashed"


def test_receipt_of_an_older_run_does_not_excuse_the_next_exit(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, first_run = _launched(conn, monkeypatch)
        assert kb.record_worker_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE) is True
        kb.detect_crashed_workers(conn)

        _, second_run = _launched(conn, monkeypatch, tid=tid, launcher_alive=True)
        assert second_run != first_run

        assert tid in kb.detect_crashed_workers(conn)


def test_receipt_of_another_process_does_not_stand_for_the_worker(kanban_home, monkeypatch):
    """A `hermes` the worker runs inside a command inherits the card env and leaves its own receipt; the worker's exit stays unknown."""
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch, worker_pid=os.getpid() + 7)

        assert kb.record_worker_exit(0) is False  # this process is not the card's worker
        kb._append_event(conn, tid, "worker_exit", {"exit_code": 0, "pid": os.getpid()}, run_id=run_id)
        conn.commit()

        assert tid not in kb.detect_crashed_workers(conn)
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "worker_lost" in kinds and "protocol_violation" not in kinds, "the foreign receipt is not read as this worker's clean exit"


def test_receipt_is_refused_for_a_run_that_is_not_current(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 1))

        assert kb.record_worker_exit(0) is False
        assert conn.execute("SELECT 1 FROM task_events WHERE kind='worker_exit'").fetchone() is None


@pytest.mark.parametrize("entry, code", [
    (lambda: (_ for _ in ()).throw(SystemExit(75)), 75),
    (lambda: (_ for _ in ()).throw(SystemExit()), 0),
    (lambda: (_ for _ in ()).throw(SystemExit("fatal message")), 1),
    (lambda: (_ for _ in ()).throw(KeyboardInterrupt()), 130),
    (lambda: (_ for _ in ()).throw(RuntimeError("boom")), 1),
    (lambda: None, 0),
    (lambda: 3, 3),
])
def test_entry_point_wrapper_leaves_the_exit_code_of_every_way_out(kanban_home, monkeypatch, entry, code):
    with kb.connect() as conn:
        tid, run_id = _launched(conn, monkeypatch)
        try:
            kb.run_with_exit_receipt(entry)
        except BaseException:
            pass
        receipt = kb._worker_exit_receipt(conn, tid, os.getpid())
    assert receipt == code


def test_entry_point_wrapper_does_nothing_outside_a_kanban_worker(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert kb.run_with_exit_receipt(lambda: "ran") == "ran"
