"""WORKER_IDENTITY_20261010: a PID alone is never authority to signal a process.

A worker is the process with the recorded PID *and* the creation time persisted
when it was spawned. Reclaim, stale detection, the operator reclaim and the
runtime limit all terminate through one primitive that checks both before each
signal. A reused PID belongs to somebody else: it is not signalled and the
worker counts as gone. A live process that cannot be verified is not signalled
either, and the claim is held instead of handing the card to a second worker.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def bystander():
    """A live process that is not the worker: what a reused PID points at."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def _running_card(conn, pid, started_at):
    """A card whose worker ran past every limit; the recorded identity is (pid, started_at)."""
    task_id = kb.create_task(conn, title="long job", assignee="worker", max_runtime_seconds=1)
    assert kb.claim_task(conn, task_id) is not None
    kb._set_worker_pid(conn, task_id, pid)
    old = int(time.time()) - 7200
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at=?, started_at=?, claim_expires=?, last_heartbeat_at=NULL WHERE id=?",
                     (started_at, old, old, task_id))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (old, task_id))
    return task_id


PATHS = {
    "runtime limit": lambda conn, task_id, signal_fn: kb.enforce_max_runtime(conn, signal_fn=signal_fn),
    "stale running": lambda conn, task_id, signal_fn: kb.detect_stale_running(conn, stale_timeout_seconds=1, signal_fn=signal_fn),
    "expired claim": lambda conn, task_id, signal_fn: kb.release_stale_claims(conn, signal_fn=signal_fn),
    "operator reclaim": lambda conn, task_id, signal_fn: kb.reclaim_task(conn, task_id, signal_fn=signal_fn),
}


def _events(conn, task_id):
    return [event.kind for event in kb.list_events(conn, task_id)]


@pytest.mark.parametrize("path", sorted(PATHS))
def test_reused_pid_is_never_signalled_and_the_worker_counts_as_gone(conn, bystander, monkeypatch, path):
    signals = []
    real_start = kb._process_start_time(bystander.pid)
    assert real_start is not None
    # The worker recorded with this PID was created at another time: the PID was reused.
    task_id = _running_card(conn, bystander.pid, real_start - 300)
    # An expired claim is extended while the PID answers; make the heartbeat stale so the claim is reclaimed.
    monkeypatch.setattr(kb, "DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS", 1)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (int(time.time()) - 7200, task_id))
    if path == "stale running":
        monkeypatch.setattr(kb, "_STALE_HEARTBEAT_GAP_SECONDS", 1)

    PATHS[path](conn, task_id, lambda pid, sig: signals.append((pid, sig)))

    assert signals == [], "a process that only shares the PID was signalled"
    assert bystander.poll() is None
    task = kb.get_task(conn, task_id)
    assert task.status != "running" and task.worker_pid is None, "the worker is gone: the card is released"


@pytest.mark.parametrize("path", ["runtime limit", "stale running", "expired claim"])
def test_live_process_without_a_verifiable_identity_is_not_signalled_and_keeps_the_claim(conn, bystander, monkeypatch, path):
    signals = []
    task_id = _running_card(conn, bystander.pid, None)
    monkeypatch.setattr(kb, "DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS", 1)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (int(time.time()) - 7200, task_id))
    if path == "stale running":
        monkeypatch.setattr(kb, "_STALE_HEARTBEAT_GAP_SECONDS", 1)

    PATHS[path](conn, task_id, lambda pid, sig: signals.append((pid, sig)))

    assert signals == [], "a live process was signalled on its PID alone"
    task = kb.get_task(conn, task_id)
    assert task.status == "running" and task.worker_pid == bystander.pid, "no second worker beside a process that may be the worker"
    assert "reclaim_deferred" in _events(conn, task_id)


def test_the_worker_itself_is_still_terminated(conn, bystander):
    signals = []
    task_id = _running_card(conn, bystander.pid, kb._process_start_time(bystander.pid))

    def deliver(pid, sig):
        signals.append((pid, sig))
        os.kill(pid, sig)
        bystander.wait(timeout=10)

    assert kb.enforce_max_runtime(conn, signal_fn=deliver) == [task_id]
    assert signals == [(bystander.pid, signal.SIGTERM)]
    assert bystander.poll() is not None


def test_default_signal_never_targets_the_process_running_the_reconciliation(conn):
    own = kb._process_start_time(os.getpid())
    lock = kb._claimer_id()
    result = kb._terminate_reclaimed_worker(os.getpid(), lock, started_at=own)
    assert result["own_process"] is True and result["termination_attempted"] is False


def test_deferred_terminations_carry_the_worker_identity(conn):
    ref = kb._WorkerRef(4242, "host:1", 1700000000.5)
    assert ref == (4242, "host:1") and tuple(ref) == (4242, "host:1")
    pid, claim_lock = ref
    assert (pid, claim_lock, ref.started_at) == (4242, "host:1", 1700000000.5)
