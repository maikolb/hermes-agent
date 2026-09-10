"""BLOCK_LESS7_20260910: checar produção antes da spec é mecanismo, não texto."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path / "kanban.db"


def _started(conn):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "42"},
        text="Corrigir o gabarito da questão 12.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    assert request["id"] == rid
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def _spec(conn, task):
    return delivery.save_spec(conn, task.id, task.current_run_id,
        {"goal": "Gabarito correto", "criteria": [{"id": "AC1", "text": "Questão 12 com gabarito B"}],
         "steps": ["Ler produção", "Corrigir"], "delivery_type": "report"},
        author="worker", evidence={"source": "worker"})


def test_new_spec_refused_without_precheck(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        with pytest.raises(delivery.WorkflowError, match="precheck"):
            _spec(conn, task)


def test_precheck_already_delivered_points_to_completion_and_unlocks_spec(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        record = delivery.record_precheck(conn, task.id, task.current_run_id, {
            "checked": [{"target": "https://admin.example/questions/12", "method": "http", "result": "gabarito já é B"}],
            "verdict": "already_delivered"})
        assert record["verdict"] == "already_delivered"
        wf = delivery.get_workflow(conn, task.id)
        assert "kanban_complete" in wf["next_action"]
        assert _spec(conn, task) == 1
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (task.id,))]
        assert "nfos_precheck" in kinds


def test_precheck_rejects_empty_or_unknown_verdict(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        with pytest.raises(delivery.WorkflowError, match="checked"):
            delivery.record_precheck(conn, task.id, task.current_run_id, {"checked": [], "verdict": "partial"})
        with pytest.raises(delivery.WorkflowError, match="verdict"):
            delivery.record_precheck(conn, task.id, task.current_run_id, {
                "checked": [{"target": "prod", "method": "db", "result": "ok"}], "verdict": "maybe"})


def test_gate_is_off_when_validation_is_not_the_owner_mode(board, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        task = _started(conn)
        assert _spec(conn, task) == 1
