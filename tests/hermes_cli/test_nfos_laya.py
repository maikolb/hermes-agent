"""Explicit local HTTP fixtures exercising native NFOS; not live model evidence."""
import json

import pytest
import yaml

from hermes_cli import nfos_jev as engine, nfos_delivery as d, nfos_principal_review as review, kanban_db as kb
from tests.hermes_cli.test_nfos_jev import http_fixture, prepare_probe
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, save_report


def select_laya(conn, task, server, uses):
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'config.yaml'
    cfg = yaml.safe_load(path.read_text())
    delivery = cfg.setdefault('kanban', {}).setdefault('delivery', {})
    delivery['decision_engines'] = {'allowed': ['laya', 'jev']}
    delivery['laya'] = {'enabled': False, 'endpoint': server.url + '/systemone',
                        'uses': uses, 'model_revision': 'fixture-revision', 'source_revision': 'fixture-source'}
    path.write_text(yaml.safe_dump(cfg))
    config = engine.settings(engine='laya')
    config.pop('enabled')
    selection = {'engine': 'laya', 'source': 'user_hashtag', 'revision': engine._digest(config), 'config': config}
    state = json.loads(d.get_workflow(conn, task.id)['state_json'])
    state['decision_engine_selection'] = selection
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (json.dumps(state), task.id))
    conn.commit()
    server.model, server.model_revision = 'convaiinnovations/laya-multilingual', 'fixture-revision'
    return selection


def save(conn, task, spec):
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})


def latest(conn, task):
    return conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='spec_review' ORDER BY rowid DESC LIMIT 1", (task.id,)).fetchone()


@pytest.mark.parametrize('choice,verdict,action', [('complete', 'accept', 'continue'), ('missing', 'changes', 'changes')])
def test_native_laya_primary_replaces_principal(task_context, http_fixture, choice, verdict, action):
    conn, task, spec, _ = task_context
    select_laya(conn, task, http_fixture, ['budget', 'spec'])
    http_fixture.choices.update(coverage=choice, verdict=verdict, size='P')
    save(conn, task, spec)
    assert latest(conn, task)['author'] == 'Laya'
    assert latest(conn, task)['action'] == action
    assert review.accepted(conn, task.id, 'spec_review') is (action == 'continue')
    assert not kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert all(call[0] == '/systemone' and call[1] is None for call in http_fixture.calls)
    assert kb.get_task(conn, task.id).model_override == task.model_override
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_primary_spec' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['used_engine'] == 'laya' and receipt['principal_calls_saved'] == 1


@pytest.mark.parametrize('failure', ['revision', 'source_revision', 'contradiction', 'context', 'confidence'])
def test_laya_fallback_has_explicit_receipt(task_context, http_fixture, failure):
    conn, task, spec, _ = task_context
    select_laya(conn, task, http_fixture, ['spec'])
    if failure == 'revision':
        http_fixture.model_revision = 'wrong-checkpoint'
    elif failure == 'source_revision':
        http_fixture.source_revision = 'wrong-source'
    elif failure == 'contradiction':
        http_fixture.choices.update(coverage='missing', verdict='accept')
    elif failure == 'confidence':
        http_fixture.confidence = .2
    else:
        request = d.get_request(conn, d.get_workflow(conn, task.id)['request_id'])
        payload = json.loads(request['payload']); payload['text'] += ' contexto completo' * 4000
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), request['id'])); conn.commit()
    save(conn, task, spec)
    assert latest(conn, task)['status'] == 'pending'
    assert not review.accepted(conn, task.id, 'spec_review')
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['selected_engine'] == 'laya' and receipt['used_engine'] is None
    assert receipt['fallback_reason'] and receipt['principal_calls_saved'] == 0
    if failure == 'context':
        assert not http_fixture.calls
    if failure == 'confidence':
        assert receipt['inference_engine'] == 'laya' and receipt['inference_answers']
        assert 'answers' not in receipt


@pytest.mark.parametrize('use', ['impediment', 'evidence'])
def test_laya_executes_native_probe_without_second_action_review(task_context, http_fixture, monkeypatch, use):
    conn, task, spec, artifact = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    select_laya(conn, task, http_fixture, [use])
    if use == 'impediment':
        assert kb.block_task(conn, task.id, reason='Medir o total configurado', kind='transient', expected_run_id=task.current_run_id)
        assert not kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    else:
        save_report(conn, task, artifact)
        assert not review.accepted(conn, task.id, 'final_review')
    assert http_fixture.probes == ['/count']
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_action' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['used_engine'] == 'laya' and receipt['typed_action_executed']
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


def test_selected_config_drift_invalidates_acceptance(task_context, http_fixture):
    from hermes_constants import get_hermes_home
    conn, task, spec, _ = task_context
    select_laya(conn, task, http_fixture, ['spec'])
    save(conn, task, spec)
    assert review.accepted(conn, task.id, 'spec_review')
    path = get_hermes_home() / 'config.yaml'
    cfg = yaml.safe_load(path.read_text()); cfg['kanban']['delivery']['laya']['model_revision'] = 'new-revision'
    path.write_text(yaml.safe_dump(cfg))
    assert not review.accepted(conn, task.id, 'spec_review')


def test_probe_failure_escalates_without_claiming_saved_call(task_context, http_fixture, monkeypatch):
    from tests.hermes_cli.test_nfos_principal_acceptance import accept
    conn, task, spec, _ = task_context
    spec = prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    spec['criteria'][0]['probe']['expect'] = {'equals': 99}
    save(conn, task, spec)
    accept(conn, task, 'spec_review')
    select_laya(conn, task, http_fixture, ['impediment'])
    assert kb.block_task(conn, task.id, reason='Verificar contagem', kind='transient')
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_action' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['principal_calls_saved'] == 0 and receipt['fallback_reason'] == 'probe_failed_or_inconclusive'


def test_invalid_selected_provider_keeps_selection_in_receipt(task_context, http_fixture):
    from hermes_constants import get_hermes_home
    conn, task, spec, _ = task_context
    select_laya(conn, task, http_fixture, ['spec'])
    path = get_hermes_home() / 'config.yaml'
    cfg = yaml.safe_load(path.read_text()); cfg['kanban']['delivery']['laya']['endpoint'] = 'https://public.example/systemone'
    path.write_text(yaml.safe_dump(cfg))
    save(conn, task, spec)
    assert not http_fixture.calls
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['selected_engine'] == 'laya' and receipt['used_engine'] is None
    assert receipt['fallback_reason'] == 'invalid_config'


def test_server_token_limit_refusal_is_explicit(task_context, http_fixture):
    conn, task, spec, _ = task_context
    select_laya(conn, task, http_fixture, ['spec'])
    http_fixture.code = 422
    http_fixture.malformed = {'reason': 'context_too_large'}
    save(conn, task, spec)
    assert latest(conn, task)['status'] == 'pending'
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['fallback_reason'] == 'context_too_large' and receipt['used_engine'] is None
