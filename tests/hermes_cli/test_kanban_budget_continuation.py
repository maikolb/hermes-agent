"""BUDGET_CONTINUE_20260910: esgotamento de orçamento com progresso persistido volta para a fila sem
contar falha nem disparar gave_up; teto por janela de desbloqueio; sem progresso segue o breaker."""
import time

from hermes_cli import kanban_db

ERR = "Iteration budget exhausted (200/200) — task could not complete within the allowed iterations"


def _db(tmp_path, monkeypatch, cap=3):
    monkeypatch.setattr(kanban_db, "_budget_continuation_cap", lambda: cap)
    conn = kanban_db.connect(tmp_path / "kanban.db")
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_1', 'a', 'running', ?, 0)",
        (now,),
    )
    with kanban_db.write_txn(conn):
        kanban_db._append_event(conn, "t_1", "created", {})
    return conn


def _start_run(conn, progress=True):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES ('t_1', 'running', ?)", (now,)
    )
    run_id = cur.lastrowid
    conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = 't_1'", (run_id,))
    conn.commit()
    if progress:
        with kanban_db.write_txn(conn):
            kanban_db._append_event(conn, "t_1", "nfos_progress", {"stage": "implement"}, run_id=run_id)
    return run_id


def _exhaust(conn):
    return kanban_db._record_task_failure(
        conn, "t_1", ERR, outcome="timed_out", release_claim=True, end_run=True,
    )


def _task(conn):
    return conn.execute(
        "SELECT status, consecutive_failures, current_run_id FROM tasks WHERE id = 't_1'"
    ).fetchone()


def _kinds(conn):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = 't_1' ORDER BY id")]


def test_budget_exhaustion_with_progress_is_a_continuation(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _start_run(conn)
    assert _exhaust(conn) is False
    t = _task(conn)
    assert t["status"] == "ready" and t["consecutive_failures"] == 0 and t["current_run_id"] is None
    run = conn.execute("SELECT status, outcome, ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "timed_out" and run["outcome"] == "timed_out" and run["ended_at"] is not None
    kinds = _kinds(conn)
    assert "budget_continued" in kinds and "gave_up" not in kinds


def test_without_progress_the_breaker_counts(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    _start_run(conn, progress=False)
    assert _exhaust(conn) is False
    assert _task(conn)["consecutive_failures"] == 1
    assert "budget_continued" not in _kinds(conn)


def test_cap_then_breaker_then_unblock_resets(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, cap=2)
    for _ in range(2):
        _start_run(conn)
        assert _exhaust(conn) is False
        assert _task(conn)["consecutive_failures"] == 0
    # terceira exaustão: teto atingido, conta falha (1 < limite 2, volta a ready)
    _start_run(conn)
    _exhaust(conn)
    assert _task(conn)["consecutive_failures"] == 1
    # quarta: dispara o breaker
    _start_run(conn)
    assert _exhaust(conn) is True
    assert _task(conn)["status"] == "blocked" and "gave_up" in _kinds(conn)
    # desbloqueio abre janela nova
    with kanban_db.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready', consecutive_failures = 0 WHERE id = 't_1'")
        kanban_db._append_event(conn, "t_1", "unblocked", {})
    _start_run(conn)
    assert _exhaust(conn) is False
    assert _task(conn)["consecutive_failures"] == 0
    assert _kinds(conn).count("budget_continued") == 3


def test_cap_zero_disables(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, cap=0)
    _start_run(conn)
    _exhaust(conn)
    assert _task(conn)["consecutive_failures"] == 1


def test_other_outcomes_untouched(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    _start_run(conn)
    kanban_db._record_task_failure(
        conn, "t_1", "pid dead", outcome="crashed", release_claim=True, end_run=True,
    )
    assert _task(conn)["consecutive_failures"] == 1
    assert "budget_continued" not in _kinds(conn)
