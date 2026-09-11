"""SLOT_HML_ONLY_20260911: em owner mode o slot de publicação do projeto só vale para staging/HML; pr, merge e deploy não esperam."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

REPO = "https://github.com/concursa-ai/concursa.ai"
HML = "https://hml.example.com"
SHA = "a" * 40
TREE = "b" * 40


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


def _card(conn, tmp_path, destination):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": "15"},
        text="Corrigir extração.",
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod", "method": "ui", "result": "bug"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Extração correta", "criteria": [{"id": "C1", "text": "ok"}], "steps": ["Corrigir"],
        "delivery_type": "code", "size": "P", "delivery_destination": destination},
        author="worker", evidence={"source": "worker"})
    wf = delivery.get_workflow(conn, task.id)
    state = json.loads(wf["state_json"] or "{}")
    state.update(candidate_sha=SHA, candidate_tree=TREE)
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(state), task.id))
    conn.commit()
    return kb.get_task(conn, task.id)


PR_ROUTE = {"environment": "pr", "target": REPO, "source": "config", "authorization_message": "owner", "verification_operation": "pr"}
HML_ROUTE = {"environment": "hml", "target": HML, "source": "config", "authorization_message": "owner", "verification_operation": "homolog"}


def test_production_pr_needs_no_publication_slot_in_owner_mode(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board, PR_ROUTE)
        effect = delivery.begin_effect(conn, task.id, task.current_run_id, operation="pr", target=REPO, candidate=SHA)
        assert effect["execute"] is True


def test_hml_publication_still_takes_the_slot(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board, HML_ROUTE)
        with pytest.raises(delivery.WorkflowError, match="publication slot"):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="homolog", target=HML, candidate=SHA)


def test_slot_still_required_outside_owner_mode(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, board, PR_ROUTE)
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        with pytest.raises(delivery.WorkflowError):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="pr", target=REPO, candidate=SHA)
