"""BLOCK_LESS9_20260910: o tamanho da spec (P/M/G) define o orçamento do run e a classe no board."""
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
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "7"},
        text="Aumentar a conta do tenant.",
        project={"board": "pilot", "profile": "default", "delivery_type": "operation"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod db", "method": "db", "result": "plan=free"}], "verdict": "not_delivered"})
    return task


def _spec(conn, task, **extra):
    spec = {"goal": "Plano scale", "criteria": [{"id": "C1", "text": "plan=scale"}], "steps": ["Atualizar"], "delivery_type": "operation"}
    spec.update(extra)
    return delivery.save_spec(conn, task.id, task.current_run_id, spec, author="worker", evidence={"source": "worker"})


def test_size_sets_the_run_budget(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        assert _spec(conn, task, size="P") == 1
        assert kb.get_task(conn, task.id).max_runtime_seconds == 2700
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (task.id,))]
        assert "nfos_spec_size" in kinds


def test_new_spec_without_size_is_refused_in_owner_mode(board):
    with kb.connect_closing() as conn:
        task = _started(conn)
        with pytest.raises(delivery.WorkflowError, match="size P, M or G"):
            _spec(conn, task)


def test_size_is_optional_outside_owner_mode(board, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        task = _started(conn)
        assert _spec(conn, task) == 1
        assert _spec(conn, task, size="g") == 2
        assert kb.get_task(conn, task.id).max_runtime_seconds == 14400
