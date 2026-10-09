"""O motor NFOS obedece ao processo do projeto (project_workflow): banco real do Kanban, requests sintéticos.

Os testes com o processo do Concursa usam o módulo project_workflow do repositório nexa-factory-os, apontado
por PROJECT_WORKFLOW_SRC; sem ele, só rodam os que não dependem do módulo (motor desligado e falha fechada).
"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_process

MODULE_SRC = os.environ.get('PROJECT_WORKFLOW_SRC', '')


def _module_available():
    if not MODULE_SRC or not (Path(MODULE_SRC) / 'project_workflow' / 'enforce.py').is_file():
        return False
    if MODULE_SRC not in sys.path:
        sys.path.insert(0, MODULE_SRC)
    return hasattr(importlib.import_module('project_workflow.enforce'), 'judge')


needs_module = pytest.mark.skipif(not _module_available(), reason='precisa de PROJECT_WORKFLOW_SRC apontando para o módulo project_workflow')


@pytest.fixture
def card(tmp_path, monkeypatch, request):
    home = tmp_path / 'home'
    board = home / 'kanban' / 'boards' / 'concursa-ai' / 'kanban.db'
    board.parent.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    pw = tmp_path / 'pw' / 'project_workflow.db'
    pw.parent.mkdir()
    monkeypatch.setenv('PROJECT_WORKFLOW_DB', str(pw))
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': {
        'principal_validation': True, 'worker_model': 'gpt-5.6-luna',
        'worker_provider': 'openai-codex', 'worker_reasoning_effort': 'high'}}}))
    delivery_type = getattr(request, 'param', 'code')
    with kb.connect_closing() as conn:
        request_id = d.receive_request(conn, source={'platform': 'fixture', 'chat_id': 'local', 'thread_id': 't', 'message_id': '1'},
                                       text='Corrigir o cargo', project={'board': 'concursa-ai', 'profile': 'default',
                                                                        'delivery_type': delivery_type, 'repo_path': str(tmp_path)})
        claim = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, request_id, claim['claim_token'], pid=os.getpid())
        yield conn, task, pw


def policy(pw, mode):
    (pw.parent / 'policy.json').write_text(json.dumps({'enforce': True, 'motor': {'concursa-ai': mode}}), encoding='utf-8')


def seed(pw):
    from project_workflow.store import Store
    store = Store(pw)
    store.init()
    # O Concursa entra na versão 2 fixada aqui (portões do Maikol no dado e no Aceite): os testes exercitam os ganchos
    # do motor com um processo conhecido, independente da versão que o módulo entrega.
    for path in (Path(MODULE_SRC) / 'project_workflow' / 'definitions' / 'base.entrega.json',
                 Path(__file__).parent / 'fixtures' / 'concursa-ai.entrega.v2.json'):
        spec = json.loads(path.read_text(encoding='utf-8'))
        store.add_definition(spec, source_text=path.name, created_by='teste', approve_as=('teste', 'fixture'))
    return store


SPEC = {'goal': 'Corrigir o cargo', 'criteria': [{'id': 'C1', 'text': 'O cargo aparece certo'}],
        'steps': ['Reproduzir e corrigir'], 'delivery_type': 'code'}


def save_spec(conn, task, spec=SPEC):
    return d.save_spec(conn, task.id, task.current_run_id, spec, author='Claude TL', evidence={'session': 'synthetic-tl'})


def accept_spec(conn, task):
    decision = d.ask_principal(conn, task.id, task.current_run_id, kind='spec_review', question='Review the spec', context={})
    d.resolve_decision(conn, decision, action='continue', answer='Reviewed', author='Principal', assessment={
        'request_alignment': 'ok', 'scope_assessment': 'ok', 'criteria': [{'id': 'C1', 'verdict': 'accept', 'observation': 'ok'}]})


def steps(store, task, *keys):
    for key in keys:
        store.record_step('concursa-ai', task.id, key, source='agent',
                          note='aprovado: mensagem de teste' if key in ('aceite', 'autorizacao_dado') else 'feito')


def test_motor_off_changes_nothing(card):
    conn, task, pw = card
    assert nfos_process.mode('concursa-ai') == 'off'
    assert save_spec(conn, task) == 1
    assert not pw.exists(), 'sem política o motor nem abre o banco do processo'


@needs_module
def test_domain_routing_uses_real_card_revision_and_candidate(card, tmp_path):
    import hashlib
    from project_workflow import validation
    conn, task, pw = card
    store = seed(pw)
    current = Path(MODULE_SRC) / 'project_workflow' / 'definitions' / 'concursa-ai.entrega.json'
    store.add_definition(json.loads(current.read_text(encoding='utf-8')), source_text='Synthetic routing authority',
                         created_by='fixture', approve_as=('fixture', 'synthetic'))
    policy(pw, 'enforce')
    store.bind('concursa-ai', task.id, by='fixture')
    # Worktree real do card: na rota curta o motor lê o diff da candidata contra a base antes de publicar.
    import subprocess
    repo = tmp_path / 'worktree'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    git('init', '-q')
    git('config', 'user.email', 'fixture@example.com')
    git('config', 'user.name', 'fixture')
    (repo / 'README.md').write_text('base', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'apps').mkdir()
    (repo / 'apps' / 'receiver.tsx').write_text('receiver', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'receiver')
    candidate = git('rev-parse', 'HEAD')
    conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(repo), task.id))
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,'worktree_creation_requested',?,1)",
                 (task.id, json.dumps({'base_sha': base})))
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',
                 (json.dumps({'candidate_sha': candidate}), task.id))
    conn.commit()
    engine = nfos_process._engine(conn, task.id)
    assert engine['candidate_sha'] == candidate
    assert engine['instruction_revision'] == conn.execute('SELECT instruction_revision FROM tasks WHERE id=?', (task.id,)).fetchone()[0]
    proof = tmp_path / 'inspected.txt'
    proof.write_text('Synthetic source, dependency and result inspection', encoding='utf-8')
    ref = dict(path=str(proof), sha256=hashlib.sha256(proof.read_bytes()).hexdigest())
    value = dict(rationale='Receiver flow inspected', dependencies_complete=True, data_issue=False, impacts=[
        dict(domain='edital_extraction', relation='excluded', rationale='No parser dependencies', evidence=[ref]),
        dict(domain='sineta', relation='direct', rationale='Receiver flow affected', evidence=[ref])])
    plan = validation.prepare_plan(value, engine, board='concursa-ai', task_id=task.id)
    store.record_step('concursa-ai', task.id, 'triagem', source='agent', evidence={'validation_plan': plan})
    assert 'p5' in [item['step'] for item in nfos_process.position(conn, task.id)['next']]
    steps(store, task, 'p5', 'p6', 'p7', 'publicar')
    assert nfos_process.evaluate(conn, task.id, effect='deploy')[1], 'Position alone cannot publish'
    receipt = validation.prepare_result(dict(plan_digest=plan['digest'], rationale='Inspected focal proof',
        coverage=[dict(requirement='focal:sineta', status='passed', evidence=[ref])]), plan, engine)
    store.record_step('concursa-ai', task.id, 'publicar', source='agent', evidence={'validation_result': receipt})
    for _ in range(2):
        decision, refusal = nfos_process.evaluate(conn, task.id, effect='deploy')
        assert not refusal, refusal
        assert decision['validation_route'] == 'focal'
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',
                 (json.dumps({'candidate_sha': 'b' * 40}), task.id))
    conn.commit()
    assert nfos_process.evaluate(conn, task.id, effect='deploy')[1], 'Another candidate needs its own result'


def test_enforce_without_the_process_refuses_instead_of_skipping_it(card, monkeypatch, tmp_path):
    conn, task, pw = card
    policy(pw, 'enforce')
    empty = tmp_path / 'sem-modulo'
    empty.mkdir()
    monkeypatch.setenv('PROJECT_WORKFLOW_SRC', str(empty))
    for name in [m for m in sys.modules if m == 'project_workflow' or m.startswith('project_workflow.')]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, 'path', [p for p in sys.path if p != MODULE_SRC])
    with pytest.raises(nfos_process.ProcessRefusal, match='não conseguiu consultar o processo'):
        save_spec(conn, task)
    assert d.get_workflow(conn, task.id)['spec_revision'] == 0, 'a troca recusada não deixou rastro'


@needs_module
def test_the_motor_obeys_the_concursa_process(card):
    conn, task, pw = card
    store = seed(pw)
    policy(pw, 'enforce')
    with pytest.raises(nfos_process.ProcessRefusal) as refused:
        save_spec(conn, task)
    assert 'p1 (1 · Reproduzir)' in str(refused.value) and 'p3 (3 · Origem)' in str(refused.value)
    assert d.get_workflow(conn, task.id)['spec_revision'] == 0
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='nfos_spec_saved'", (task.id,)).fetchone()
    store.bind('concursa-ai', task.id, by='teste')
    steps(store, task, 'triagem', 'p1', 'p2', 'p3')
    assert save_spec(conn, task) == 1
    saved = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_spec_saved'",
                                    (task.id,)).fetchone()['payload'])
    assert saved['process']['step'] == 'p4' and saved['process']['move'] == 'forward'
    last = store.history('concursa-ai', task.id)[-1]
    assert last['step_key'] == 'p4' and last['source'] == 'engine' and last['evidence']['move'] == 'forward'
    accept_spec(conn, task)
    with pytest.raises(nfos_process.ProcessRefusal, match='Portão sem registro válido: aceite'):
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Implementar')
    steps(store, task, 'aceite')
    d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Implementar')
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'p5'
    assert d.get_workflow(conn, task.id)['stage'] == 'implement'
    with pytest.raises(nfos_process.ProcessRefusal, match='Falta registrar: p6'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='main', candidate='a' * 40)


@needs_module
def test_completion_waits_for_the_process_and_is_recorded_once(card):
    conn, task, pw = card
    store = seed(pw)
    policy(pw, 'enforce')
    store.bind('concursa-ai', task.id, by='teste')
    steps(store, task, 'triagem', 'p1', 'p2', 'p3', 'p4', 'aceite', 'p5')
    decision, refusal = nfos_process.evaluate(conn, task.id, stage='done')
    assert decision['target'] == 'fim' and 'p6 (6 · Validar)' in refusal
    with kb.write_txn(conn):
        for _ in range(3):  # o despachante tenta a cada ciclo
            nfos_process.completion_blocked(conn, task.id, task.current_run_id, refusal, append_event=kb._append_event)
    blocked = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completion_blocked_process'",
                           (task.id,)).fetchone()[0]
    assert blocked == 1
    assert d.get_workflow(conn, task.id)['next_action'] == refusal[:400]
    steps(store, task, 'p6', 'p7', 'publicar', 'conferir')
    assert nfos_process.evaluate(conn, task.id, stage='done') [1] is None


@needs_module
@pytest.mark.parametrize('card', ['report'], indirect=True)
def test_reading_cards_stay_outside_the_concursa_process(card):
    conn, task, pw = card
    seed(pw)
    policy(pw, 'enforce')
    spec = dict(SPEC, delivery_type='report')
    assert save_spec(conn, task, spec) == 1, 'pergunta e leitura não entram na esteira'
    accept_spec(conn, task)
    with pytest.raises(nfos_process.ProcessRefusal, match='executar o efeito repair'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='repair', target='prod', candidate='preimage-1')


@needs_module
def test_shadow_records_the_position_and_audits_what_it_would_refuse(card):
    conn, task, pw = card
    store = seed(pw)
    policy(pw, 'shadow')
    assert save_spec(conn, task) == 1
    with store.session() as db:
        audit = db.execute("SELECT action, detail_json FROM project_workflow_audit WHERE actor='motor'").fetchall()
    assert [row['action'] for row in audit] == ['shadow_refused']
    assert 'p1 (1 · Reproduzir)' in json.loads(audit[0]['detail_json'])['message']
    assert [h['step_key'] for h in store.history('concursa-ai', task.id)] == ['triagem', 'p4']
    shown = nfos_process.position(conn, task.id)
    assert shown['mode'] == 'shadow' and shown['current'] == 'p4' and shown['next'][0]['step'] == 'aceite'


@needs_module
def test_the_decision_lives_in_the_event_and_a_lost_projection_is_replayed(card, monkeypatch):
    conn, task, pw = card
    store = seed(pw)
    policy(pw, 'enforce')
    store.bind('concursa-ai', task.id, by='teste')
    steps(store, task, 'triagem', 'p1', 'p2', 'p3')
    from project_workflow.store import Store
    original = Store.replay_motor_events

    def lost(self, *args, **kwargs):
        raise OSError('banco do processo indisponível depois do commit')

    monkeypatch.setattr(Store, 'replay_motor_events', lost)
    assert save_spec(conn, task) == 1, 'a troca permitida já está no Kanban; a projeção perdida não a desfaz'
    monkeypatch.setattr(Store, 'replay_motor_events', original)
    event = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='nfos_spec_saved'", (task.id,)).fetchone()
    assert json.loads(event['payload'])['process'] == {'step': 'p4', 'move': 'forward', 'entry': None, 'allowed': True,
                                                      'mode': 'enforce', 'workflow_id': 2, 'version': 1}
    failed = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_process_projection_failed'",
                          (task.id,)).fetchone()
    assert json.loads(failed['payload'])['engine_event'] == event['id']
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'p3'
    # O próximo julgamento do card refaz, antes de decidir, a projeção que ficou só no evento.
    decision, refusal = nfos_process.evaluate(conn, task.id, stage='implement')
    assert decision['current'] == 'p4' and 'Portão sem registro válido: aceite' in refusal
    nfos_process.evaluate(conn, task.id, stage='implement')
    history = store.history('concursa-ai', task.id)
    assert [h['step_key'] for h in history] == ['triagem', 'p1', 'p2', 'p3', 'p4'], 'refeita uma vez, na ordem'
    assert history[-1]['evidence'] == {'move': 'forward', 'engine_event': event['id']}


def test_a_broken_policy_keeps_the_motor_locked(card):
    conn, task, pw = card
    (pw.parent / 'policy.json').write_text('{"motor": {"concursa-ai": "enforce"', encoding='utf-8')
    assert nfos_process.mode('concursa-ai') == 'enforce' and nfos_process.mode('dovcrm') == 'enforce'
    (pw.parent / 'policy.json').write_text('{"motor": {"concursa-ai": "talvez"}}', encoding='utf-8')
    assert nfos_process.mode('concursa-ai') == 'enforce'
    (pw.parent / 'policy.json').unlink()
    assert nfos_process.mode('concursa-ai') == 'off', 'sem arquivo: nunca foi ligado'


@needs_module
def test_the_motor_refuses_when_its_own_decisions_cannot_be_read(card, monkeypatch):
    conn, task, pw = card
    store = seed(pw)
    policy(pw, 'enforce')
    store.bind('concursa-ai', task.id, by='teste')
    steps(store, task, 'triagem', 'p1', 'p2', 'p3')
    from project_workflow import enforce
    import sqlite3

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError('database disk image is malformed')

    monkeypatch.setattr(enforce, 'motor_events_in', unreadable)
    with pytest.raises(nfos_process.ProcessRefusal, match='não conseguiu consultar o processo'):
        save_spec(conn, task)
    assert d.get_workflow(conn, task.id)['spec_revision'] == 0
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'p3'


@needs_module
def test_the_worker_cli_gets_the_refusal_as_its_json_error_not_a_traceback(card, tmp_path):
    # O worker roda hermes_cli/nfos_delivery.py como script: o arquivo é __main__ e a recusa vem do
    # WorkflowError de hermes_cli.nfos_delivery, outra cópia da classe. Tem de sair no erro JSON da CLI.
    conn, task, pw = card
    seed(pw)
    policy(pw, 'enforce')
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps(SPEC), encoding='utf-8')
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'session': 'synthetic-worker'}), encoding='utf-8')
    script = Path(d.__file__)
    env = {**os.environ, 'HERMES_KANBAN_TASK': task.id, 'HERMES_KANBAN_RUN_ID': str(task.current_run_id),
           'PYTHONPATH': str(script.parents[1]), 'PROJECT_WORKFLOW_SRC': MODULE_SRC, 'PYTHONIOENCODING': 'utf-8'}
    result = subprocess.run([sys.executable, str(script), 'save-spec', '--input', str(spec), '--evidence', str(evidence),
                             '--author', 'worker', '--db', os.environ['HERMES_KANBAN_DB']],
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 1 and 'Traceback' not in result.stderr, result.stderr[-1500:]
    error = json.loads(result.stdout.strip().splitlines()[-1])
    assert error['type'] == 'ProcessRefusal' and 'p1 (1 · Reproduzir)' in error['error']
    assert d.get_workflow(conn, task.id)['spec_revision'] == 0


def seed_base(pw):
    """Só a esteira padrão: o board do card não tem processo próprio."""
    from project_workflow.store import Store
    store = Store(pw)
    store.init()
    spec = json.loads((Path(MODULE_SRC) / 'project_workflow' / 'definitions' / 'base.entrega.json').read_text(encoding='utf-8'))
    store.add_definition(spec, source_text='esteira padrão', created_by='teste', approve_as=('teste', 'fixture'))
    return store


def kinds(conn, task, kind):
    return [json.loads(row['payload']) for row in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (task.id, kind)).fetchall()]


@needs_module
def test_a_board_without_its_own_process_follows_the_default_pipeline(card):
    conn, task, pw = card
    store = seed_base(pw)
    policy(pw, 'enforce')
    assert save_spec(conn, task) == 1
    assert [h['step_key'] for h in store.history('concursa-ai', task.id)] == ['analise', 'spec']
    shown = nfos_process.position(conn, task.id)
    assert shown['title'] == 'Esteira NFOS' and shown['current'] == 'spec' and shown['governed']
    accept_spec(conn, task)
    with pytest.raises(nfos_process.ProcessRefusal, match='Falta registrar: implementar'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='main', candidate='a' * 40)


@needs_module
def test_the_effect_moves_the_card_with_its_decision_in_the_effect_event(card):
    # O worker faz o efeito e registra o estágio depois: o efeito leva o card à etapa, com a decisão no evento
    # do efeito e a projeção depois do commit; o efeito já pedido que ainda move o card deixa evento próprio.
    conn, task, pw = card
    store = seed_base(pw)
    policy(pw, 'enforce')
    save_spec(conn, task)
    accept_spec(conn, task)
    effect = {'operation': 'repair', 'target': 'producao', 'candidate': 'preimage-1'}
    assert d.begin_effect(conn, task.id, task.current_run_id, **effect)['execute']
    requested = kinds(conn, task, 'nfos_effect_requested')[-1]
    assert requested['process']['step'] == 'implementar' and requested['process']['move'] == 'forward'
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'implementar'
    assert not d.begin_effect(conn, task.id, task.current_run_id, **effect)['execute']
    assert kinds(conn, task, 'nfos_process_moved') == [], 'o mesmo efeito na mesma etapa não move nada'
    save_spec(conn, task, dict(SPEC, goal='Corrigir o cargo e o vínculo'))
    accept_spec(conn, task)
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'implementar', \
        'spec revista sem portão no meio continua a mudança: o card fica'
    store.record_step('concursa-ai', task.id, 'spec', source='agent', note='o Principal devolveu o card à spec')
    assert not d.begin_effect(conn, task.id, task.current_run_id, **effect)['execute']
    moved = kinds(conn, task, 'nfos_process_moved')
    assert len(moved) == 1 and moved[0]['process']['step'] == 'implementar'
    assert store.history('concursa-ai', task.id)[-1]['step_key'] == 'implementar'
