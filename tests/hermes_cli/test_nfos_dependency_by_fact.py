"""DEPENDENCY_BY_FACT_20261010: a dependência de uma pausa é um fato que o motor confere, não um texto que o Principal relê.

Em 10/10/2026 o Concursa tinha nove pausas esperando dependência declarada como "quem entrega" e "o que falta" em texto livre.
No prazo o relógio só devolvia a pergunta ao mesmo Principal: o que faltava a seis delas tinha sido entregue de 1 a 6 h antes,
e um chamado esperava o card da causa enquanto a causa, filha dele na ordenação, esperava o chamado. Agora a dependência leva
quem entrega em campo tipado e o fato conferível (outro card, sonda, laboratório); o motor confere a cada 5 min e, quando o fato
fica verdadeiro, encerra a pausa e devolve o card à fila sem acordar ninguém.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_runtime as runtime
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_maintenance_backoff import ANSWER, _context, _obligation, _requests
from tests.hermes_cli.test_nfos_maintenance_pause import _paused_and_exited, running  # noqa: F401
from tests.hermes_cli.test_nfos_principal_acceptance import assessment, task_context  # noqa: F401

HOUR = 3600
TICK = 300
NEED = 'Mecanismo publicado de inverso focal literal, validado no caso nominal.'
CARD = {'kind': 'card', 'name': 'executor do card da causa'}
LAB = {'kind': 'lab', 'name': 'mantenedor do Concursa-Isolado'}
PROBE = {'kind': 'http', 'url': 'https://app.example/sign-in', 'expect': {'status': 200, 'contains_all': ['Entrar']}}


class _Waits:
    """O registro único, carregado no uso: na árvore sem a regra o teste falha no que afirma, não na coleta."""

    def __getattr__(self, name):
        from hermes_cli import nfos_waits
        return getattr(nfos_waits, name)


waits = _Waits()


@pytest.fixture
def paused(running, monkeypatch):
    """Card pausado com o worker fora do ar, a obrigação de reparo pendente e um relógio que o teste adianta."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    decision_id = _obligation(conn, task)
    now = [float(int(time.time()))]
    monkeypatch.setattr(time, 'time', lambda: now[0])

    def advance(seconds=TICK):
        now[0] += seconds
        runtime.reconcile_runtime(conn)

    return conn, task, decision_id, advance


def _cause(conn):
    return kb.create_task(conn, title='Card da causa', assignee='default', delivery_type='report', requires_repo=False)


def _finish(conn, task_id):
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), task_id))
    conn.commit()


def _declare(conn, decision_id, check, executor=CARD, **extra):
    d.resolve_decision(conn, decision_id, action='continue', answer=ANSWER, author='Principal',
                       dependency=dict({'executor': executor, 'need': NEED, 'check': check}, **extra))


def _pause(conn, task):
    return json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])['maintenance_pause']


def test_dependency_on_another_card_ends_the_pause_when_that_card_delivers(paused):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _declare(conn, did, {'kind': 'card', 'task_id': cause})
    row, context = d.get_decision(conn, did), _context(conn, did)
    assert row['status'] == 'pending' and context['lab_wait']['hold'] is True and context['lab_wait']['kind'] == 'dependency'
    wait = waits.open_for_decision(conn, did)
    assert context['lab_wait']['wait_id'] == wait['id'] and wait['reason'] == 'external_dependency'
    assert wait['executor'] == CARD and wait['predicate'] == {'kind': 'card', 'task_id': cause, 'until': 'done'}
    assert wait['deadline_at'] == wait['created_at'] + d.MAINTENANCE_DEPENDENCY_HOURS[1] * HOUR
    assert wait['origin']['pause_run_id'] == task.current_run_id and wait['origin']['subject'] == NEED
    assert did not in [x['id'] for x in d.pending_decisions(conn)], 'fora da fila do Principal enquanto o motor confere'
    assert NEED[:40] in d.get_workflow(conn, task.id)['next_action']
    before = len(_requests(conn, task, did))
    for _ in range(12):
        advance()
    assert len(_requests(conn, task, did)) == before, 'uma hora de conferências sem acordar o Principal'
    assert repair.maintenance_pause_pending(conn, task.id) and kb.get_task(conn, task.id).status == 'blocked'
    assert waits.get(conn, wait['id'])['attempts'] >= 10 and waits.get(conn, wait['id'])['evidence']['card_status'] != 'done'

    _finish(conn, cause)
    advance()
    done = waits.get(conn, wait['id'])
    assert (done['status'], done['outcome']) == ('satisfied', 'resumed') and done['evidence']['card_status'] == 'done'
    pause = _pause(conn, task)
    assert pause['repair_kind'] == 'dependency_delivered' and pause['repaired_by'] == 'NFOS automation'
    assert wait['id'] in ' '.join(pause['repair_evidence']) and cause in ' '.join(pause['repair_evidence'])
    row, context = d.get_decision(conn, did), _context(conn, did)
    assert row['status'] == 'resolved' and 'dependência entregue' in row['answer'] and context['lab_wait']['hold'] is False
    assert not repair.maintenance_pause_pending(conn, task.id)
    assert kb.get_task(conn, task.id).status == 'ready', 'o card voltou à fila na mesma passada'
    assert len(_requests(conn, task, did)) == before, 'e ninguém foi acordado para isso'
    assert 'Dependência entregue' in d.get_workflow(conn, task.id)['next_action']
    assert kb.claim_task(conn, task.id).id == task.id


