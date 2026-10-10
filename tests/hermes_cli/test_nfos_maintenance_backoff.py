"""MAINTENANCE_BACKOFF_20261009: reparo de pausa que o Principal não alcança não o acorda a cada 15 min.

Em 09/10/2026 cinco pausas do Concursa (preparo do laboratório que o broker não fazia, portão do processo) pediam "Execute o
reparo desta pausa". Cada resposta sem reparo voltava a pendente e o lembrete seguia a cada 15 min por card: de 40 a 70% dos
pedidos ao Principal em três horas, com 31 respostas devolvidas. Agora o lembrete dobra a cada resposta devolvida e o
Principal pode declarar de quem o reparo depende: a pausa espera sem lembrete e volta para ele reconferir no prazo."""
import json
import sys
import time

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_runtime as runtime
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_maintenance_pause import _paused_and_exited, running  # noqa: F401
from tests.hermes_cli.test_nfos_principal_acceptance import assessment, task_context  # noqa: F401

DEPENDENCY = {'owner': 'laboratório (Codex)', 'need': 'importar o PDF do caso de produção para o corpus', 'recheck_hours': 6}
ANSWER = 'O caso de produção não está no laboratório; o preparo depende de capacidade nova do broker.'


def _obligation(conn, task):
    d.sweep_awaiting_principal(conn)
    return conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending' "
                        "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?", (task.id, task.current_run_id)).fetchone()[0]


def _requests(conn, task, did):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'", (task.id,)).fetchall()
    return [p for p in (json.loads(r[0]) for r in rows) if p.get('decision_id') == did]


def _context(conn, did):
    return json.loads(d.get_decision(conn, did)['context'])


def _resume(conn, task):
    request = dict(pause_run_id=task.current_run_id, actor='Principal', reason='Capacidade entregue', evidence=['recibo real do preparo'])
    preview = repair.resume_after_repair(conn, task.id, **request)
    repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)


