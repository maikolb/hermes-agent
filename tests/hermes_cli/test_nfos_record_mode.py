"""RECORD_MODE_20260911: em owner mode o runtime registra e não conduz: efeito sem candidato aprovado, sem slot e sem ordem;
identidade preservada (SHA exato); slot vira registro; premissas únicas; juiz de modelo desligado; fechamento grava o ambiente."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime

REPO = "https://github.com/concursa-ai/concursa.ai"
PROD = "https://admin.concursaai.com"
SHA = "a" * 40


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


def _card(conn, tmp_path, n=1):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(30 + n)},
        text=f"Corrigir item {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod", "method": "ui", "result": "bug"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Corrigir", "criteria": [{"id": "C1", "text": "ok"}], "steps": ["Corrigir"], "delivery_type": "code", "size": "P",
        "delivery_destination": {"environment": "production", "target": PROD, "source": "config",
                                 "authorization_message": "owner", "verification_operation": "deploy"}},
        author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def test_merge_is_recorded_without_approval_slot_or_homologation(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board)
        effect = delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)
        assert effect["execute"] is True
        row = conn.execute("SELECT operation, target, candidate, status FROM nfos_effects WHERE task_id=?", (task.id,)).fetchone()
        assert (row[0], row[1], row[2], row[3]) == ("merge", REPO + "/tree/main", SHA, "unknown")


def test_candidate_identity_is_still_required(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board)
        with pytest.raises(delivery.WorkflowError, match="exact commit SHA"):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="pr", target=REPO, candidate="latest")


def test_conducted_checks_still_apply_outside_owner_mode(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, board)
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        with pytest.raises(delivery.WorkflowError):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)


def test_slot_is_a_record_not_a_lock(board):
    with kb.connect_closing() as conn:
        holder = _card(conn, board, 1)
        other = _card(conn, board, 2)
        assert delivery.acquire_project(conn, "pilot", holder.id, holder.current_run_id, SHA) is True
        assert delivery.acquire_project(conn, "pilot", other.id, other.current_run_id, "b" * 40) is True
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (holder.id,))]
        assert "nfos_project_delivery_shared" in kinds


def test_premises_are_one_short_list():
    items = runtime.owner_premises()
    assert len(items) == 7
    assert any("delivery_environment" in p for p in items)
    assert any("Budgets are real" in p for p in items)
    assert "record mode" in runtime._premises_prefix()


def test_judge_is_off_in_owner_mode(board):
    from tools import kanban_tools as kt
    with kb.connect_closing() as conn:
        task = _card(conn, board)
    assert kt._goal_mode_handoff_rejection(task, "Entregue em produção com readback.") is None
