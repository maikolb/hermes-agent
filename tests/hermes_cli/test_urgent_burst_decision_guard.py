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
        "CREATE TABLE IF NOT EXISTS nfos_decisions (id TEXT PRIMARY KEY, task_id TEXT, status TEXT, created_at INTEGER)"
    )
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
    conn.execute("INSERT INTO nfos_decisions (id, task_id, status, created_at) VALUES ('d1', 't_urg', 'human', ?)", (now,))
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 0
    conn.execute("UPDATE nfos_decisions SET status='resolved' WHERE id='d1'")
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