def test_reminders_of_an_unrepaired_pause_back_off_with_each_answer(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    question = d.get_decision(conn, did)['question']
    assert '"dependency"' in question, 'a obrigação diz como declarar a dependência'
    assert 'Quem entrega não é você nem a engenharia deste projeto' in question and 'fecha como entrega parcial, com o resto obrigatório no card de continuação' in question
    assert question.index('Siga o que as instruções do projeto mandam para esse caso') < question.index('fecha como entrega parcial')
    gap = d.DECISION_REMINDER_GAP
    clock = [time.time()]
    monkeypatch.setattr(d.time, 'time', lambda: clock[0])
    for answers, wait in ((1, 2 * gap), (2, 4 * gap), (3, 8 * gap), (4, 8 * gap)):
        d.resolve_decision(conn, did, action='continue', answer=f'Explicação {answers}: o preparo segue indisponível', author='Principal')
        assert d.get_decision(conn, did)['status'] == 'pending', 'a obrigação não fica órfã'
        assert _context(conn, did)['maintenance_deferrals'] == answers
        before = len(_requests(conn, task, did))
        clock[0] += wait - 5
        runtime.reconcile_runtime(conn)
        assert len(_requests(conn, task, did)) == before, f'sem lembrete antes de {wait // 60} min depois de {answers} respostas'
        clock[0] += 10
        runtime.reconcile_runtime(conn)
        runtime.reconcile_runtime(conn)
        assert len(_requests(conn, task, did)) == before + 1, f'um lembrete aos {wait // 60} min'
    assert d.MAINTENANCE_REMINDER_MAX_GAP == 8 * gap


def test_declared_dependency_holds_the_pause_without_reminders_until_its_recheck(running, monkeypatch):
    from agent import kanban_stop
    from gateway.wake import current_notify_receipt
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    start = int(time.time())
    clock = [start]
    monkeypatch.setattr(d.time, 'time', lambda: clock[0])
    d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=DEPENDENCY)
    row, context = d.get_decision(conn, did), _context(conn, did)
    assert row['status'] == 'pending' and context['maintenance_recovery']['pause_run_id'] == task.current_run_id
    wait = context['lab_wait']
    assert wait['kind'] == 'dependency' and wait['hold'] is True and wait['until'] == start + 6 * 3600
    assert wait['owner'] == DEPENDENCY['owner'] and wait['need'] == DEPENDENCY['need']
    assert did not in [x['id'] for x in d.pending_decisions(conn)], 'fora da fila do Principal enquanto espera'
    action = d.get_workflow(conn, task.id)['next_action']
    assert DEPENDENCY['need'] in action and DEPENDENCY['owner'] in action
    declared = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_maintenance_dependency_declared'", (task.id,))]
    assert len(declared) == 1 and declared[0]['until'] == start + 6 * 3600
    before = len(_requests(conn, task, did))
    clock[0] = start + 5 * 3600
    runtime.reconcile_runtime(conn)
    runtime.reconcile_runtime(conn)
    assert len(_requests(conn, task, did)) == before, 'cinco horas sem um pedido ao Principal'
    assert repair.maintenance_pause_pending(conn, task.id) and kb.get_task(conn, task.id).status == 'blocked'
    token = current_notify_receipt.set({'db_path': conn.execute('PRAGMA database_list').fetchone()[2],
                                        'principal_task_id': task.id, 'delivery_id': 'wake-dependencia'})
    try:
        assert kanban_stop._principal_continuation(None) is None, 'a espera não segura o turno do Principal'
    finally:
        current_notify_receipt.reset(token)
    clock[0] = start + 6 * 3600 + 1
    runtime.reconcile_runtime(conn)
    runtime.reconcile_runtime(conn)
    assert len(_requests(conn, task, did)) == before + 1, 'no prazo, volta ao Principal uma vez'
    recheck = _requests(conn, task, did)[-1]['question']
    assert recheck.startswith('Reconferência de dependência declarada em '), 'o pedido abre com a dependência, não com o reparo'
    # RECHECK_WITHOUT_SIX_20261010: a orientação vem antes da necessidade e cabe no corte de 500 caracteres do aviso.
    assert 'Confira por fato' in recheck[:120] and 'não informe recheck_hours: sem ele a pausa volta em 1 h' in recheck[:330]
    assert DEPENDENCY['need'] in recheck[:500] and DEPENDENCY['owner'] in recheck[:500]
    assert recheck.index('não informe recheck_hours') < recheck.index(DEPENDENCY['need'])
    assert recheck.endswith(d.get_decision(conn, did)['question']), 'a pergunta gravada segue inteira depois da abertura'
    assert not d.get_decision(conn, did)['question'].startswith('Reconferência'), 'a pergunta gravada não muda'
    assert _context(conn, did)['lab_wait']['hold'] is False
    assert did in [x['id'] for x in d.pending_decisions(conn)]
    clock[0] += 60
    runtime.reconcile_runtime(conn)
    assert len(_requests(conn, task, did)) == before + 1, 'o lembrete seguinte respeita o recuo'
    # Ainda sem a capacidade: declara de novo e a pausa volta a esperar.
    d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=dict(DEPENDENCY, recheck_hours=1))
    assert _context(conn, did)['lab_wait']['hold'] is True and _context(conn, did)['lab_wait']['until'] == clock[0] + 3600
    # O reparo real fecha a obrigação e o card volta à fila.
    _resume(conn, task)
    assert d.sweep_awaiting_principal(conn) == [task.id]
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)
    assert kb.claim_task(conn, task.id).id == task.id


@pytest.mark.parametrize('dependency', [
    {'owner': 'laboratório'}, {'owner': ' ', 'need': 'capacidade'}, {'owner': 'laboratório', 'need': 'capacidade', 'recheck_hours': 0},
    {'owner': 'laboratório', 'need': 'capacidade', 'recheck_hours': 48}, {'owner': 'laboratório', 'need': 'capacidade', 'recheck_hours': '6'},
    'laboratório'])
def test_invalid_dependency_is_refused_and_the_obligation_stays_as_it_was(running, monkeypatch, dependency):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    saved = dict(d.get_decision(conn, did))
    with pytest.raises(d.WorkflowError, match='dependency'):
        d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=dependency)
    assert dict(d.get_decision(conn, did)) == saved


def test_dependency_is_only_for_the_repair_of_an_open_pause(running, monkeypatch):
    conn, task, artifact, process, args = running
    asked = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Qual caminho seguir?', context={})
    with pytest.raises(d.WorkflowError, match='maintenance pause'):
        d.resolve_decision(conn, asked, action='continue', answer='Siga pelo caminho curto.', author='Principal', dependency=DEPENDENCY)
    assert d.get_decision(conn, asked)['status'] == 'pending', 'nada foi gravado'
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    _resume(conn, task)
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=DEPENDENCY)


