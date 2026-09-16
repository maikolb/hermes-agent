"""Owner destination guidance reaches the effect guard without rewriting the card."""
import json

import pytest

from tests.hermes_cli.test_nfos_delivery_environment import (
    board, _code_card, kb, delivery, runtime, review, REPO, SHA,
)


def guidance(conn, task, text="Esse é direto pra produção. Esse é urgente!", message="owner-1"):
    result = delivery.receive_owner_guidance(conn, task.id, text=text,
        source={"actor": "Maikol", "message_id": message, "platform": "vigilia"})
    delivery.resolve_decision(conn, result["decision_id"], action="continue",
        answer="Prosseguir no destino autorizado pelo proprietário.", author="Principal")
    return result["decision_id"]


@pytest.fixture(autouse=True)
def hml_project(monkeypatch):
    monkeypatch.setattr(runtime, "project_config", lambda *a, **k: {
        "delivery_environment": "hml", "enabled": True, "board": "pilot"})


def effect(conn, task):
    return delivery.begin_effect(conn, task.id, task.current_run_id,
        operation="merge", target=REPO + "/tree/main", candidate=SHA)


def test_actual_owner_phrase_authorizes_without_body_or_spec_rewrite(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Implementar concessão administrativa.")
        revision = delivery.get_workflow(conn, task.id)["spec_revision"]
        guidance(conn, task)
        assert effect(conn, task)["execute"]
        assert kb.get_task(conn, task.id).body == task.body
        assert kb.get_task(conn, task.id).instruction_revision == task.instruction_revision
        assert delivery.get_workflow(conn, task.id)["spec_revision"] == revision


@pytest.mark.parametrize("text", ["Não colocar em produção", "só HML",
    "Não publicar em produção", "Ambiente observado: produção.",
    "Não é direto pra produção", "Só HML agora; publicar em produção depois"])
def test_restrictions_and_mentions_do_not_authorize(board, text):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        guidance(conn, task, text)
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            effect(conn, task)


@pytest.mark.parametrize("case", ["stale", "hml_scope", "superseded", "forged", "pending", "target_changed", "source_changed", "comment_changed"])
def test_guidance_is_bound_to_current_owner_scope(board, case):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.", environment="hml" if case == "hml_scope" else "production")
        decision_id = guidance(conn, task)
        if case == "stale":
            conn.execute("UPDATE tasks SET instruction_revision=instruction_revision+1 WHERE id=?", (task.id,))
        elif case == "superseded":
            guidance(conn, task, "Somente HML", "owner-2")
        elif case == "forged":
            conn.execute("DELETE FROM task_events WHERE kind='nfos_principal_requested' AND json_extract(payload,'$.decision_id')=?", (decision_id,))
        elif case == "pending":
            conn.execute("UPDATE nfos_decisions SET status='pending' WHERE id=?", (decision_id,))
        elif case == "source_changed":
            conn.execute("UPDATE nfos_decisions SET context=json_set(context,'$.source.message_id','forged') WHERE id=?", (decision_id,))
        elif case == "comment_changed":
            conn.execute("UPDATE task_comments SET body='Unrelated comment' WHERE task_id=?", (task.id,))
        elif case == "target_changed":
            spec = delivery.get_spec(conn, task.id)
            content = json.loads(spec["content"])
            content["delivery_destination"]["target"] = "https://github.com/other/repo"
            conn.execute("UPDATE nfos_artifacts SET content=? WHERE id=?", (json.dumps(content), spec["id"]))
        conn.commit()
        with pytest.raises(delivery.WorkflowError):
            effect(conn, task)


@pytest.mark.parametrize("stale", [False, True])
def test_preexisting_authentic_receipt_uses_instruction_event_order(board, stale):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        decision_id = guidance(conn, task)
        conn.execute("UPDATE nfos_decisions SET context=json_remove(context,'$.received_instruction_revision','$.received_destination') WHERE id=?", (decision_id,))
        if stale:
            kb.update_task_instruction(conn, task.id, body="Outro escopo", author="Maikol", expected_revision=task.instruction_revision)
        conn.commit()
        if stale:
            with pytest.raises(delivery.WorkflowError):
                effect(conn, task)
        else:
            assert effect(conn, task)["execute"]


@pytest.mark.parametrize("referenced", [True, False])
@pytest.mark.parametrize('text', ['Esse é direto pra produção. Esse é urgente!',
    'Não é somente em hml, esse é direto em prod', 'Pode atender no ambiente que combinamos.'])
def test_owner_changes_hml_destination_then_current_spec_binds_the_message(board, referenced, monkeypatch, text):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.", environment="hml")
        guidance(conn, task, text, "owner-destination-change")
        content = json.loads(delivery.get_spec(conn, task.id)["content"])
        content["delivery_destination"].update(environment="production",
            source="Vigília message_id=owner-destination-change" if referenced else "worker",
            authorization_message=text)
        monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
        delivery.save_spec(conn, task.id, task.current_run_id, content,
            author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="spec acceptance is pending"):
            effect(conn, task)
        decision_id = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind="spec_review", question="Review authorized production destination", context={})
        delivery.resolve_decision(conn, decision_id, action="continue", answer="Current scope reviewed",
            author="Principal", assessment={"request_alignment": "Owner production message preserved",
                "scope_assessment": "Same correction at the owner's requested destination",
                "criteria": [{"id": "C1", "verdict": "accept", "observation": "Modal correction and authorized destination covered"}]})
        if referenced:
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
        else:
            with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
                effect(conn, task)


def test_positive_production_order_can_reject_hml(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        guidance(conn, task, "Publique em produção, não em HML.")
        assert effect(conn, task)["execute"]


def test_unrelated_guidance_preserves_previous_destination_order(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        guidance(conn, task)
        guidance(conn, task, "Mantenha as contas existentes", "owner-2")
        assert effect(conn, task)["execute"]


def test_later_hml_restriction_overrides_original_body_production_order(board):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir e publicar em produção.")
        guidance(conn, task, "Não publique em produção, somente HML.")
        with pytest.raises(delivery.WorkflowError, match="delivers in HML"):
            effect(conn, task)


def test_owner_destination_arrives_before_first_spec(board, monkeypatch):
    save_spec = delivery.save_spec
    text = "Esse é direto pra produção. Esse é urgente!"

    def receive_before_save(conn, task_id, run_id, content, **kwargs):
        task = kb.get_task(conn, task_id)
        guidance(conn, task, text, "before-first-spec")
        content["delivery_destination"].update(source="Vigília before-first-spec", authorization_message=text)
        return save_spec(conn, task_id, run_id, content, **kwargs)

    monkeypatch.setattr(delivery, "save_spec", receive_before_save)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, "Corrigir licença.")
        assert effect(conn, task)["execute"]


@pytest.mark.parametrize('text', ['Não é somente em hml, esse é direto em prod',
    'Não é somente em hml, esse é direto em produção', 'Publique em prod'])
def test_authenticated_prod_abbreviation_authorizes(board, text):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, 'Corrigir recuperação de senha.')
        guidance(conn, task, text)
        assert effect(conn, task)['execute']


@pytest.mark.parametrize('restriction', ['Não publique em prod', 'Somente HML'])
def test_later_restriction_revokes_prod_authorization(board, restriction):
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, 'Corrigir recuperação de senha.')
        guidance(conn, task, 'Não é somente em hml, esse é direto em prod')
        guidance(conn, task, restriction, 'owner-2')
        with pytest.raises(delivery.WorkflowError, match='delivers in HML'):
            effect(conn, task)
