"""Free text reaches the existing Principal instead of a keyword decision."""
import pytest
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept, assessment, save_report
from hermes_cli import nfos_delivery as d, nfos_principal_review as review


@pytest.mark.parametrize('question', [
    'slot está livre, mas efeito recusou destino',
    'DATABASE_URL está configurada, mas não temos autorização para publicar',
    'A cota não acabou; o readback mostra o tenant errado',
])
def test_actual_ask_keeps_unresolved_meaning_for_principal(task_context, question):
    conn, task, _, _ = task_context
    decision = d.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question=question, context={})
    assert d.get_decision(conn, decision)['status'] == 'pending'
    conn.execute('UPDATE nfos_decisions SET created_at=1 WHERE id=?', (decision,)); conn.commit()
    d.nudge_open_decisions(conn, task.id)
    assert d.get_decision(conn, decision)['status'] == 'pending'


def test_human_question_is_not_vetoed_by_credential_words(task_context):
    conn, task, _, _ = task_context
    decision = d.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Owner alone can authorize a new paid access.', context={})
    d.resolve_decision(conn, decision, action='human', author='Principal',
        answer='PERGUNTA para Maikol: autoriza pagar pela nova credencial da sonda? Acesso existente comprovadamente insuficiente.')
    assert d.get_decision(conn, decision)['status'] == 'human'


def test_valid_observation_needs_no_human_confirmation(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False, 'result_review': True})
    accept(conn, task, 'spec_review'); save_report(conn, task, artifact)
    decision = d.ask_principal(conn, task.id, task.current_run_id,
        kind='final_review', question='Review usable result with historical limitation', context={})
    reviewed = assessment(artifact)
    reviewed['resolution'] = 'Count delivered'
    reviewed['criteria'][0].update(
        verdict='observe', observation='Result verified; historical timestamp unavailable')
    d.resolve_decision(conn, decision, action='continue', author='Principal',
        answer='Result accepted with observation', assessment=reviewed)
    assert d.get_decision(conn, decision)['status'] == 'resolved'
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND status='human'", (task.id,)).fetchone()


def test_maintenance_assessment_is_optional_and_not_inferred_from_words():
    assert d._human_is_maintenance({'failure':{'kind':'runtime_maintenance'}})
    assert not d._human_is_maintenance({'failure':'slot'})
    assert not d._human_is_maintenance(None)


def test_record_mode_prompt_keeps_routine_work_autonomous(monkeypatch):
    from hermes_cli import nfos_runtime as runtime
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation':False,'result_review':True})
    prompt = runtime.worker_instructions()
    assert 'Handle slots, local hooks, readbacks and already authorized credentials' in prompt
    assert 'without asking for each step' in ' '.join(prompt.split())
    assert 'A valid final observation needs no owner approval' in prompt
