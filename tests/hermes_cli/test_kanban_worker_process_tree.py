"""WORKER_TREE_20261010: a worker that ends leaves no process of its own behind.

Real case (board dovcrm, run 938 of t_595598f8, 07/10/2026): the worker started
``node src/server.js`` in the background, closed its own run when the iteration
budget ran out and exited. The server sat in a session of its own, so nothing
reached it: init adopted it and it ran for three more days.

A worker's children are known only while the worker lives. The engine registers
them, by PID and creation time, on every tick, when the run closes and right
before it signals the worker; after the worker is gone it ends what is
registered. A process that only shares a PID with a registered one was created
at another time and is never signalled.
"""
import json
import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb

# The children here end up adopted by init, outside the test's own process tree.
pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]

WORKER = """
import json, os, subprocess, sys, time
import psutil
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)
receipt = {'pid': child.pid, 'started_at': psutil.Process(child.pid).create_time(),
           'sid': os.getsid(child.pid), 'pgid': os.getpgid(child.pid)}
with open(sys.argv[1] + '.tmp', 'w') as handle:
    json.dump(receipt, handle)
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
time.sleep(300)
"""


def _alive(identity):
    try:
        process = psutil.Process(identity["pid"])
        return process.status() != psutil.STATUS_ZOMBIE and abs(process.create_time() - identity["started_at"]) < 0.01
    except psutil.NoSuchProcess:
        return False