def test_dependency_on_a_confirmed_effect_of_another_card(paused):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _declare(conn, did, {'kind': 'card', 'task_id': cause, 'until': 'effect', 'operation': 'deploy'})
    advance()
    assert waits.open_for_decision(conn, did)['attempts'] == 1
    now = int(time.time())
    conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,evidence,created_at,updated_at) "
                 "VALUES('eff_1',?,1,'deploy','hml','0123abc','confirmed','{}',?,?)", (cause, now, now))
    conn.commit()
    advance()
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)
    assert _pause(conn, task)['repair_kind'] == 'dependency_delivered'


@pytest.mark.parametrize('dependency', [
    {'executor': {'kind': 'principal', 'name': 'Principal / engenharia do projeto'}, 'need': NEED, 'check': {'kind': 'card', 'task_id': 'CAUSE'}},
    {'executor': {'kind': 'runtime', 'name': 'Manutenção NFOS'}, 'need': NEED, 'check': {'kind': 'card', 'task_id': 'CAUSE'}},
    {'executor': {'kind': 'card', 'name': ' '}, 'need': NEED, 'check': {'kind': 'card', 'task_id': 'CAUSE'}},
    {'executor': CARD, 'need': NEED, 'check': {'kind': 'card', 'task_id': 't_inexistente'}},
    {'executor': CARD, 'need': NEED, 'check': {'kind': 'o laboratório voltar'}},
    {'executor': CARD, 'need': NEED, 'check': {'kind': 'decision_resolved', 'decision_id': 'dec_x'}},
    {'executor': CARD, 'need': NEED, 'check': {'kind': 'card', 'task_id': 'CAUSE', 'until': 'effect'}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'probe', 'probe': {'kind': 'http', 'url': 'https://app.example', 'expect': {'status': 200}}}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'lab_status', 'services': ['web; rm -rf /']}},
    {'executor': CARD, 'need': ' ', 'check': {'kind': 'card', 'task_id': 'CAUSE'}},
    {'executor': CARD, 'need': NEED, 'check': 'o card da causa concluir'},
    {'owner': 'Executor do card de engenharia CAUSE', 'need': NEED, 'check': {'kind': 'card', 'task_id': 'CAUSE'}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'lab_command', 'command': ['exec', 'rm', '-rf', '/'], 'exit': 0}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'lab_command', 'command': ['job', 'status', '../x'], 'exit': 0}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'lab_command', 'command': ['job', 'status', 'job-abc123'], 'exit_not': 64}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'lab_command', 'command': ['status']}},
    {'executor': LAB, 'need': NEED, 'check': {'kind': 'event', 'name': 'nfos_wait_declared'}},
])
def test_dependency_the_runtime_could_not_check_or_nobody_can_deliver_is_refused(paused, dependency):
    conn, task, did, _ = paused
    cause = _cause(conn)
    dependency = json.loads(json.dumps(dependency).replace('CAUSE', cause))
    before = _context(conn, did)
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency=dependency)
    assert d.get_decision(conn, did)['status'] == 'pending' and _context(conn, did) == before
    assert waits.open_for_decision(conn, did) is None and repair.maintenance_pause_pending(conn, task.id)


