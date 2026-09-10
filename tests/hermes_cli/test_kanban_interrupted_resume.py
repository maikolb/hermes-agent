"""INTERRUPTED_20260910: worker morto junto com o gateway retoma sem falha, sem gave_up, e volta na
frente da fila; morte de worker nascido neste processo continua sendo crash."""
import subprocess
import time

from hermes_cli import kanban_db


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _db(tmp_path):
    return kanban_db.connect(tmp_path / "kanban.db")


def _running_task(conn, tid, worker_started_at, pid=None):
    now = int(time.time())
    pid = pid or _dead_pid()
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, started_at, worker_pid, worker_started_at, "
        "claim_lock, consecutive_failures, task_role, priority) "
        "VALUES (?, 'a', 'running', ?, ?, ?, ?, ?, 0, 'work', 3)",
        (tid, now - 7200, now - 3600, pid, worker_started_at, kanban_db._claimer_id()),
    )
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, worker_pid) VALUES (?, 'running', ?, ?)",
        (tid, now - 3600, pid),
    )
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (cur.lastrowid, tid))
    conn.commit()
    return cur.lastrowid


def _task(conn, tid):
    return conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)
    ).fetchone()


def _kinds(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    )]


def test_worker_older_than_gateway_is_interrupted_not_crashed(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    now = time.time()
    monkeypatch.setattr(kanban_db, "_gateway_process_start", lambda: now - 10)
    monkeypatch.setattr(kanban_db, "_resolve_crash_grace_seconds", lambda: 0)
    run_id = _running_task(conn, "t_1", worker_started_at=now - 600)
    crashed = kanban_db.detect_crashed_workers(conn)
    assert crashed == []
    assert kanban_db.detect_crashed_workers._last_interrupted == ["t_1"]
    t = _task(conn, "t_1")
    assert t["status"] == "ready" and t["consecutive_failures"] == 0
    run = conn.execute("SELECT status, outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "reclaimed" and run["outcome"] == "reclaimed"
    kinds = _kinds(conn, "t_1")
    assert "interrupted" in kinds and "crashed" not in kinds and "gave_up" not in kinds


def test_four_at_once_do_not_trip_the_systemic_breaker(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    now = time.time()
    monkeypatch.setattr(kanban_db, "_gateway_process_start", lambda: now - 10)
    monkeypatch.setattr(kanban_db, "_resolve_crash_grace_seconds", lambda: 0)
    for i in range(4):
        _running_task(conn, f"t_{i}", worker_started_at=now - 600)
    assert kanban_db.detect_crashed_workers(conn) == []
    for i in range(4):
        t = _task(conn, f"t_{i}")
        assert t["status"] == "ready" and t["consecutive_failures"] == 0
        assert "gave_up" not in _kinds(conn, f"t_{i}")


def test_worker_born_in_this_process_still_counts_as_crash(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    now = time.time()
    monkeypatch.setattr(kanban_db, "_gateway_process_start", lambda: now - 3600)
    monkeypatch.setattr(kanban_db, "_resolve_crash_grace_seconds", lambda: 0)
    _running_task(conn, "t_1", worker_started_at=now - 100)
    assert kanban_db.detect_crashed_workers(conn) == ["t_1"]
    t = _task(conn, "t_1")
    assert t["status"] == "ready" and t["consecutive_failures"] == 1
    assert "crashed" in _kinds(conn, "t_1") and "interrupted" not in _kinds(conn, "t_1")


def test_interrupted_cards_come_first_until_claimed_again(tmp_path):
    conn = _db(tmp_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, task_role, priority) VALUES "
        "('t_old', 'a', 'ready', ?, 'work', 3), ('t_int', 'b', 'ready', ?, 'work', 3)",
        (now - 9000, now - 100),
    )
    conn.commit()
    with kanban_db.write_txn(conn):
        kanban_db._append_event(conn, "t_int", "claimed", {"run_id": 1})
        kanban_db._append_event(conn, "t_int", "interrupted", {"pid": 1})
    rows = conn.execute(
        "SELECT id, assignee FROM tasks WHERE status = 'ready' AND task_role = 'work' "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    assert [r["id"] for r in rows] == ["t_old", "t_int"]
    assert [r["id"] for r in kanban_db._resume_first(conn, rows)] == ["t_int", "t_old"]
    with kanban_db.write_txn(conn):
        kanban_db._append_event(conn, "t_int", "claimed", {"run_id": 2})
    assert [r["id"] for r in kanban_db._resume_first(conn, rows)] == ["t_old", "t_int"]
