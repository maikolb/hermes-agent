"""URGENT2_20260910: an urgent card with an open NFOS decision does not create the burst slot."""
import time

from hermes_cli import kanban_db as kb


def test_urgent_with_open_decision_creates_no_burst(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, task_role, priority) VALUES ('t_urg', 'a', 'ready', ?, 'work', 100)",
        (now,),
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nfos_decisions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id INTEGER NOT NULL, "
        "kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', question TEXT NOT NULL, context TEXT NOT NULL, "
        "answer TEXT, author TEXT, action TEXT, spec_revision INTEGER NOT NULL, created_at INTEGER NOT NULL, "
        "resolved_at INTEGER, dispatched_at INTEGER)"
    )
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
    conn.execute(
        "INSERT INTO nfos_decisions (id, task_id, run_id, kind, status, question, context, spec_revision, created_at) "
        "VALUES ('d1', 't_urg', 1, 'impediment', 'human', 'q?', '{}', 1, ?)",
        (now,),
    )
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 0
    conn.execute("UPDATE nfos_decisions SET status='resolved' WHERE id='d1'")
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