def test_card_that_cannot_move_before_this_one_is_not_awaited(paused):
    """CS-0017, 10/10/2026: o chamado esperava a causa e a causa, filha dele na ordenação, esperava o chamado."""
    conn, task, did, _ = paused
    child = _cause(conn)
    kb.link_tasks(conn, task.id, child)
    with pytest.raises(d.WorkflowError, match='would stop both'):
        _declare(conn, did, {'kind': 'card', 'task_id': child})
    assert waits.open_for_decision(conn, did) is None
    # O mesmo ciclo por espera: o outro card já espera por este.
    other = _cause(conn)
    waits.declare(conn, other, None, reason='external_dependency', executor=CARD,
                  predicate={'kind': 'card', 'task_id': task.id}, subject='o chamado')
    with pytest.raises(d.WorkflowError, match='would stop both'):
        _declare(conn, did, {'kind': 'card', 'task_id': other})
    # Sem a aresta de ordenação, a espera pela causa é aceita.
    assert kb.unlink_tasks(conn, task.id, child)
    _declare(conn, did, {'kind': 'card', 'task_id': child})
    assert waits.open_for_decision(conn, did)['predicate']['task_id'] == child


def test_awaited_card_closed_without_delivery_expires_the_wait_at_once(paused):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _declare(conn, did, {'kind': 'card', 'task_id': cause})
    wait = waits.open_for_decision(conn, did)
    before = len(_requests(conn, task, did))
    conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (cause,))
    conn.commit()
    advance()
    closed = waits.get(conn, wait['id'])
    assert (closed['status'], closed['outcome']) == ('expired', 'returned_to_obligation')
    assert closed['evidence']['expired_because'] == 'dependency_not_delivered'
    requests = _requests(conn, task, did)
    assert len(requests) == before + 1, 'a decisão volta ao Principal uma vez'
    assert 'o card esperado fechou sem entrega' in requests[-1]['question'][:400] and NEED[:40] in requests[-1]['question'][:400]
    assert _context(conn, did)['lab_wait']['hold'] is False and did in [x['id'] for x in d.pending_decisions(conn)]
    assert repair.maintenance_pause_pending(conn, task.id), 'a pausa segue: a dependência não foi entregue'
    with pytest.raises(d.WorkflowError, match='já venceu neste card'):
        _declare(conn, did, {'kind': 'card', 'task_id': cause})


def test_dependency_not_delivered_until_the_final_deadline_returns_once(paused):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _declare(conn, did, {'kind': 'card', 'task_id': cause})
    wait = waits.open_for_decision(conn, did)
    before = len(_requests(conn, task, did))
    advance(d.MAINTENANCE_DEPENDENCY_HOURS[1] * HOUR - TICK)
    assert waits.get(conn, wait['id'])['status'] == 'open' and len(_requests(conn, task, did)) == before
    advance(2 * TICK)
    closed = waits.get(conn, wait['id'])
    assert closed['status'] == 'expired' and closed['evidence']['expired_because'] == 'deadline'
    assert len(_requests(conn, task, did)) == before + 1
    assert 'não foi entregue' in _requests(conn, task, did)[-1]['question'][:300]
    advance(60)
    assert len(_requests(conn, task, did)) == before + 1, 'o lembrete seguinte respeita o recuo da obrigação de reparo'


def test_probe_declared_as_the_fact_releases_only_when_it_passes(paused, monkeypatch):
    conn, task, did, advance = paused
    results = [('FAIL', {'status': 503}, None), ('INDETERMINADO', None, 'URLError: timed out'), ('PASS', {'status': 200}, None)]
    probed = []

    def probe(conn_, task_id, spec):
        probed.append((task_id, spec['url']))
        return results[min(len(probed), len(results)) - 1]

    monkeypatch.setattr(d, '_execute_probe', probe)
    _declare(conn, did, {'kind': 'probe', 'probe': PROBE}, executor=LAB)
    advance()
    wait = waits.open_for_decision(conn, did)
    assert wait['evidence']['probe_state'] == 'FAIL' and wait['evidence']['http_status'] == 503
    advance()
    wait = waits.open_for_decision(conn, did)
    assert wait['evidence']['unreadable'] == 1 and 'URLError' in wait['evidence']['probe_error']
    assert repair.maintenance_pause_pending(conn, task.id), 'medição indeterminada não entrega nem vence a espera'
    advance()
    assert probed == [(task.id, PROBE['url'])] * 3
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)
    assert 'PASS' in ' '.join(_pause(conn, task)['repair_evidence'])


def test_laboratory_must_answer_on_the_app_the_card_needs(paused, monkeypatch):
    """LAB_CAPABILITY_PER_CARD_20261010: um app respondendo liberava a espera de todos, mesmo com o app do card fora do ar."""
    conn, task, did, advance = paused
    report = {'services': {'web': {'http': 200}, 'admin': {'error_type': 'TimeoutError'}}}
    reads = []

    def status(timeout=None):
        reads.append(1)
        return 'report', report

    monkeypatch.setattr(d, '_lab_status', status)
    _declare(conn, did, {'kind': 'lab_status', 'services': ['admin']}, executor=LAB)
    advance()
    wait = waits.open_for_decision(conn, did)
    assert wait['evidence']['answered'] == ['web'] and repair.maintenance_pause_pending(conn, task.id)
    report['services']['admin'] = {'http': 302}
    advance()
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)
    # Uma leitura do laboratório serve todas as esperas da mesma passada.
    reads.clear()
    budget = waits.Budget(3, time.monotonic() + 60)
    probe = {'predicate': {'kind': 'lab_status', 'services': ['web']}}
    assert d._lab_status_check(conn, probe, budget)[0] is True and d._lab_status_check(conn, probe, budget)[0] is True
    assert len(reads) == 1


