"""CAUSE_URGENCY_20261015: a causa (code) ligada a um caso urgente (operation) entra na frente da fila e abre a vaga extra."""
import time

from hermes_cli import kanban_db as kb


def _card(conn, tid, delivery, priority, status="ready"):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, task_role, priority, delivery_type) VALUES (?, ?, ?, ?, 'work', ?, ?)",
        (tid, tid, status, int(time.time()), priority, delivery),
    )


def test_cause_with_urgent_case_uses_the_burst_slot_and_goes_first(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    _card(conn, "t_caso", "operation", 100, status="running")
    _card(conn, "t_causa", "code", 0)
    _card(conn, "t_outra", "code", 0)
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_caso', 't_causa')")
    conn.commit()
    assert kb.effective_priority(conn, "t_causa") == 100 and kb.effective_priority(conn, "t_outra") == 0
    # O caso urgente já roda; a causa ligada é urgente na fila, mas sem outro urgente rodando só o caso conta.
    conn.execute("UPDATE tasks SET status='done' WHERE id='t_caso'")
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
    rows = conn.execute("SELECT * FROM tasks WHERE id IN ('t_outra', 't_causa') ORDER BY id").fetchall()
    assert [r["id"] for r in kb._resume_first(conn, rows)] == ["t_causa", "t_outra"]


def test_archived_case_or_a_case_card_does_not_lend_priority(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    _card(conn, "t_caso", "operation", 100)
    _card(conn, "t_causa", "code", 0)
    _card(conn, "t_filho_op", "operation", 0)
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_caso', 't_causa')")
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_caso', 't_filho_op')")
    conn.commit()
    assert kb.effective_priority(conn, "t_filho_op") == 0, "só a causa (code) herda"
    conn.execute("UPDATE tasks SET status='archived' WHERE id='t_caso'")
    conn.commit()
    assert kb.effective_priority(conn, "t_causa") == 0
