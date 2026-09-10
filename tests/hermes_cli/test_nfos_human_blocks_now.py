"""HUMAN_BLOCK_NOW_20260910: a decisão human bloqueia o card na hora quando o worker do run já saiu; com worker
vivo o próprio worker bloqueia (contrato existente). OPEN_DECISION_SKIP: card com pergunta aberta não é relançado."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path / "kanban.db"


def _started(conn):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "9"},
        text="Corrigir disciplinas do cargo Delegado.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def _worker_gone_card_requeued(conn, task):
    """O que o detector de crash faz quando o worker sai com pergunta aberta: run encerrado, card de volta a ready."""
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL WHERE id=?", (task.id,))
    conn.commit()


def test_human_decision_blocks_the_card_at_once_when_the_worker_is_gone(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Qual cargo exato está afetado no admin?", context={})
        _worker_gone_card_requeued(conn, task)
        delivery.resolve_decision(conn, decision, action="human", answer="PERGUNTA para Maikol: qual cargo exato está afetado?", author="Principal")
        t = kb.get_task(conn, task.id)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"


def test_live_worker_keeps_the_contract_and_blocks_itself(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Qual cargo exato está afetado no admin?", context={})
        delivery.resolve_decision(conn, decision, action="human", answer="PERGUNTA para Maikol: qual cargo?", author="Principal")
        assert kb.get_task(conn, task.id).status == "running"
        assert kb.block_task(conn, task.id, reason="PERGUNTA para Maikol: qual cargo?", kind="needs_input", expected_run_id=task.current_run_id)


def test_open_decision_is_seen_by_the_dispatcher_guard(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        assert not kb._nfos_decision_open(conn, task.id)
        delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment", question="Falta o print do caso?", context={})
        assert kb._nfos_decision_open(conn, task.id)