def _gone(identity, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(identity):
            return True
        time.sleep(0.05)
    return False


def _end(identity):
    if _alive(identity):
        os.kill(identity["pid"], signal.SIGKILL)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)  # no real profile on the test machine
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def worker(tmp_path):
    """A worker, in a session of its own as the dispatcher starts it, and the child it started in another session."""
    receipt = tmp_path / "child.json"
    process = subprocess.Popen([sys.executable, "-c", WORKER, str(receipt)], start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = json.loads(receipt.read_text())
        # Session and group of its own: a signal to the worker, or to the worker's group, never reaches it.
        assert child["sid"] == child["pgid"] == child["pid"] != os.getsid(process.pid)
        yield process, child
    finally:
        if child:
            _end(child)
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.fixture
def bystander():
    """A live process the engine never started: what a reused PID points at."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def _running_card(conn, pid, *, max_runtime_seconds=None):
    """A card claimed two hours ago whose worker is the process ``pid``."""
    task_id = kb.create_task(conn, title="long job", assignee="worker", max_runtime_seconds=max_runtime_seconds)
    assert kb.claim_task(conn, task_id) is not None
    kb._set_worker_pid(conn, task_id, pid)
    old = int(time.time()) - 7200
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at=?, claim_expires=?, last_heartbeat_at=NULL WHERE id=?", (old, old, task_id))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (old, task_id))
    return task_id


def _tick(conn):
    return kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)


def _events(conn, task_id, kind):
    return [json.loads(row["payload"] or "{}") for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def _stale_heartbeat(conn, task_id, monkeypatch):
    monkeypatch.setattr(kb, "DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS", 1)
    monkeypatch.setattr(kb, "_STALE_HEARTBEAT_GAP_SECONDS", 1)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (int(time.time()) - 7200, task_id))


ENGINE_ENDS_THE_WORKER = {
    "runtime limit": lambda conn, task_id: kb.enforce_max_runtime(conn),
    "stale running": lambda conn, task_id: kb.detect_stale_running(conn, stale_timeout_seconds=1),
    "expired claim": lambda conn, task_id: kb.release_stale_claims(conn),
    "operator reclaim": lambda conn, task_id: kb.reclaim_task(conn, task_id),
}


@pytest.mark.parametrize("path", sorted(ENGINE_ENDS_THE_WORKER))
def test_engine_ends_the_worker_together_with_the_child_it_left_in_another_session(conn, worker, monkeypatch, path):
    process, child = worker
    task_id = _running_card(conn, process.pid, max_runtime_seconds=1)
    _stale_heartbeat(conn, task_id, monkeypatch)

    ENGINE_ENDS_THE_WORKER[path](conn, task_id)

    assert process.wait(timeout=10) is not None, "the worker itself was not ended"
    assert _gone(child), "the worker is gone and the process it started is still running, adopted by init"
    assert kb.get_task(conn, task_id).status != "running"


def test_worker_that_closes_its_own_run_and_exits_has_its_child_ended_by_the_next_tick(conn, worker):
    """Run 938: nobody signalled the worker. It reported the exhausted budget, closed the run and left."""
    process, child = worker
    task_id = _running_card(conn, process.pid)
    kb._record_task_failure(
        conn, task_id, "Iteration budget exhausted (400/400)",
        outcome="timed_out", release_claim=True, end_run=True,
    )
    process.terminate()
    process.wait(timeout=10)
    assert _alive(child), "the premise: the child outlives the worker that started it"

    _tick(conn)

    assert _gone(child), "the run is closed, its worker is gone and the process it started is still running"


def test_worker_that_dies_with_its_run_open_has_its_child_ended_by_the_tick_that_notices(conn, worker):
    """The commonest ending: the worker leaves by itself (a question for the Principal, a crash) and only a
    later tick closes the run. By then nobody is the parent of what it started."""
    process, child = worker
    task_id = _running_card(conn, process.pid)
    _tick(conn)
    assert process.poll() is None and _alive(child), "a tick never touches a running worker or its processes"

    process.kill()
    process.wait(timeout=10)
    _tick(conn)

    assert kb.get_task(conn, task_id).status != "running"
    assert _gone(child), "the run was closed for a dead worker and the process it started is still running"


def _listed(conn, task_id):
    return [row["pid"] for row in conn.execute(
        "SELECT pid FROM task_run_processes WHERE task_id=? ORDER BY pid", (task_id,))]


def _closed_run_of_a_worker_that_left(conn, process):
    """The card's run, closed by its own worker, which then exited (as in run 938)."""
    task_id = _running_card(conn, process.pid)
    run_id = kb.get_task(conn, task_id).current_run_id
    worker = (process.pid, kb._process_start_time(process.pid))
    kb._record_task_failure(
        conn, task_id, "Iteration budget exhausted (400/400)",
        outcome="timed_out", release_claim=True, end_run=True,
    )
    process.terminate()
    process.wait(timeout=10)
    return task_id, run_id, worker


def test_process_that_only_shares_the_pid_of_a_listed_one_is_never_signalled(conn, worker, bystander):
    process, child = worker
    task_id, run_id, (worker_pid, worker_started_at) = _closed_run_of_a_worker_that_left(conn, process)
    assert _listed(conn, task_id) == [child["pid"]]
    # The run also lists a process that ended long ago; its PID now belongs to somebody else's process.
    reused = {"pid": bystander.pid, "started_at": kb._process_start_time(bystander.pid) - 300}
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_run_processes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, reused["pid"], reused["started_at"], worker_pid, worker_started_at, int(time.time())))
    signals = []

    def deliver(pid, sig):
        signals.append((pid, sig))
        os.kill(pid, sig)

    assert kb.end_closed_run_processes(conn, signal_fn=deliver) == [run_id]

    assert _gone(child)
    assert {pid for pid, _ in signals} == {child["pid"]}, "a process that only shares the PID was signalled"
    assert bystander.poll() is None
    assert _listed(conn, task_id) == []
    [event] = _events(conn, task_id, "worker_processes_ended")
    assert [p["pid"] for p in event["processes"]] == [child["pid"]] and event["still_alive"] == []
    assert (event["worker_pid"], event["worker_started_at"]) == (worker_pid, worker_started_at)