def test_fact_already_true_waits_for_the_previous_executor_to_leave(paused, monkeypatch):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _finish(conn, cause)
    _declare(conn, did, {'kind': 'card', 'task_id': cause})
    idle, busy = repair._idle, [True]

    def not_idle(conn_, task_id):
        if busy[0]:
            raise d.WorkflowError('Previous worker termination is not confirmed')
        return idle(conn_, task_id)

    monkeypatch.setattr(repair, '_idle', not_idle)
    advance()
    wait = waits.open_for_decision(conn, did)
    assert wait['status'] == 'open' and wait['evidence']['not_ready'] is True and wait['attempts'] == 0
    assert repair.maintenance_pause_pending(conn, task.id) and d.get_decision(conn, did)['status'] == 'pending'
    busy[0] = False
    advance()
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)


def test_read_only_laboratory_command_with_the_declared_exit_code(paused, monkeypatch):
    """Tipos 2 e 3 da lista de 10/10/2026: o canal recusava a tarefa com saída 64, o job não tinha recibo terminal."""
    conn, task, did, advance = paused
    import subprocess
    runs, code = [], [64]

    def run(argv, **kwargs):
        runs.append((argv[1:], kwargs.get('shell', False)))
        if code[0] is None:
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
        return subprocess.CompletedProcess(argv, code[0], stdout='{}', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    _declare(conn, did, {'kind': 'lab_command', 'command': ['job', 'status', 'job-abc123'], 'exit': 0}, executor=LAB)
    advance()
    assert waits.open_for_decision(conn, did)['evidence']['exit'] == 64
    code[0] = None
    advance()
    assert waits.open_for_decision(conn, did)['evidence']['unreadable'] == 1, 'comando que não rodou não entrega nem vence'
    code[0] = 0
    advance()
    assert runs == [(['job', 'status', 'job-abc123'], False)] * 3, 'o subcomando declarado, em lista e sem shell'
    assert d.get_decision(conn, did)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)


def test_laboratory_out_of_reach_does_not_satisfy_a_command(paused, monkeypatch):
    """Só o código esperado prova o fato: com o laboratório fora do ar o comando sai com 255, e a pausa segue."""
    conn, task, did, advance = paused
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 255, stdout='', stderr=''))
    _declare(conn, did, {'kind': 'lab_command', 'command': ['runtime', 'capabilities'], 'exit': 0}, executor=LAB)
    for _ in range(3):
        advance()
    assert repair.maintenance_pause_pending(conn, task.id) and waits.open_for_decision(conn, did)['evidence']['exit'] == 255


def test_declaring_the_fact_has_the_same_gate_as_ending_the_pause(paused, monkeypatch):
    conn, task, did, _ = paused
    from agent import delegation_context
    cause = _cause(conn)
    with delegation_context.non_dispatcher_owned_context():  # tarefa agendada disparada de dentro de um worker, por exemplo
        with pytest.raises(d.WorkflowError, match='Only the Principal maintainer'):
            _declare(conn, did, {'kind': 'card', 'task_id': cause})
    assert waits.open_for_decision(conn, did) is None and 'lab_wait' not in _context(conn, did)


def test_dependency_without_a_fact_needs_someone_real_and_is_bounded(paused):
    """Os três tipos sem fato de 10/10/2026 (rota de processo que não existe, autorização com o próprio Principal como
    dono, credencial e gasto do dono) são recusados pelo tipo de quem entrega, sem ler texto."""
    conn, task, did, _ = paused
    for nobody in ({'kind': 'principal', 'name': 'Principal / engenharia Concursa'},
                   {'kind': 'runtime', 'name': 'Manutenção NFOS / project_workflow'}, {'kind': 'owner', 'name': 'Maikol'}):
        with pytest.raises(d.WorkflowError, match='Nobody waits for the Principal'):
            d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal',
                               dependency={'executor': nobody, 'need': NEED})
    assert 'lab_wait' not in _context(conn, did)
    d.resolve_decision(conn, did, action='continue', answer=ANSWER, author='Principal', dependency={'executor': LAB, 'need': NEED})
    held = _context(conn, did)['lab_wait']
    assert held['hold'] is True and held['executor'] == LAB and held['until'] == held['since'] + 3600
    assert waits.open_for_decision(conn, did) is None, 'sem predicado não é espera do registro: é a volta contada ao Principal'


