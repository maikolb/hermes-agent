"""INTENT_DESTINATION_20260923: owner guidance never authorizes production by its wording; the destination is the
owner's intent recorded in the spec and accepted by the Principal, and new guidance holds production until judged."""
import json

import pytest

from tests.hermes_cli.test_nfos_delivery_environment import (
    board, _code_card, kb, delivery, runtime, review, REPO, SHA,
)


def guidance(conn, task, text="Esse é direto pra produção. Esse é urgente!", message="owner-1", action="continue"):
    result = delivery.receive_owner_guidance(conn, task.id, text=text,
        source={"actor": "Maikol", "message_id": message, "platform": "vigilia"})
    if action:
        delivery.resolve_decision(conn, result["decision_id"], action=action,
            answer="Julgado pelo Principal.", author="Principal")
    return result["decision_id"]


@pytest.fixture(autouse=True)
def hml_project(monkeypatch):
    monkeypatch.setattr(runtime, "project_config", lambda *a, **k: {
        "delivery_environment": "hml", "enabled": True, "board": "pilot"})


def effect(conn, task):
    return delivery.begin_effect(conn, task.id, task.current_run_id,
        operation="merge", target=REPO + "/tree/main", candidate=SHA)


def revise(conn, task, environment, text, message):
    content = json.loads(delivery.get_spec(conn, task.id)["content"])
    content["delivery_destination"].update(environment=environment,
        source="Vigília message_id=" + message, authorization_message=text)
    delivery.save_spec(conn, task.id, task.current_run_id, content, author="worker", evidence={"source": "worker"})


@pytest.mark.parametrize("text", ["Esse é direto pra produção. Esse é urgente!", "Publique em prod", "Pode subir"])
def test_guidance_wording_alone_never_authorizes_production(board, text):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.", environment="hml")
        guidance(conn, task, text)
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            effect(conn, task)


def test_guidance_does_not_rewrite_the_card_or_the_spec(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Implementar concessão administrativa.")
        revision = delivery.get_workflow(conn, task.id)["spec_revision"]
        guidance(conn, task, "Mantenha as contas existentes")
        assert effect(conn, task)["execute"]
        assert kb.get_task(conn, task.id).body == task.body
        assert kb.get_task(conn, task.id).instruction_revision == task.instruction_revision
        assert delivery.get_workflow(conn, task.id)["spec_revision"] == revision


@pytest.mark.parametrize("action", [None, "changes"])
def test_later_guidance_holds_an_accepted_production_destination(board, action):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir e publicar em produção.")
        guidance(conn, task, "Somente HML por enquanto", action=action)
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            effect(conn, task)


def test_revised_spec_carries_the_new_destination(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir e publicar em produção.")
        guidance(conn, task, "Somente HML por enquanto", action="changes")
        revise(conn, task, "hml", "Somente HML por enquanto", "owner-1")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            effect(conn, task)
        guidance(conn, task, "Agora pode ir pra produção", "owner-2", action="changes")
        revise(conn, task, "production", "Agora pode ir pra produção", "owner-2")
        assert effect(conn, task)["execute"]


def test_owner_destination_arrives_before_first_spec(board, monkeypatch):
    save_spec = delivery.save_spec
    text = "Esse é direto pra produção. Esse é urgente!"

    def receive_before_save(conn, task_id, run_id, content, **kwargs):
        task = kb.get_task(conn, task_id)
        guidance(conn, task, text, "before-first-spec", action="changes")
        content["delivery_destination"].update(source="Vigília before-first-spec", authorization_message=text)
        return save_spec(conn, task_id, run_id, content, **kwargs)

    monkeypatch.setattr(delivery, "save_spec", receive_before_save)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        assert effect(conn, task)["execute"]


def test_owner_changes_hml_destination_then_principal_accepts_the_revised_spec(board, monkeypatch):
    text = "Esse é urgente, vai direto pra produção"
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.", environment="hml")
        guidance(conn, task, text, "owner-destination-change", action="changes")
        monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
        revise(conn, task, "production", text, "owner-destination-change")
        with pytest.raises(delivery.WorkflowError, match="spec acceptance is pending"):
            effect(conn, task)
        decision_id = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind="spec_review", question="Review owner production destination", context={})
        delivery.resolve_decision(conn, decision_id, action="continue", answer="Current scope reviewed",
            author="Principal", assessment={"request_alignment": "Owner wants production now",
                "scope_assessment": "Same correction at the owner's requested destination",
                "criteria": [{"id": "C1", "verdict": "accept", "observation": "Modal correction and authorized destination covered"}]})
        with pytest.raises(delivery.WorkflowError, match="review of this candidate is pending"):
            effect(conn, task)
        state = json.loads(delivery.get_workflow(conn, task.id)["state_json"])
        state.update(homolog_sha=SHA, homolog_evidence={"fixture": "Synthetic HML result"})
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(state), task.id))
        conn.commit()
        candidate_review = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind="review", question="Review synthetic candidate", context={})
        delivery.resolve_decision(conn, candidate_review, action="approve",
            answer="Synthetic candidate reviewed", author="Principal")
        assert effect(conn, task)["execute"]
