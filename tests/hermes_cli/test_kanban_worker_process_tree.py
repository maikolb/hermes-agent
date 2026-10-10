"""WORKER_TREE_20261010: a worker that ends leaves no process of its own behind.

Real case (board dovcrm, run 938 of t_595598f8, 07/10/2026): the worker started
``node src/server.js`` in the background, closed its own run when the iteration
budget ran out and exited. The server sat in a session of its own, so nothing
reached it: init adopted it and it ran for three more days.

What a worker started is told apart in three ways, each with PID and creation
time: its descendants, read right before the engine signals it; its descendants
again, listed when its run closes; and the run its environment names, which
every process it started inherits and which is all that is left once the worker
went away by itself. After the worker is gone the engine ends them. A process
that only shares a PID with one of them was created at another time and is
never signalled.
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

SLEEP = [sys.executable, "-c", "import time; time.sleep(300)"]

# What the worker leaves running, by mode:
#   session   a child in a session of its own; it inherits the worker's environment.
#   scrubbed  the same child, started with an environment that names no run.
#   detached  a grandchild whose parent exits at once: init is its parent while the worker still runs.
WORKER = """
import json, os, subprocess, sys, time
import psutil
mode, receipt = sys.argv[1], sys.argv[2]
sleep = [sys.executable, '-c', 'import time; time.sleep(300)']
if mode == 'detached':
    launcher = subprocess.Popen([sys.executable, '-c',
        'import subprocess, sys; print(subprocess.Popen(' + repr(sleep) + ', start_new_session=True).pid, flush=True)'],
        stdout=subprocess.PIPE, text=True)
    pid = int(launcher.stdout.readline())
    launcher.wait()
else:
    env = None
    if mode == 'scrubbed':
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_KANBAN_')}
    pid = subprocess.Popen(sleep, start_new_session=True, env=env).pid
process = psutil.Process(pid)
found = {'pid': pid, 'started_at': process.create_time(), 'sid': os.getsid(pid), 'pgid': os.getpgid(pid),
         'ppid': process.ppid()}
with open(receipt + '.tmp', 'w') as handle:
    json.dump(found, handle)
os.replace(receipt + '.tmp', receipt)
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


def _identity(pid):
    return {"pid": pid, "started_at": kb._process_start_time(pid)}


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


def _claimed_card(conn, *, max_runtime_seconds=None):
    task_id = kb.create_task(conn, title="long job", assignee="worker", max_runtime_seconds=max_runtime_seconds)
    task = kb.claim_task(conn, task_id)
    assert task is not None
    return task_id, task.current_run_id


def _run_environment(task_id, run_id, *, db=None):
    """The environment the dispatcher gives a worker: its board, its card and its run."""
    return dict(os.environ, HERMES_KANBAN_DB=db or os.environ["HERMES_KANBAN_DB"],
                HERMES_KANBAN_TASK=task_id, HERMES_KANBAN_RUN_ID=str(run_id))


def _record_worker(conn, task_id, pid):
    """The card's worker is ``pid`` and has been running for two hours."""
    kb._set_worker_pid(conn, task_id, pid)
    old = int(time.time()) - 7200
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at=?, claim_expires=?, last_heartbeat_at=NULL WHERE id=?", (old, old, task_id))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (old, task_id))


@pytest.fixture
def start_worker(conn, tmp_path):
    """Start a worker as the dispatcher does (own session, run in the environment) with one process left behind."""
    started = []

    def start(mode="session", *, max_runtime_seconds=None):
        task_id, run_id = _claimed_card(conn, max_runtime_seconds=max_runtime_seconds)
        receipt = tmp_path / f"{task_id}.json"
        process = subprocess.Popen([sys.executable, "-c", WORKER, mode, str(receipt)],
                                   env=_run_environment(task_id, run_id), start_new_session=True)
        started.append([process, None])
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = json.loads(receipt.read_text())
        started[-1][1] = child
        # Session and group of its own: a signal to the worker, or to the worker's group, never reaches it.
        assert child["sid"] == child["pgid"] == child["pid"] != os.getsid(process.pid)
        assert (child["ppid"] == process.pid) == (mode != "detached")
        _record_worker(conn, task_id, process.pid)
        return task_id, run_id, process, child

    try:
        yield start
    finally:
        for process, child in started:
            if child:
                _end(child)
            if process.poll() is None:
                process.kill()
            process.wait()