def test_probe_internals_stay_in_the_wait_row_and_nowhere_else(paused, monkeypatch):
    """A sonda pode levar cabeçalho com credencial e consulta com dado do destino: só a linha da espera, que a executa, guarda."""
    conn, task, did, advance = paused
    probe = {'kind': 'http', 'url': 'https://app.example/api/saude?token=segredo-na-url',
             'headers': {'Authorization': 'Bearer segredo-literal'}, 'expect': {'status': 200, 'contains_all': ['pronto']}}
    monkeypatch.setattr(d, '_execute_probe', lambda conn_, task_id, spec: ('PASS', {'status': 200}, None))
    _declare(conn, did, {'kind': 'probe', 'probe': probe}, executor=LAB)
    wait = waits.open_for_decision(conn, did)
    assert wait['predicate']['probe'] == probe, 'a linha da espera guarda a sonda inteira para executá-la'
    shown = waits.card_waits(conn, task.id)[0]
    assert shown['predicate'] == {'kind': 'probe', 'probe': {'kind': 'http', 'target': 'app.example'}}
    advance()
    assert d.get_decision(conn, did)['status'] == 'resolved'
    outside = [r[0] or '' for r in conn.execute('SELECT payload FROM task_events WHERE task_id=?', (task.id,))]
    outside += [r[0] or '' for r in conn.execute('SELECT context FROM nfos_decisions WHERE task_id=?', (task.id,))]
    outside += [r[0] or '' for r in conn.execute('SELECT answer FROM nfos_decisions WHERE task_id=?', (task.id,))]
    outside += [r[0] or '' for r in conn.execute('SELECT metadata FROM task_runs WHERE task_id=?', (task.id,))]
    outside += [r[0] or '' for r in conn.execute('SELECT next_action FROM nfos_workflows WHERE task_id=?', (task.id,))]
    outside.append(json.dumps(waits.card_waits(conn, task.id)))
    assert not [text for text in outside if 'segredo' in text or 'Bearer' in text or 'contains_all' in text]


def test_only_the_runtime_ends_a_pause_and_only_on_its_own_verified_dependency(paused, monkeypatch):
    conn, task, did, advance = paused
    cause = _cause(conn)
    _declare(conn, did, {'kind': 'card', 'task_id': cause})
    wait = waits.open_for_decision(conn, did)
    request = dict(wait_id=wait['id'], evidence=['a dependência chegou'])
    with pytest.raises(d.WorkflowError, match='verified by the wait registry'):  # a espera ainda está aberta
        repair.finish_dependency_pause(conn, task.id, task.current_run_id, **request)
    with pytest.raises(d.WorkflowError, match='verified by the wait registry'):
        repair.finish_dependency_pause(conn, task.id, task.current_run_id, wait_id='wait_inexistente', evidence=['x'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    with pytest.raises(d.WorkflowError, match='Only the runtime'):
        repair.finish_dependency_pause(conn, task.id, task.current_run_id, **request)
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    assert repair.maintenance_pause_pending(conn, task.id)


def test_card_of_another_tenant_is_not_awaited(paused):
    conn, task, did, _ = paused
    foreign = kb.create_task(conn, title='Card de outro negócio', assignee='default', delivery_type='report', requires_repo=False,
                             tenant='outro-negocio')
    with pytest.raises(d.WorkflowError, match='does not exist on this board'):
        _declare(conn, did, {'kind': 'card', 'task_id': foreign})
    assert waits.open_for_decision(conn, did) is None


def test_budget_pause_takes_no_dependency(paused):
    conn, task, did, _ = paused
    metadata = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])
    metadata['maintenance_pause']['kind'] = 'runtime_budget_exhausted'
    conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
    conn.commit()
    with pytest.raises(d.WorkflowError, match='budget pause'):
        _declare(conn, did, {'kind': 'card', 'task_id': _cause(conn)})
    assert waits.open_for_decision(conn, did) is None


def test_principal_is_told_how_to_declare_a_fact_the_runtime_checks():
    text = ' '.join(runtime.principal_instructions().split())
    assert '"check"' in text and 'the fact the runtime verifies by itself' in text
    assert '{"kind":"card","task_id":"t_..."}' in text and '"kind":"lab_status","services":["web"]' in text
    assert 'ends the pause and returns the card to the queue without waking you' in text
