"""DELIVERY_ENV_20260911: ambiente de entrega por projeto define a rota; projeto hml vai a produção pelo destino da spec (INTENT_DESTINATION_20260923);
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


def _code_card(conn, tmp_path, text, environment="production"):
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
        "goal": "Modal interno", "criteria": [{"id": "C1", "text": "sem prompt nativo", "mandatory": True,
            "probe": {"kind": "sql", "query": "SELECT uses_native_prompt FROM modal_checks", "expect": {"scalar": 0}}}], "steps": ["Corrigir"],
        "delivery_type": "code", "size": "P",
        "delivery_destination": {"environment": environment, "target": REPO, "source": "config do projeto",
                                 "authorization_message": "owner 11/09", "verification_operation": "pr" if environment == "pr" else "deploy"}},
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


def test_green_pr_candidate_needs_no_new_homologation(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir extração.")
        wf = delivery.get_workflow(conn, task.id)
        state = json.loads(wf["state_json"])
        state["homolog_sha"] = "c" * 40  # homologado antes do rebase; candidato atual difere
        assert delivery._delivery_candidate(conn, task.id, state) == SHA


# INTENT_DESTINATION_20260923 (ordem do Maikol: "Vcs tem que entender pela intenção"): o destino é a intenção do dono,
# lida pelo worker na spec e aceita pelo Principal. O runtime não interpreta texto: nenhuma frase autoriza nem bloqueia
# produção sozinha; a orientação do dono só segura produção até o Principal julgá-la.
HML_PROJECT = {"delivery_environment": "hml", "staging_branch": "staging", "enabled": True}


def _hml(monkeypatch):
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: dict(HML_PROJECT, board=board))


def _merge(conn, task):
    return delivery.begin_effect(conn, task.id, task.current_run_id, operation="merge", target=REPO + "/tree/main", candidate=SHA)


def _guidance(conn, task, text, message, action=None):
    receipt = delivery.receive_owner_guidance(conn, task.id, text=text,
        source={"platform": "portal", "actor": "Maikol", "message_id": message})
    if action:
        delivery.resolve_decision(conn, receipt["decision_id"], action=action, answer="Julgado pelo Principal", author="Principal")
    return receipt["decision_id"]


def _revise_destination(conn, task, environment, message):
    content = json.loads(delivery.get_spec(conn, task.id)["content"])
    content["delivery_destination"].update(environment=environment, source="portal message_id=" + message,
                                           authorization_message="orientação " + message)
    delivery.save_spec(conn, task.id, task.current_run_id, content, author="worker", evidence={"source": "worker"})


def test_hml_project_goes_to_production_when_the_spec_destination_is_production(board, monkeypatch):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\n#deepseek veja e arrume urgente", environment="production")
        assert _merge(conn, task)["execute"] is True


@pytest.mark.parametrize("text", [
    "[Maikol|996979567]\nsuba para produção",
    "[Maikol|996979567]\n#deepseek arruma isso urgente em produção",
    "Corrigir o modal e depois subir para produção (ordem do Maikol).",
])
def test_card_text_never_authorizes_production_by_itself(board, monkeypatch, text):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, text, environment="hml")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            _merge(conn, task)


def test_pending_owner_guidance_holds_production_until_the_principal_judges_it(board, monkeypatch):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\nurgente", environment="production")
        _guidance(conn, task, "Espera, deixa eu ver uma coisa antes", "owner-1")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            _merge(conn, task)


def test_guidance_the_principal_keeps_does_not_change_the_destination(board, monkeypatch):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\nurgente", environment="production")
        _guidance(conn, task, "Mantenha as contas existentes", "owner-1", action="continue")
        assert _merge(conn, task)["execute"] is True


@pytest.mark.parametrize("new_environment,allowed", [("hml", False), ("production", True)])
def test_guidance_that_changes_the_destination_needs_a_new_spec(board, monkeypatch, new_environment, allowed):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\nurgente", environment="production")
        _guidance(conn, task, "Mudei de ideia sobre o destino", "owner-1", action="changes")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            _merge(conn, task)
        _revise_destination(conn, task, new_environment, "owner-1")
        if allowed:
            assert _merge(conn, task)["execute"] is True
        else:
            with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
                _merge(conn, task)


def test_guidance_before_the_current_spec_was_already_absorbed(board, monkeypatch):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\narrume o login", environment="hml")
        _guidance(conn, task, "Esse é urgente, vai direto pra produção", "owner-1", action="changes")
        _revise_destination(conn, task, "production", "owner-1")
        assert _merge(conn, task)["execute"] is True


def test_production_destination_waits_for_the_principal_when_review_is_on(board, monkeypatch):
    _hml(monkeypatch)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "[Maikol|996979567]\nurgente", environment="hml")
        monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
        _revise_destination(conn, task, "production", "owner-1")
        with pytest.raises(delivery.WorkflowError):
            _merge(conn, task)
        assert not delivery._owner_production_destination(conn, kb.get_task(conn, task.id),
                                                          json.loads(delivery.get_spec(conn, task.id)["content"])["delivery_destination"])


def test_routes_follow_the_recorded_destination_not_the_text():
    production = {"environment": "production", "target": REPO, "verification_operation": "deploy"}
    for env in ("hml", "dev", "test"):
        route = runtime.delivery_route({"delivery_environment": env, "staging_branch": "staging"}, destination=production)
        assert route["environment"] == "production" and route["route"].startswith("Owner production request")
        assert "only this card's change" in route["route"] and "Do not promote the staging branch" in route["route"]
    assert runtime.delivery_route(HML_PROJECT, destination={"environment": "hml"})["environment"] == "hml"
    assert runtime.delivery_route(HML_PROJECT)["environment"] == "hml"
    prod_project = {"delivery_environment": "production", "staging_branch": "staging"}
    assert runtime.delivery_route(prod_project, destination=production) == runtime.delivery_route(prod_project)


def test_show_closing_judge_and_delivery_record_follow_the_recorded_destination(tmp_path, monkeypatch):
    from tools import kanban_tools
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: dict(HML_PROJECT, board=board))
    monkeypatch.setattr(kb, "get_current_board", lambda: "dovcrm")
    db = tmp_path / "dovcrm" / "kanban.db"
    production = {"environment": "production", "target": REPO, "verification_operation": "deploy"}
    assert delivery._delivery_environment_for_db(db, destination=production)["environment"] == "production"
    assert delivery._delivery_environment_for_db(db, destination={"environment": "hml"})["environment"] == "hml"
    assert "PRODUCTION" in kanban_tools._delivery_environment_note(production)
    assert "HML" in kanban_tools._delivery_environment_note({"environment": "hml"})


def test_no_keyword_parser_remains():
    import inspect
    source = inspect.getsource(delivery)
    for name in ("_PRODUCTION_ORDER_RX", "_OWNER_PRODUCTION_RX", "_URGENT_RX", "_express_production_order",
                 "_production_guidance_intent", "_owner_urgent_production"):
        assert name not in source