@pytest.fixture
def sleeper():
    """Start a live process the engine never started; ``env`` may claim a run."""
    started = []

    def start(env=None):
        process = subprocess.Popen(SLEEP, start_new_session=True, env=env)
        started.append(process)
        return process

    try:
        yield start
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait()


def _tick(conn):
    return kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)


def _events(conn, task_id, kind):
    return [json.loads(row["payload"] or "{}") for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def _listed(conn, task_id):
    return [row["pid"] for row in conn.execute(
        "SELECT pid FROM task_run_processes WHERE task_id=? ORDER BY pid", (task_id,))]


def _stale_heartbeat(conn, task_id, monkeypatch):
    monkeypatch.setattr(kb, "DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS", 1)
    monkeypatch.setattr(kb, "_STALE_HEARTBEAT_GAP_SECONDS", 1)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (int(time.time()) - 7200, task_id))


def _budget_exhausted(conn, task_id):
    """What the worker of run 938 did: it reported the exhausted iteration budget and closed its own run."""
    kb._record_task_failure(
        conn, task_id, "Iteration budget exhausted (400/400)",
        outcome="timed_out", release_claim=True, end_run=True,
    )


def _leave(process):
    """The worker exits by itself."""
    process.terminate()
    process.wait(timeout=10)


# ---------------------------------------------------------------------------
# The engine ends the worker: runtime limit, stale, expired claim, operator
# ---------------------------------------------------------------------------

ENGINE_ENDS_THE_WORKER = {
    "runtime limit": lambda conn, task_id: kb.enforce_max_runtime(conn),
    "stale running": lambda conn, task_id: kb.detect_stale_running(conn, stale_timeout_seconds=1),
    "expired claim": lambda conn, task_id: kb.release_stale_claims(conn),
    "operator reclaim": lambda conn, task_id: kb.reclaim_task(conn, task_id),
}


@pytest.mark.parametrize("mode", ["session", "scrubbed"])
@pytest.mark.parametrize("path", sorted(ENGINE_ENDS_THE_WORKER))
def test_engine_ends_the_worker_together_with_the_child_it_left_in_another_session(conn, start_worker, monkeypatch, path, mode):
    task_id, _, process, child = start_worker(mode, max_runtime_seconds=1)
    _stale_heartbeat(conn, task_id, monkeypatch)

    ENGINE_ENDS_THE_WORKER[path](conn, task_id)

    assert process.wait(timeout=10) is not None, "the worker itself was not ended"
    assert _gone(child), "the worker is gone and the process it started is still running, adopted by init"
    assert kb.get_task(conn, task_id).status != "running"


def test_process_that_left_the_worker_before_the_signal_is_ended_by_the_tick(conn, start_worker):
    """Init already is its parent when the worker is signalled; the run in its environment is what tells it apart."""
    task_id, _, process, child = start_worker("detached", max_runtime_seconds=1)

    assert _tick(conn).timed_out == [task_id]

    assert process.wait(timeout=10) is not None
    assert _gone(child), "the run timed out and a process it started is still running"


# ---------------------------------------------------------------------------
# The worker closes its own run and exits: normal end, exhausted budget
# ---------------------------------------------------------------------------

WORKER_CLOSES_ITS_RUN = {
    "completed": lambda conn, task_id: kb.complete_task(conn, task_id, summary="delivered"),
    "budget exhausted": _budget_exhausted,
}


