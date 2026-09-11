"""CODE_FAST_ROUTE_20260910: em owner mode, card de código segue a rota de produção sem homologação: preparação e revisão
automáticas (revisão aprova só com PR reconciliado e CI verde), merge e deploy permitidos no destino de PR de revisão."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

REPO = "https://github.com/concursa-ai/concursa.ai"
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


def _code_card(conn, tmp_path):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "13"},
        text="Corrigir a extração do edital.",
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "produção", "method": "ui", "result": "sintoma presente"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Extração correta", "criteria": [{"id": "C1", "text": "cargo extraído"}], "steps": ["Corrigir"],
        "delivery_type": "code", "size": "M",
        "delivery_destination": {"environment": "pr", "target": REPO, "source": "premissas do owner",
                                 "authorization_message": "owner 10/09", "verification_operation": "pr"}},
        author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _set_candidate(conn, task):
    wf = delivery.get_workflow(conn, task.id)
    state = json.loads(wf["state_json"] or "{}")
    state.update(candidate_sha=SHA, candidate_tree=TREE)
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(state), task.id))
    conn.commit()


def _green_pr(conn, task):
    ev = {"candidate": SHA, "tree": TREE, "ci_status": "success", "pull_request": REPO + "/pull/1", "readback": {"state": "OPEN"}}
    now = int(time.time())
    conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,evidence,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", ("e" * 64, task.id, task.current_run_id, "pr", REPO, SHA, "confirmed", json.dumps(ev), now, now))
    conn.commit()


def _own_slot(conn, task):
    conn.execute("INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)",
                 ("pilot", task.id, task.current_run_id, SHA, int(time.time())))
    conn.commit()


def test_preparation_is_automatic(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board)
        did = delivery.ask_principal(conn, task.id, task.current_run_id, kind="preparation",
                                     question="Bind the candidate to staging?", context={"preparation": {"target": REPO, "candidate": SHA}})
        d = delivery.get_decision(conn, did)
        assert d["status"] == "resolved" and d["action"] == "continue" and "automático" in d["answer"]


def test_review_waits_for_green_ci_then_approves(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board)
        _set_candidate(conn, task)
        did = delivery.ask_principal(conn, task.id, task.current_run_id, kind="review", question="Aprovar merge?", context={})
        d = delivery.get_decision(conn, did)
        assert d["status"] == "resolved" and d["action"] == "continue" and "ci_status" in d["answer"]
        _green_pr(conn, task)
        did = delivery.ask_principal(conn, task.id, task.current_run_id, kind="review", question="Aprovar merge?", context={})
        d = delivery.get_decision(conn, did)
        assert d["action"] == "approve"
        assert delivery._approved(conn, task.id, delivery.get_workflow(conn, task.id)["spec_revision"])


def test_merge_is_allowed_on_the_review_pr_route_in_owner_mode(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board)
        _set_candidate(conn, task)
        _green_pr(conn, task)
        delivery.ask_principal(conn, task.id, task.current_run_id, kind="review", question="Aprovar merge?", context={})
        _own_slot(conn, task)
        effect = delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)
        assert effect["execute"] is True


def test_merge_stays_outside_the_review_pr_route_without_owner_mode(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board)
        _set_candidate(conn, task)
        _green_pr(conn, task)
        _own_slot(conn, task)
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        with pytest.raises(delivery.WorkflowError, match="ends at the review PR|spec acceptance is pending"):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)