def test_identity_is_checked_before_every_signal():
    """The signal primitive itself: no signal for a PID whose process was created at another time."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        real = {"pid": sleeper.pid, "started_at": kb._process_start_time(sleeper.pid)}
        signals = []
        other_time = dict(real, started_at=real["started_at"] - 300)
        assert kb._end_processes([other_time], lambda pid, sig: signals.append((pid, sig)), grace=0) == []
        assert signals == []
        # A hook that delivers nothing: the process is asked to end, then killed, and reported as still alive.
        assert kb._end_processes([real], lambda pid, sig: signals.append((pid, sig)), grace=0) == [real]
        assert signals == [(sleeper.pid, signal.SIGTERM), (sleeper.pid, signal.SIGKILL)]
        own = {"pid": os.getpid(), "started_at": kb._process_start_time(os.getpid())}
        assert kb._end_processes([own], lambda pid, sig: signals.append((pid, sig)), grace=0) == []
        assert len(signals) == 2, "the process running the reconciliation is never a target"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_listed_process_is_spared_while_a_running_card_owns_it(conn, worker):
    """An older run may list a process that today belongs to the worker of a running card."""
    process, child = worker
    left = subprocess.Popen([sys.executable, "-c", "pass"])
    gone_worker = (left.pid, kb._process_start_time(left.pid))
    older = _running_card(conn, left.pid)
    left.wait(timeout=10)
    older_run = kb.get_task(conn, older).current_run_id
    assert kb.reclaim_task(conn, older)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_run_processes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (older_run, older, child["pid"], child["started_at"], gone_worker[0], gone_worker[1], int(time.time())))
    current = _running_card(conn, process.pid)

    _tick(conn)

    assert process.poll() is None and _alive(child), "a process of a running card was ended on an older run's word"
    assert _listed(conn, older) == [] and _events(conn, older, "worker_processes_ended") == []
    assert _listed(conn, current) == [child["pid"]]


def test_processes_stay_while_the_worker_that_closed_its_run_is_still_leaving(conn):
    """The worker that closes its own run lists its processes itself; they are ended only after it is gone."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        identity = {"pid": child.pid, "started_at": kb._process_start_time(child.pid)}
        task_id = _running_card(conn, os.getpid())  # this process is the worker
        kb._record_task_failure(
            conn, task_id, "Iteration budget exhausted (400/400)",
            outcome="timed_out", release_claim=True, end_run=True,
        )
        assert identity["pid"] in _listed(conn, task_id)

        assert kb.end_closed_run_processes(conn) == []

        assert _alive(identity) and identity["pid"] in _listed(conn, task_id)
    finally:
        child.kill()
        child.wait()


def test_open_native_command_of_the_run_is_left_to_its_own_reconciliation(conn, worker):
    """A native command has its own receipt and exit grace; the list waits until that receipt is closed."""
    from hermes_cli import nfos_tool

    process, child = worker
    task_id, run_id, _ = _closed_run_of_a_worker_that_left(conn, process)
    nfos_tool.init_schema(conn)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO nfos_tool_calls (id, task_id, run_id, argv_json, cwd, status, created_at, timeout_seconds) "
            "VALUES ('call_open', ?, ?, '[]', '.', 'running', ?, 60)", (task_id, run_id, time.time()))

    assert kb.end_closed_run_processes(conn) == []
    assert _alive(child) and _listed(conn, task_id) == [child["pid"]]

    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_tool_calls SET status='interrupted' WHERE id='call_open'")
    assert kb.end_closed_run_processes(conn) == [run_id]
    assert _gone(child)


def test_tick_keeps_the_list_of_a_running_worker_to_what_is_alive(conn, worker):
    process, child = worker
    task_id = _running_card(conn, process.pid)
    _tick(conn)
    assert _listed(conn, task_id) == [child["pid"]]

    _end(child)
    assert _gone(child)
    _tick(conn)

    assert _listed(conn, task_id) == [] and process.poll() is None
    assert _events(conn, task_id, "worker_processes_ended") == [], "nothing was ended by the engine"
