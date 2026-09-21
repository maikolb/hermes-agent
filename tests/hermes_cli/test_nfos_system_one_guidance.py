"""Native SQLite/batch contracts. Model replies in this file are explicit fixtures."""
import json
import io
from types import SimpleNamespace

import pytest

from hermes_cli import nfos_jev as engine, nfos_delivery as d
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept
from tests.hermes_cli.test_nfos_jev import http_fixture
from tests.hermes_cli.test_nfos_laya import select_laya


def test_no_probe_spec_can_guide_and_trace_actual_next_tool(task_context, http_fixture, monkeypatch):
    from tests.hermes_cli.test_nfos_system_one_enforcement import selected_context, native_call
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    requirement = engine._requirement(conn, task.id)
    assert requirement['route'] == 'acquire_context' and requirement['enforced']
    assert requirement['targets'].get(str(artifact))
    native_call(monkeypatch, agent, 'read_file', {'path': str(artifact)}, lambda args: artifact.read_text())
    completed = engine._requirement(conn, task.id)
    assert completed['status'] == 'completed' and completed['typed_action_executed']
    assert completed['principal_calls_saved'] == 0
    assert completed['result_sha256'] == engine._digest(artifact.read_text())


def test_phase_limits_reserve_lifecycle_capacity():
    policy = engine._validate_system_one({'mode': 'active', 'engine': 'jev',
        'max_decisions_per_run': 12, 'phase_limits': {
            'budget': 2, 'spec': 2, 'guidance': 3, 'impediment': 2, 'evidence': 3}})
    assert sum(policy['phase_limits'].values()) == policy['max_decisions_per_run']


def test_unavailable_tools_never_become_guidance_options():
    agent = SimpleNamespace(tools=[{'type': 'function', 'function': {'name': 'send_payment'}},
                                   {'type': 'function', 'function': {'name': 'write_file'}}])
    assert set(engine._guidance_routes(agent, 'implement', 'stage_start', {}, [])) == {
        'continue_worker', 'escalate_existing'}