def test_lab_pause_that_already_waits_takes_no_dependency(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, dict(args, resume_when='lab_available'), monkeypatch)
    did = _obligation(conn, task)
    assert _context(conn, did)['lab_wait']['kind'] == 'transport'
    with pytest.raises(d.WorkflowError, match='already waits'):
        d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=DEPENDENCY)
    assert _context(conn, did)['lab_wait']['kind'] == 'transport' and 'maintenance_deferrals' not in _context(conn, did)


def test_native_cli_declares_the_dependency(running, monkeypatch, capsys):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    answer = artifact.parent / 'dependencia.json'
    answer.write_text(json.dumps({'answer': ANSWER, 'dependency': DEPENDENCY}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['nfos', 'decide', '--decision', did, '--resolution', 'continue', '--input', str(answer)])
    d.main()
    result = json.loads(capsys.readouterr().out)
    assert result['saved'] and result['decision']['status'] == 'pending'
    assert _context(conn, did)['lab_wait']['kind'] == 'dependency'


def test_principal_instructions_say_who_can_own_a_dependency_and_how_to_recheck_it():
    """DEPENDENCY_HAS_OWNER_20261010: em 10/10/2026 as nove pausas abertas do Concursa esperavam dependência. Os donos
    declarados eram "Administração autorizada do Concursa-Isolado, sob coordenação do Principal", "Manutenção NFOS /
    project_workflow" e "Principal / engenharia Concursa": ninguém que fosse avisado, e num caso o próprio Principal. Dois
    chamados esperavam uma autorização que o projeto não pergunta ao dono."""
    text = ' '.join(runtime.principal_instructions().split())
    assert "Never name yourself, the Principal or this project's own engineering as its owner" in text
    assert 'an authorization this project does not ask of its owner is nobody\'s to give' in text
    assert ('closes as a real partial delivery (partial_delivery=true with blockers and follow_ups), the remainder a '
            'mandatory criterion of the follow-up card') in text
    assert 'test the need by fact before declaring it again' in text
    # PROJECT_RULE_BEFORE_PARTIAL_20261010: às 09:56 UTC de 10/10 o Principal leu "closes as a real partial delivery" e
    # encaminhou o fechamento parcial de um chamado com dado errado, que o dono tinha acabado de recusar para o projeto.
    assert text.index('Follow what the project instructions say for that case') < text.index('closes as a real partial delivery (partial_delivery=true')
    assert 'a project may forbid closing with the request unmet' in text and 'Where they give no rule' in text


def test_dependency_without_hours_returns_in_one_hour_and_backs_off_on_the_same_pause(running, monkeypatch):
    """DEPENDENCY_RECHECK_20261010: sem prazo dito, a pausa volta em 1 h; declarada de novo, em 2, 4 e 6 h. O prazo dito pelo
    Principal vale como dito e não reinicia o recuo."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    assert '"recheck_hours":6' not in d.get_decision(conn, did)['question'], 'o exemplo não ensina mais as 6 h'
    clock = [int(time.time())]
    monkeypatch.setattr(d.time, 'time', lambda: clock[0])
    bare = {k: DEPENDENCY[k] for k in ('owner', 'need')}
    for hours in (1, 2, 4, 6, 6):
        d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=bare)
        wait = _context(conn, did)['lab_wait']
        assert wait['hold'] is True and wait['until'] == clock[0] + hours * 3600, hours
        clock[0] = wait['until'] + 1
        runtime.reconcile_runtime(conn)
        runtime.reconcile_runtime(conn)
        assert _context(conn, did)['lab_wait']['hold'] is False, 'no prazo volta ao Principal'
    d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=dict(bare, recheck_hours=3))
    assert _context(conn, did)['lab_wait']['until'] == clock[0] + 3 * 3600


def test_principal_instructions_no_longer_teach_a_fixed_six_hours():
    text = ' '.join(runtime.principal_instructions().split())
    assert '"recheck_hours":6' not in text
    assert 'returns to you in 1 hour, then 2, 4 and 6 for the same pause' in text
    assert 'add "recheck_hours" (1 to 24) only when you know the hour that fact can change' in text