@pytest.mark.parametrize("mode", ["session", "scrubbed", "detached"])
@pytest.mark.parametrize("ending", sorted(WORKER_CLOSES_ITS_RUN))
def test_worker_that_closes_its_own_run_and_exits_has_its_process_ended_by_the_next_tick(conn, start_worker, ending, mode):
    """Run 938: nobody signalled the worker. It closed the run and left; the reconciliation came 43 s later."""
    task_id, run_id, process, child = start_worker(mode)
    WORKER_CLOSES_ITS_RUN[ending](conn, task_id)
    assert kb.get_task(conn, task_id).current_run_id is None
    _leave(process)
    assert _alive(child), "the premise: the process outlives the worker that started it"

    _tick(conn)

    assert _gone(child), "the run is closed, its worker is gone and the process it started is still running"
    [event] = _events(conn, task_id, "worker_processes_ended")
    assert [p["pid"] for p in event["processes"]] == [child["pid"]] and event["still_alive"] == []
    assert _listed(conn, task_id) == []


# ---------------------------------------------------------------------------
# The worker goes away with its run still open: a question for the Principal, a crash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["session", "detached"])
def test_worker_that_dies_with_its_run_open_has_its_process_ended_by_the_tick_that_notices(conn, start_worker, mode):
    """The commonest ending: only a later tick closes the run, and by then nobody is the parent of what it started."""
    task_id, _, process, child = start_worker(mode)
    _tick(conn)
    assert process.poll() is None and _alive(child), "a tick never touches a running worker or its processes"

    process.kill()
    process.wait(timeout=10)
    _tick(conn)

    assert kb.get_task(conn, task_id).status != "running"
    assert _gone(child), "the run was closed for a dead worker and the process it started is still running"


# ---------------------------------------------------------------------------
# Who is never signalled
# ---------------------------------------------------------------------------

def _closed_run_of_a_worker_that_left(conn, start_worker, mode="session"):
    task_id, run_id, process, child = start_worker(mode)
    worker = (process.pid, kb._process_start_time(process.pid))
    _budget_exhausted(conn, task_id)
    _leave(process)
    return task_id, run_id, worker, child


def test_process_that_only_shares_the_pid_of_a_listed_one_is_never_signalled(conn, start_worker, sleeper):
    task_id, run_id, (worker_pid, worker_started_at), child = _closed_run_of_a_worker_that_left(conn, start_worker)
    assert _listed(conn, task_id) == [child["pid"]]
    # The run also lists a process that ended long ago; its PID now belongs to somebody else's process.
    bystander = sleeper()
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_run_processes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, bystander.pid, kb._process_start_time(bystander.pid) - 300,
             worker_pid, worker_started_at, int(time.time())))
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


def test_identity_is_checked_before_every_signal(sleeper):
    """The signal primitive itself: no signal for a PID whose process was created at another time."""
    process = sleeper()
    real = _identity(process.pid)
    signals = []
    other_time = dict(real, started_at=real["started_at"] - 300)
    assert kb._end_processes([other_time], lambda pid, sig: signals.append((pid, sig)), grace=0) == []
    assert signals == []
    # A hook that delivers nothing: the process is asked to end, then killed, and reported as still alive.
    assert kb._end_processes([real], lambda pid, sig: signals.append((pid, sig)), grace=0) == [real]
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert kb._end_processes([_identity(os.getpid())], lambda pid, sig: signals.append((pid, sig)), grace=0) == []
    assert len(signals) == 2, "the process running the reconciliation is never a target"


def test_process_is_tried_once_and_named_when_it_could_not_be_ended(conn, start_worker):
    task_id, run_id, _, child = _closed_run_of_a_worker_that_left(conn, start_worker)
    signals = []

    assert kb.end_closed_run_processes(conn, signal_fn=lambda pid, sig: signals.append((pid, sig))) == [run_id]
    assert kb.end_closed_run_processes(conn, signal_fn=lambda pid, sig: signals.append((pid, sig))) == []

    assert signals == [(child["pid"], signal.SIGTERM), (child["pid"], signal.SIGKILL)], "one attempt, not one per tick"
    [event] = _events(conn, task_id, "worker_processes_ended")
    assert event["processes"] == [] and [p["pid"] for p in event["still_alive"]] == [child["pid"]]