def test_native_budget_reserves_atomically_and_never_records_key(tmp_path, monkeypatch):
    from scripts.nfos_system_one_eval_budget import guarded_call, BudgetUnavailable
    import urllib.request
    ledger = tmp_path / 'ledger.json'
    initial = {'key_usage': '0', 'total_usage': '0', 'total_credits': '3'}
    ledger.write_text(json.dumps({'initial': initial, 'requests': [], 'tariff': {
        'pricing': {'prompt': '0.000000042', 'completion': '0'}, 'context_length': 32000}}))
    def account(req, **kwargs):
        data = {'usage': 0} if req.full_url.endswith('/key') else {'total_usage': 0, 'total_credits': 3}
        return io.BytesIO(json.dumps({'data': data}).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', account)
    cfg = {'endpoint': 'https://openrouter.ai/api/v1/systemone', 'timeout_seconds': 1,
           'evaluation_budget': {'ledger_path': str(ledger), 'limit_usd': '2.88'}}
    payload = {'questions': {'route': {}}, 'state': {'private': 'not exported'}}
    def send(*args):
        current = json.loads(ledger.read_text())['requests'][-1]
        assert current['pending'] and current['question_count'] == 1
        assert current['reserved'] == '0.003688000'
        with pytest.raises(BudgetUnavailable, match='in_flight'):
            guarded_call(cfg, payload, 'credential-fixture', send)
        return {'usage': {'cost': '0.000001'}}
    guarded_call(cfg, payload, 'credential-fixture', send)
    text = ledger.read_text()
    assert 'credential-fixture' not in text and 'not exported' not in text
    assert json.loads(text)['requests'][-1]['billed'] == '0.000001'
    assert not ledger.with_suffix('.json.lock').exists()


def test_phase_exhaustion_does_not_starve_guidance(task_context, http_fixture, monkeypatch):
    conn, task, _, _ = task_context
    engine.configure_system_one({'mode': 'active', 'engine': 'laya',
        'classes': ['budget', 'impediment', 'evidence'], 'max_decisions_per_run': 6,
        'phase_limits': {'budget': 1, 'spec': 1, 'guidance': 2, 'impediment': 1, 'evidence': 1}})
    select_laya(conn, task, http_fixture, ['budget', 'impediment', 'evidence'])
    calls = []
    def reply(cfg, state, questions):
        calls.append(state)
        return {'answers': {'route': {'type': 'choice', 'choice': 'continue_worker', 'confidence': .99}},
                'model': cfg['model'], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
    monkeypatch.setattr(engine, '_request', reply)
    question = {'route': {'type': 'choice', 'criteria': {'continue_worker': 'Continue'}}}
    assert engine.evaluate(conn, task.id, task.current_run_id, 'budget', {'case': 1}, question)
    assert engine.evaluate(conn, task.id, task.current_run_id, 'budget', {'case': 2}, question) is None
    assert engine.evaluate(conn, task.id, task.current_run_id, 'evidence',
                           {'case': 3, 'decision_phase': 'guidance'}, question)
    assert len(calls) == 2


def test_active_policy_never_opts_untagged_worker_in(task_context, monkeypatch):
    conn, task, _, _ = task_context
    engine.configure_system_one({'mode': 'active', 'engine': 'jev'})
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setattr(engine, '_request', lambda *a: pytest.fail('untagged inference'))
    before = conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
    agent = SimpleNamespace(tools=[{'function': {'name': 'read_file'}}])
    assert engine.worker_material_opportunity(agent, [{'name': 'read_file', 'content': 'ok'}], 1) is None
    assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0] == before


def test_native_budget_refuses_insufficient_headroom_without_inference(tmp_path, monkeypatch):
    from scripts.nfos_system_one_eval_budget import guarded_call, BudgetUnavailable
    import urllib.request
    ledger = tmp_path / 'ledger.json'
    ledger.write_text(json.dumps({'initial': {'key_usage': '0', 'total_usage': '0', 'total_credits': '3'},
        'requests': [{'billed': '2.879', 'pending': False}],
        'tariff': {'pricing': {'prompt': '0.000000042', 'completion': '0'}, 'context_length': 32000}}))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, **kw: io.BytesIO(json.dumps({'data':
        {'usage': 2.879} if req.full_url.endswith('/key') else {'total_usage': 2.879, 'total_credits': 3}}).encode()))
    cfg = {'endpoint': 'https://openrouter.ai/api/v1/systemone', 'timeout_seconds': 1,
        'evaluation_budget': {'ledger_path': str(ledger), 'limit_usd': '2.88'}}
    with pytest.raises(BudgetUnavailable, match='budget_or_tariff_limit'):
        guarded_call(cfg, {'questions': {'route': {}}}, 'fixture', lambda *a: pytest.fail('paid call'))
    assert len(json.loads(ledger.read_text())['requests']) == 1


@pytest.mark.parametrize('output,exit_code,expected', [
    ('setupFiles: []; testTimeout: 30000; missing file', 2, None),
    ('usage: nfos-delivery [--timeout TIMEOUT]; command not found', 127, None),
    ('Connection timed out while reading the authorized endpoint', 1, 'recoverable_failure'),
    ('HTTP 503 Service Unavailable', 1, 'recoverable_failure'),
])
def test_transient_trigger_uses_failure_statement_not_config_or_cli_help(
        task_context, http_fixture, monkeypatch, output, exit_code, expected):
    conn, task, _, _ = task_context
    accept(conn, task, 'spec_review')
    engine.configure_system_one({'mode': 'active', 'engine': 'laya',
        'classes': ['budget', 'impediment', 'evidence']})
    select_laya(conn, task, http_fixture, ['budget', 'impediment', 'evidence'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    triggers = []
    monkeypatch.setattr(engine, '_guide_worker',
        lambda agent, conn, task, wf, trigger, failures: triggers.append(trigger))
    agent = SimpleNamespace(_nfos_system_one_seen={
        'stage:' + str(task.current_run_id): d.get_workflow(conn, task.id)['stage']})
    engine.worker_material_opportunity(agent, [{'role': 'tool', 'name': 'terminal',
        'content': json.dumps({'output': output, 'exit_code': exit_code})}], 1)
    assert triggers == ([expected] if expected else [])
