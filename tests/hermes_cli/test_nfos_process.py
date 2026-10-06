"""O motor NFOS obedece ao processo do projeto (project_workflow): banco real do Kanban, requests sintéticos.

Os testes com o processo do Concursa usam o módulo project_workflow do repositório nexa-factory-os, apontado
por PROJECT_WORKFLOW_SRC; sem ele, só rodam os que não dependem do módulo (motor desligado e falha fechada).
"""
import importlib
import json
import os
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
    definitions = Path(MODULE_SRC) / 'project_workflow' / 'definitions'
    for name in ('base.entrega.json', 'concursa-ai.entrega.json'):
        spec = json.loads((definitions / name).read_text(encoding='utf-8'))
        store.add_definition(spec, source_text=name, created_by='teste', approve_as=('teste', 'fixture'))
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
        d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='main', candidate='abc123')


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
