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


def test_cancelled_case_does_not_lend_priority(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    for tid in ("t_c1", "t_c2", "t_c3"):
        _card(conn, tid, "operation", 100, status="done")
        _card(conn, "causa_" + tid, "code", 0)
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (tid, "causa_" + tid))
    conn.execute("UPDATE tasks SET title='[CANCELADO] pedido' WHERE id='t_c1'")
    conn.execute("INSERT INTO nfos_workflows (task_id, request_id, stage, spec_revision, next_action, state_json, updated_at) "
                 "VALUES ('t_c2', NULL, 'cancelled', 0, '', '{}', 1)")
    conn.commit()
    assert kb.effective_priority(conn, "causa_t_c1") == 0, "título [CANCELADO]"
    assert kb.effective_priority(conn, "causa_t_c2") == 0, "estágio cancelled"
    assert kb.effective_priority(conn, "causa_t_c3") == 100, "caso vivo (done) continua emprestando"


def test_same_rule_as_the_workflow_module_for_chamado_parents(tmp_path):
    """Pai é caso (chamado do portal mesmo sendo code, ou operation); filho é causa (code sem chamado)."""
    import json
    conn = kb.connect(tmp_path / "k.db")

    def request(tid, chamado):
        payload = {"origin": {"portal": {"chamado": "CS-1"}}} if chamado else {"origin": {"topic": 41}}
        conn.execute("INSERT INTO nfos_requests (id, source_key, payload, created_at) VALUES (?, ?, ?, 1)",
                     ("r_" + tid, "k_" + tid, json.dumps(payload)))
        conn.execute("INSERT INTO nfos_workflows (task_id, request_id, stage, spec_revision, next_action, state_json, updated_at) "
                     "VALUES (?, ?, 'analysis', 0, '', '{}', 1)", (tid, "r_" + tid))

    _card(conn, "chamado_code", "code", 100)
    _card(conn, "code_puro", "code", 90)
    _card(conn, "causa", "code", 0)
    _card(conn, "causa_de_chamado", "code", 0)
    for tid, chamado in (("chamado_code", True), ("code_puro", False), ("causa", False), ("causa_de_chamado", True)):
        request(tid, chamado)
    for parent, child in (("chamado_code", "causa"), ("code_puro", "causa"), ("chamado_code", "causa_de_chamado")):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
    conn.commit()
    assert kb.effective_priority(conn, "causa") == 100, "chamado code é caso e empresta"
    conn.execute("UPDATE tasks SET priority=0 WHERE id='chamado_code'")
    conn.commit()
    assert kb.effective_priority(conn, "causa") == 0, "code sem chamado não é caso"
    conn.execute("UPDATE tasks SET priority=100 WHERE id='chamado_code'")
    conn.commit()
    assert kb.effective_priority(conn, "causa_de_chamado") == 0, "filho que é chamado não é causa"
