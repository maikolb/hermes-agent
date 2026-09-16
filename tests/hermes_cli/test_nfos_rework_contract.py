"""REWORK_CONTRACT_20260914: card NFOS em worktree com a política Git do board selada, reclassificado para código pelo save_spec
(required volta a 1), nunca grava o manifesto de pedido da fila de revisão. O portão de fechamento trata isso como contrato não
inicializado, com saída só por publicação NFOS confirmada, e não como adulteração (t_1668f98c, retrabalho do CCS-9)."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

POLICY = {"base_branch": "main", "required": True}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _card(conn, n):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(900 + n)},
        text=f"Corrigir pontuação {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod", "method": "ui", "result": "errado"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Pontuação correta", "criteria": [{"id": "C1", "text": "análise sem -1", "mandatory": True,
            "probe": {"kind": "sql", "query": "SELECT score FROM analysis WHERE id = 1", "expect": {"scalar": 31}}}],
        "steps": ["Reparar"], "delivery_type": "report", "size": "P"}, author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _row(**kw):
    base = {"required": 1, "policy_json": None, "policy_fingerprint": None, "request_json": None, "receipt_json": None}
    base.update(kw)
    return base


def _required(conn, task_id):
    row = conn.execute("SELECT required FROM task_git_delivery WHERE task_id=?", (task_id,)).fetchone()
    return None if row is None else int(row[0])


def _events(conn, task_id, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def _blocked_codes(conn, task_id):
    return [e.get("code") for e in _events(conn, task_id, "completion_blocked_delivery")]


def _seal_board_policy(conn, task_id):
    text, fingerprint = kb._canonical_delivery_document(POLICY)
    conn.execute("UPDATE task_git_delivery SET policy_json=?, policy_fingerprint=? WHERE task_id=?", (text, fingerprint, task_id))
    conn.commit()


def _publish(conn, task, n, status="confirmed"):
    now = int(time.time())
    conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,created_at,updated_at) "
                 "VALUES(?,?,?,'merge','https://github.com/x/y','abc1234',?,?,?)",
                 (f"m{n}", task.id, task.current_run_id, status, now, now))
    conn.commit()


def _owner_gate(monkeypatch):
    """Produção roda em owner mode; o relatório coerente é dado, para o portão Git decidir sozinho."""
    monkeypatch.setattr(delivery, "_owner_mode", lambda: True)
    monkeypatch.setattr(delivery, "completion_ready", lambda conn, task_id, evidence_check=None: True)
    monkeypatch.setattr(delivery, "completion_evidence_check", lambda conn, task_id: None)


def _reclassified_code_card(conn, n):
    """Trajetória do t_1668f98c: card NFOS adotado em worktree (required=0) com a política do board selada no create_task; o save_spec
    real reclassifica para código e religa required=1 sem pedido nem recibo."""
    task = _card(conn, n)
    conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (task.id,))
    conn.execute("INSERT OR IGNORE INTO task_git_delivery(task_id,required_at,required) VALUES(?,?,0)", (task.id, int(time.time())))
    conn.commit()
    _seal_board_policy(conn, task.id)
    assert _required(conn, task.id) == 0
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Pontuação correta", "criteria": [{"id": "C1", "text": "análise sem -1", "mandatory": True,
            "probe": {"kind": "sql", "query": "SELECT score FROM analysis WHERE id = 1", "expect": {"scalar": 31}}}],
        "steps": ["Corrigir o código"], "delivery_type": "code", "size": "P"}, author="worker", evidence={"source": "worker"})
    assert kb.get_task(conn, task.id).delivery_type == "code" and _required(conn, task.id) == 1
    return kb.get_task(conn, task.id)


def test_uninitialized_contract_rule():
    text, fingerprint = kb._canonical_delivery_document(POLICY)
    rule = kb._delivery_contract_uninitialized
    assert rule(None, True) is False
    assert rule(_row(required=0), True) is False
    assert rule(_row(), False) is True  # CLOSURE_RECOVERY_20260911: sem política
    assert rule(_row(policy_json=text, policy_fingerprint=fingerprint), True) is True  # card NFOS: política intacta, sem pedido
    assert rule(_row(policy_json=text, policy_fingerprint=fingerprint), False) is False  # fila de revisão comum segue exigindo o pedido
    assert rule(_row(policy_json=text, policy_fingerprint="outra"), True) is False  # política alterada segue adulterada
    assert rule(_row(policy_json="{não é json", policy_fingerprint=fingerprint), True) is False
    assert rule(_row(policy_json=text, policy_fingerprint=fingerprint, request_json="{}"), True) is False
    assert rule(_row(policy_json=text, policy_fingerprint=fingerprint, receipt_json="{}"), True) is False


def test_reclassified_code_card_without_publication_is_blocked_as_uninitialized_not_tampered(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _reclassified_code_card(conn, 3)
        _owner_gate(monkeypatch)
        assert kb.complete_task(conn, task.id, result="entregue", summary="entregue") is False
        assert _blocked_codes(conn, task.id) == ["delivery_contract_uninitialized"]
        assert kb.get_task(conn, task.id).status != "done"


def test_reclassified_code_card_closes_only_after_confirmed_publication(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _reclassified_code_card(conn, 4)
        _owner_gate(monkeypatch)
        _publish(conn, task, 4, status="unknown")
        assert kb.complete_task(conn, task.id, result="entregue", summary="entregue") is False
        conn.execute("UPDATE nfos_effects SET status='confirmed' WHERE id='m4'")
        conn.commit()
        assert kb.complete_task(conn, task.id, result="entregue", summary="entregue") is True
        assert [e["waived"] for e in _events(conn, task.id, "delivery_contract_uninitialized")] == [False, True]
        assert _blocked_codes(conn, task.id) == ["delivery_contract_uninitialized"]
        assert kb.get_task(conn, task.id).status == "done"


def test_altered_sealed_policy_is_still_refused_as_tampered(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _reclassified_code_card(conn, 5)
        conn.execute("UPDATE task_git_delivery SET policy_fingerprint=? WHERE task_id=?", ("0" * 64, task.id))
        conn.commit()
        _owner_gate(monkeypatch)
        _publish(conn, task, 5)
        assert kb.complete_task(conn, task.id, result="entregue", summary="entregue") is False
        assert _blocked_codes(conn, task.id) == ["delivery_contract_tampered"]
        assert kb.get_task(conn, task.id).status != "done"


def test_rework_child_of_code_card_cannot_close_before_nfos_enrollment(board, monkeypatch):
    with kb.connect_closing() as conn:
        parent = _card(conn, 6)
        conn.execute("UPDATE tasks SET status='done', completed_at=?, worker_pid=NULL, claim_lock=NULL, current_run_id=NULL, "
                     "delivery_type='code', workspace_kind='worktree', workspace_path=NULL WHERE id=?", (int(time.time()), parent.id))
        conn.commit()
        res = delivery.request_rework(conn, parent.id, criterion="C1", reason="análise ainda orienta -1", evidence=["registro 3fa80d2b"], author="auditor")
        child = kb.get_task(conn, res["task_id"])
        assert child.delivery_type == "code" and child.workspace_kind == "worktree"
        assert delivery.get_workflow(conn, child.id) is None and _required(conn, child.id) == 1
        _seal_board_policy(conn, child.id)
        _owner_gate(monkeypatch)
        assert kb.complete_task(conn, child.id, result="entregue", summary="entregue") is False
        assert _blocked_codes(conn, child.id) == ["delivery_review_required"]
        assert kb.get_task(conn, child.id).status != "done"
