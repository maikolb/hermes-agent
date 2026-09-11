"""DELIVERY_ENV_20260911: ambiente de entrega por projeto define a rota; projeto hml não vai a produção sem ordem expressa;
candidato com PR verde dispensa nova homologação; o show expõe a rota."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime

REPO = "https://github.com/Project-Factory-26/next-crm"
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


def _code_card(conn, tmp_path, text, environment="pr"):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "4", "message_id": "14"},
        text=text,
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "hml", "method": "ui", "result": "sintoma presente"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Modal interno", "criteria": [{"id": "C1", "text": "sem prompt nativo"}], "steps": ["Corrigir"],
        "delivery_type": "code", "size": "P",
        "delivery_destination": {"environment": environment, "target": REPO, "source": "config do projeto",
                                 "authorization_message": "owner 11/09", "verification_operation": "pr"}},
        author="worker", evidence={"source": "worker"})
    wf = delivery.get_workflow(conn, task.id)
    state = json.loads(wf["state_json"] or "{}")
    state.update(candidate_sha=SHA, candidate_tree=TREE)
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(state), task.id))
    ev = {"candidate": SHA, "tree": TREE, "ci_status": "success", "pull_request": REPO + "/pull/56", "readback": {"state": "OPEN"}}
    now = int(time.time())
    conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,evidence,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", ("f" * 64, task.id, task.current_run_id, "pr", REPO, SHA, "confirmed", json.dumps(ev), now, now))
    conn.execute("INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)",
                 ("pilot", task.id, task.current_run_id, SHA, now))
    conn.commit()
    delivery.ask_principal(conn, task.id, task.current_run_id, kind="review", question="Aprovar?", context={})
    return kb.get_task(conn, task.id)


def test_routes_follow_the_project_environment():
    assert "staging branch staging" in runtime.delivery_route({"delivery_environment": "production", "staging_branch": "staging"})["route"]
    assert runtime.delivery_route({"delivery_environment": "production"})["route"].startswith("Branch from main; PR to main")
    hml = runtime.delivery_route({"delivery_environment": "hml", "staging_branch": "staging"})
    assert hml["environment"] == "hml" and "Delivered in HML is delivered" in hml["route"]
    assert runtime.delivery_route({"delivery_environment": "dev"})["route"].startswith("Deliver on the dev environment")
    assert runtime.delivery_route({})["environment"] == "production"


def test_express_production_order_is_an_imperative_not_a_mention():
    assert not delivery._express_production_order("Ambiente observado: produção, tenant sanitizado. Corrigir o modal.")
    assert delivery._express_production_order("Depois de validar no HML, suba para produção hoje.")
    assert delivery._express_production_order("Pode mergear em main e publicar em PRD.")


def test_hml_project_refuses_production_merge_without_order(board, monkeypatch):
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"delivery_environment": "hml", "staging_branch": "staging", "enabled": True, "board": board})
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Criar produto abre diálogo nativo. Ambiente observado: produção.")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)


def test_hml_project_allows_production_with_the_owner_order(board, monkeypatch):
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"delivery_environment": "hml", "staging_branch": "staging", "enabled": True, "board": board})
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir o modal e depois subir para produção (ordem do Maikol).")
        effect = delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)
        assert effect["execute"] is True


def test_green_pr_candidate_needs_no_new_homologation(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir extração.")
        wf = delivery.get_workflow(conn, task.id)
        state = json.loads(wf["state_json"])
        state["homolog_sha"] = "c" * 40  # homologado antes do rebase; candidato atual difere
        assert delivery._delivery_candidate(conn, task.id, state) == SHA
