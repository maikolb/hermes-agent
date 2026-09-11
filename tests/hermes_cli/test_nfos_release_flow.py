"""RELEASE_FLOW_20260911: em owner mode, PR com CI define o candidato (rebase sem nova homologação), merge não exige árvore
igual à homologada, slot só para staging/HML na resposta automática."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

REPO = "https://github.com/concursa-ai/concursa.ai"
PROD = "https://admin.concursaai.com"
SHA1, TREE1 = "a" * 40, "b" * 40   # candidato homologado
SHA2, TREE2 = "c" * 40, "d" * 40   # candidato rebaseado, com PR verde


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


def _card(conn, tmp_path):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": "16"},
        text="Corrigir extração do edital.",
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod", "method": "ui", "result": "bug"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Extração correta", "criteria": [{"id": "C1", "text": "ok"}], "steps": ["Corrigir"],
        "delivery_type": "code", "size": "M",
        "delivery_destination": {"environment": "production", "target": PROD, "source": "config",
                                 "authorization_message": "owner", "verification_operation": "deploy"}},
        author="worker", evidence={"source": "worker"})
    wf = delivery.get_workflow(conn, task.id)
    state = json.loads(wf["state_json"] or "{}")
    state.update(candidate_sha=SHA1, candidate_tree=TREE1, homolog_sha=SHA1)
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(state), task.id))
    conn.commit()
    return kb.get_task(conn, task.id)


def test_slot_answer_sends_production_effects_ahead():
    answer = delivery._auto_continue_answer("impediment", "The canonical staging slot is occupied by another task")
    assert "PR em main, merge e deploy seguem sem acquire-project" in answer


def test_green_pr_rebinds_the_candidate_and_merge_needs_no_homologated_tree(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board)
        run = task.current_run_id
        # PR do candidato rebaseado, sem slot e sem nova homologação
        effect = delivery.begin_effect(conn, task.id, run, operation="pr", target=REPO, candidate=SHA2)
        assert effect["execute"] is True
        delivery.reconcile_effect(conn, effect["id"], found=True, caller_task_id=task.id, caller_run_id=run,
                                  evidence={"readback": {"state": "OPEN"}, "candidate": SHA2, "tree": TREE2,
                                            "ci_status": "success", "pull_request": REPO + "/pull/388"})
        state = json.loads(delivery.get_workflow(conn, task.id)["state_json"])
        assert state["candidate_sha"] == SHA2 and state["candidate_tree"] == TREE2
        # revisão mecânica aprova com o PR verde
        did = delivery.ask_principal(conn, task.id, run, kind="review", question="Aprovar merge?", context={})
        assert delivery.get_decision(conn, did)["action"] == "approve"
        # merge do candidato rebaseado; árvore integrada diferente da homologada não trava
        merge = delivery.begin_effect(conn, task.id, run, operation="merge", target=REPO + "/tree/main", candidate=SHA2)
        assert merge["execute"] is True
        delivery.reconcile_effect(conn, merge["id"], found=True, caller_task_id=task.id, caller_run_id=run,
                                  evidence={"readback": {"merged": True}, "candidate": SHA2, "tree": "e" * 40, "integrated_sha": "f" * 40})
        state = json.loads(delivery.get_workflow(conn, task.id)["state_json"])
        assert state["integrated_sha"] == "f" * 40
