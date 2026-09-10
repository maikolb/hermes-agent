"""HUMAN_BLOCK_NOW_20260910: a decisão human do principal bloqueia o card na hora, e um respawn não a apaga."""
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


def test_human_decision_blocks_the_card_immediately(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Qual cargo exato está afetado no admin?", context={})
        delivery.resolve_decision(conn, decision, action="human", answer="PERGUNTA para Maikol: qual cargo exato está afetado?", author="Principal")
        t = kb.get_task(conn, task.id)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"


def test_reconcile_blocks_even_when_the_run_changed(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Qual cargo exato está afetado no admin?", context={})
        # simula a decisão pertencendo a um run antigo e o card correndo de novo
        conn.execute("UPDATE nfos_decisions SET status='human', action='human', answer='PERGUNTA para Maikol: qual cargo?', author='Principal', run_id=run_id-1 WHERE id=?", (decision,))
        conn.commit()
        assert kb.get_task(conn, task.id).status == "running"
        delivery.reconcile_human_answers(conn)
        assert kb.get_task(conn, task.id).status == "blocked"