def test_process_of_a_running_card_is_spared_whatever_an_older_run_says(conn, start_worker):
    current, _, process, child = start_worker("session")
    left = subprocess.Popen([sys.executable, "-c", "pass"])
    gone_worker = (left.pid, kb._process_start_time(left.pid))
    older, older_run = _claimed_card(conn)
    _record_worker(conn, older, left.pid)
    left.wait(timeout=10)
    assert kb.reclaim_task(conn, older)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_run_processes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (older_run, older, child["pid"], child["started_at"], gone_worker[0], gone_worker[1], int(time.time())))

    _tick(conn)

    assert process.poll() is None and _alive(child), "a process of a running card was ended on an older run's word"
    assert _listed(conn, older) == [] and _events(conn, older, "worker_processes_ended") == []


def test_processes_stay_while_the_worker_that_closed_its_run_is_still_leaving(conn, sleeper, monkeypatch):
    """The worker that closes its own run lists its processes itself; they are ended only after it is gone."""
    child = _identity(sleeper().pid)
    task_id, run_id = _claimed_card(conn)
    _record_worker(conn, task_id, os.getpid())  # this process is the worker
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    _budget_exhausted(conn, task_id)
    assert child["pid"] in _listed(conn, task_id)

    assert kb.end_closed_run_processes(conn) == []

    assert _alive(child) and child["pid"] in _listed(conn, task_id)


def test_dispatcher_recorded_as_a_worker_does_not_list_its_own_children(conn, sleeper):
    """Its children are other cards' workers. Only a worker carries the card in its environment."""
    child = _identity(sleeper().pid)
    task_id, _ = _claimed_card(conn)
    _record_worker(conn, task_id, os.getpid())
    _budget_exhausted(conn, task_id)
    assert _listed(conn, task_id) == [] and _alive(child)


def test_open_native_command_of_the_run_is_left_to_its_own_reconciliation(conn, start_worker):
    """A native command has its own receipt and exit grace; the rest waits until that receipt is closed."""
    from hermes_cli import nfos_tool

    task_id, run_id, _, child = _closed_run_of_a_worker_that_left(conn, start_worker)
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


def test_environment_that_names_a_run_of_another_board_is_not_this_board_business(conn, start_worker, sleeper, tmp_path):
    task_id, run_id, _, child = _closed_run_of_a_worker_that_left(conn, start_worker)
    elsewhere = sleeper(_run_environment(task_id, run_id, db=str(tmp_path / "other-board" / "kanban.db")))

    _tick(conn)

    assert _gone(child)
    assert elsewhere.poll() is None, "same card and run numbers on another board's database"


def test_environment_is_not_enough_without_a_worker_on_record(conn, start_worker, sleeper):
    """Nobody can say that worker is gone, so nothing is an orphan of it."""
    task_id, run_id, _, child = _closed_run_of_a_worker_that_left(conn, start_worker, "detached")
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='spawned'", (task_id,))
        conn.execute("DELETE FROM task_run_processes WHERE task_id=?", (task_id,))
    claimant = sleeper(_run_environment(task_id, run_id))

    _tick(conn)

    assert _alive(child) and claimant.poll() is None
    assert _events(conn, task_id, "worker_processes_ended") == []


def test_worker_whose_creation_time_reads_a_second_off_is_still_the_worker(conn, start_worker):
    """The clock was adjusted between the two readings. The process with the worker's PID that carries the
    run is the worker, alive: neither it nor what it started is an orphan."""
    task_id, run_id, process, child = start_worker("session")
    _budget_exhausted(conn, task_id)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_run_processes SET worker_started_at = worker_started_at + 1 WHERE run_id=?", (run_id,))
        for row in conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='spawned'", (task_id,)).fetchall():
            payload = json.loads(row["payload"])
            payload["worker_started_at"] += 1
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))

    _tick(conn)

    assert process.poll() is None and _alive(child)
    assert _events(conn, task_id, "worker_processes_ended") == []


def test_run_named_by_the_dispatcher_own_environment_tells_nothing_apart(conn, start_worker, sleeper, monkeypatch):
    """A dispatcher launched from inside a worker's shell hands that run to everything it starts."""
    task_id, run_id, _, child = _closed_run_of_a_worker_that_left(conn, start_worker, "detached")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    started_by_the_dispatcher = sleeper()

    _tick(conn)

    assert started_by_the_dispatcher.poll() is None and _alive(child)
    assert _events(conn, task_id, "worker_processes_ended") == []
