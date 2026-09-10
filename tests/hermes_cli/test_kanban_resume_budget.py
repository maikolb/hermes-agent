"""RESUME_BUDGET_20260910: a budget continuation resumes before fresh cards, after urgent ones."""
import time

from hermes_cli import kanban_db as kb


def test_budget_continued_resumes_before_fresh_cards(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    now = int(time.time())
    conn.execute("INSERT INTO tasks (id, title, status, created_at, task_role, priority) VALUES "
                 "('t_old', 'a', 'ready', ?, 'work', 3), ('t_bud', 'b', 'ready', ?, 'work', 3), ('t_urg', 'c', 'ready', ?, 'work', 100)",
                 (now - 9000, now - 100, now - 50))
    conn.commit()
    with kb.write_txn(conn):
        kb._append_event(conn, "t_bud", "claimed", {"run_id": 1})
        kb._append_event(conn, "t_bud", "budget_continued", {"continuation": 1})
    rows = conn.execute("SELECT id, assignee, priority FROM tasks WHERE status='ready' ORDER BY priority DESC, created_at ASC").fetchall()
    assert [r["id"] for r in kb._resume_first(conn, rows)] == ["t_urg", "t_bud", "t_old"]
    with kb.write_txn(conn):
        kb._append_event(conn, "t_bud", "claimed", {"run_id": 2})
    assert [r["id"] for r in kb._resume_first(conn, rows)] == ["t_urg", "t_old", "t_bud"]
