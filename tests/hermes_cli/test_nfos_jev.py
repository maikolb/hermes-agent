"""Explicit HTTP fixtures, never live Jev inference. Real NFOS SQLite entrypoints."""
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_jev as jev
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept, save_report


@pytest.fixture
def http_fixture(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.server.calls.append((self.path, self.headers.get('Authorization'), body))
            if self.server.callback:
                self.server.callback()
            if self.server.delay:
                time.sleep(self.server.delay)
            answers = {}
            for key, q in body['questions'].items():
                if q['type'] == 'noul':
                    answers[key] = {'type': 'noul', 'noul': .98}
                elif q['type'] == 'score':
                    answers[key] = {'type': 'score', 'score': 1, 'confidence': .98,
                                    'probabilities': {str(i): float(i == 1) for i in range(len(q['criteria']))},
                                    'legend': {str(i): str(v) for i, v in enumerate(q['criteria'])}}
                else:
                    selected = self.server.choices.get(key, next(iter(q['criteria'])))
                    answers[key] = {'type': 'choice', 'choice': selected, 'confidence': self.server.confidence,
                                   'probabilities': {c: float(c == selected) for c in q['criteria']}}
            data = {'model': getattr(self.server, 'model', 'fixture/jev'), 'answers': answers, 'usage': {'input_tokens': 21, 'output_tokens': 4, 'cost': .000001}}
            if hasattr(self.server, 'model_revision'):
                data['model_revision'] = self.server.model_revision
                data['source_revision'] = getattr(self.server, 'source_revision', 'fixture-source')
            if self.server.malformed:
                data = self.server.malformed
            self.send_response(self.server.code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                self.wfile.write(json.dumps(data).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            self.server.probes.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"count":31}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.calls, server.probes = [], []
    server.choices, server.callback = {}, None
    server.code, server.delay, server.confidence, server.malformed = 200, 0, .99, None
    server.url = 'http://127.0.0.1:' + str(server.server_port)
    original = dict(jev.PROVIDERS)
    for name, (endpoint, model, key) in original.items():
        from urllib.parse import urlsplit
        monkeypatch.setitem(jev.PROVIDERS, name, (server.url + urlsplit(endpoint).path, model, key))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def enable(monkeypatch, uses, provider='typesafe', **options):
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'config.yaml'
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg.setdefault('kanban', {}).setdefault('delivery', {})['jev'] = {
        'enabled': True, 'provider': provider, 'uses': uses, **options}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv(jev.PROVIDERS[provider][2], 'synthetic-fixture-key')


def questions():
    return {'choice': {'type': 'choice', 'instructions': 'Select permitted action', 'criteria': {'inspect': 'Read', 'fallback': 'Use current flow'}},
            'boolean': {'type': 'noul', 'instructions': 'Is context sufficient?'},
            'score': {'type': 'score', 'instructions': 'Rate suitability', 'criteria': ['low', 'high']}}


def test_settings_missing_home_is_readonly(monkeypatch, tmp_path):
    home = tmp_path / 'missing-profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    result = jev.settings()
    assert result['enabled'] is False
    assert result['error'] == 'missing_config'
    assert not home.exists()


def test_settings_rereads_corrupt_config_without_last_known_good(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    enable(monkeypatch, ['spec'])
    assert jev.settings()['enabled'] is True
    path = tmp_path / 'config.yaml'
    path.write_text('kanban: [', encoding='utf-8')
    before = sorted(p.name for p in tmp_path.iterdir())
    result = jev.settings()
    assert result['enabled'] is False
    assert result['error'] == 'invalid_config'
    assert path.read_text(encoding='utf-8') == 'kanban: ['
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize('provider,path,model,key', [
    ('vercel', '/typesafe/v1/systemone', 'typesafe-ai/jev', 'AI_GATEWAY_API_KEY'),
    ('openrouter', '/api/v1/systemone', 'jev-1.13', 'OPENROUTER_API_KEY'),
    ('typesafe', '/v1/systemone', 'jev-1.13.0', 'TYPESAFE_API_KEY'),
])
def test_provider_http_contract(http_fixture, monkeypatch, provider, path, model, key):
    enable(monkeypatch, ['budget'], provider)
    result = jev._request(jev.settings(), {'request': 'Synthetic task'}, questions())
    assert result['answers']['choice']['choice'] == 'inspect'
    assert result['answers']['score']['score'] == 1
    assert result['answers']['boolean']['noul'] == .98
    assert result['usage']['cost'] == .000001
    route, auth, body = http_fixture.calls[0]
    assert route == path and body['model'] == model
    assert auth == 'Bearer synthetic-fixture-key'
    assert set(body) == {'model', 'state', 'questions'}
    assert jev.status()['validated_live'] is False


def test_vercel_cost_normalization():
    result = jev._parse({'model': 'typesafe-ai/jev', 'answers': {'x': {'type': 'noul', 'noul': .9}},
        'usage': {'input_tokens': 21, 'output_tokens': 0}, 'provider_metadata': {'gateway': {'cost': '0.000004'}}},
        {'x': {'type': 'noul'}})
    assert result['usage']['cost'] == .000004


@pytest.mark.parametrize('code', [401, 402, 429, 500, 529])
def test_http_failures_do_not_retry(http_fixture, monkeypatch, code):
    enable(monkeypatch, ['budget'])
    http_fixture.code = code
    assert jev._request(jev.settings(), {}, questions())['reason'] == 'http_' + str(code)
    assert len(http_fixture.calls) == 1


def test_timeout_is_bounded(http_fixture, monkeypatch):
    enable(monkeypatch, ['budget'], timeout_seconds=.1)
    http_fixture.delay = .5
    start = time.monotonic()
    assert 'reason' in jev._request(jev.settings(), {}, questions())
    assert time.monotonic() - start < .4
    assert len(http_fixture.calls) == 1


@pytest.mark.parametrize('bad', [
    {}, {'answers': {}}, {'model': 'jev', 'answers': {'choice': {'type': 'choice', 'choice': 'delete_everything'}}},
    {'model': 'jev', 'answers': {}, 'usage': {'input_tokens': -1, 'output_tokens': 0}},
])
def test_bad_contract_has_no_authority(bad):
    with pytest.raises(ValueError):
        jev._parse(bad, questions())


def test_off_entrypoints_unchanged(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    monkeypatch.setattr(jev, '_post', lambda *a: pytest.fail('disabled feature made network request'))
    before = kb.get_task(conn, task.id)
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    assert kb.get_task(conn, task.id).max_runtime_seconds == before.max_runtime_seconds
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind LIKE 'nfos_jev%'").fetchone()
    assert kb.block_task(conn, task.id, reason='Need technical investigation', kind='transient')
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id)


def test_real_save_spec_records_budget_estimate_and_feedback(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget', 'spec'])
    http_fixture.choices = {'size': 'G', 'c0': 'scope'}
    conn.execute('UPDATE tasks SET max_runtime_seconds=3000, model_override=? WHERE id=?', ('deepseek-pinned', task.id))
    conn.commit()
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    current = kb.get_task(conn, task.id)
    # The estimate is recorded only: the spec author (no size here) keeps the budget.
    assert current.max_runtime_seconds == 3000
    assert current.model_override == 'deepseek-pinned'
    assert 'size' not in json.loads(d.get_spec(conn, task.id)['content'])
    estimate = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_budget_estimate'").fetchone()[0])
    assert estimate['estimated_size'] == 'G' and estimate['applied'] is False and estimate['author_size'] is None
    decision = d.get_decision(conn, kb._nfos_pending_decision(conn, task.id, task.current_run_id))
    assert json.loads(decision['context'])['jev_spec_check']['criteria'] == [{'id': 'C1', 'issue': 'scope'}]
    assert decision['status'] == 'pending' and decision['author'] is None
    calls = len(http_fixture.calls)
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    assert len(http_fixture.calls) == calls  # Materially identical inputs are cached across spec revisions.


def test_stale_response_cannot_apply(task_context, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'])
    def changed(cfg, state, q):
        conn.execute('UPDATE tasks SET instruction_revision=instruction_revision+1 WHERE id=?', (task.id,))
        conn.commit()
        return {'answers': {'size': {'type': 'choice', 'choice': 'G', 'confidence': .99}}, 'model': 'fixture', 'usage': {}}
    monkeypatch.setattr(jev, '_request', changed)
    before = kb.get_task(conn, task.id).max_runtime_seconds
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    assert kb.get_task(conn, task.id).max_runtime_seconds == before
    assert 'size' not in json.loads(d.get_spec(conn, task.id)['content'])
    assert json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])['reason'] == 'stale'


def prepare_probe(conn, task, spec, http_fixture, monkeypatch):
    probe_spec = copy.deepcopy(spec)
    probe_spec['criteria'][0].update(mandatory=True, probe={'kind': 'http', 'url': http_fixture.url + '/count',
                                                          'json_path': 'count', 'expect': {'equals': 31}})
    d.save_spec(conn, task.id, task.current_run_id, probe_spec, author='worker', evidence={'fixture': True})
    accept(conn, task, 'spec_review')
    # Deliberate fixture target instead of production. The actual HTTP probe still runs.
    monkeypatch.setattr(d, '_local_probe_problem', lambda *a, **k: None)
    monkeypatch.setattr(d, '_probe_env_sources', lambda *a: ({}, set()))
    monkeypatch.setattr(d, '_probe_session_cookie', lambda *a: (None, []))
    return probe_spec


def test_block_always_reaches_principal_and_no_probe_can_swallow_it(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    enable(monkeypatch, ['impediment'])
    assert kb.block_task(conn, task.id, reason='Need to measure the configured count', kind='transient', expected_run_id=task.current_run_id)
    current = kb.get_task(conn, task.id)
    assert current.status == 'running' and current.current_run_id == task.current_run_id
    # A passing unrelated measurement is not evidence that this impediment is gone.
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert not http_fixture.probes and not http_fixture.calls
    assert not d._artifact(conn, task.id, 'probe:C1')
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind='nfos_jev_action'").fetchone()


def test_real_save_report_collects_evidence_without_acceptance(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    enable(monkeypatch, ['evidence'])
    save_report(conn, task, artifact)
    assert http_fixture.probes == ['/count']
    assert d._artifact(conn, task.id, 'probe:C1')
    assert kb.get_task(conn, task.id).status == 'running'
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE kind='final_review' AND status='resolved'").fetchone()


@pytest.mark.parametrize('failure', ['invalid_action', 'low_confidence'])
def test_invalid_action_and_low_confidence_fall_back(task_context, http_fixture, monkeypatch, failure):
    conn, task, spec, artifact = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    enable(monkeypatch, ['evidence'])
    if failure == 'invalid_action':
        http_fixture.choices['next'] = 'delete_everything_and_release'
    else:
        http_fixture.confidence = .2
    save_report(conn, task, artifact)
    assert http_fixture.calls and not http_fixture.probes
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert kb.get_task(conn, task.id).status == 'running'


def test_missing_key_and_transaction_do_not_call_network(task_context, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'])
    monkeypatch.delenv('TYPESAFE_API_KEY')
    monkeypatch.setattr(jev, '_post', lambda *a: pytest.fail('unexpected HTTP'))
    assert jev.evaluate(conn, task.id, task.current_run_id, 'budget', {}, questions()) is None
    with kb.write_txn(conn):
        assert jev.evaluate(conn, task.id, task.current_run_id, 'budget', {}, questions()) is None
    assert jev.status()['state'] == 'unavailable'


@pytest.mark.parametrize('case', ['estimate_smaller', 'keep', 'low_confidence', 'http503', 'missing_key'])
def test_budget_estimate_never_overrides_the_spec_author(task_context, http_fixture, monkeypatch, case):
    # BLOCK_LESS9_20260910: whoever writes the spec sets the run budget, with or without System One.
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'])
    conn.execute('UPDATE tasks SET max_runtime_seconds=1200 WHERE id=?', (task.id,)); conn.commit()
    before = kb.get_task(conn, task.id)
    spec = dict(spec, size='G')
    if case == 'estimate_smaller': http_fixture.choices['size'] = 'P'
    elif case == 'keep': http_fixture.choices['size'] = 'keep'
    elif case == 'low_confidence': http_fixture.confidence = .2
    elif case == 'missing_key': monkeypatch.delenv('TYPESAFE_API_KEY')
    else: http_fixture.code = 503
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    after = kb.get_task(conn, task.id)
    assert after.max_runtime_seconds == d.SPEC_SIZE_BUDGET['G']
    assert json.loads(d.get_spec(conn, task.id)['content'])['size'] == 'G'
    assert (after.model_override, after.provider_override, after.reasoning_effort) == (before.model_override, before.provider_override, before.reasoning_effort)
    estimate = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_budget_estimate'").fetchone()
    if case == 'estimate_smaller':
        recorded = json.loads(estimate[0])
        assert recorded['estimated_size'] == 'P' and recorded['author_size'] == 'G' and recorded['applied'] is False
    else:
        assert estimate is None


def test_disabled_budget_keeps_existing_native_size_behavior(task_context, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'], enabled=False)
    conn.execute('UPDATE tasks SET max_runtime_seconds=1200 WHERE id=?', (task.id,)); conn.commit()
    d.save_spec(conn, task.id, task.current_run_id, dict(spec, size='G'), author='worker', evidence={'fixture': True})
    assert kb.get_task(conn, task.id).max_runtime_seconds == d.SPEC_SIZE_BUDGET['G']


def test_context_secrets_are_redacted(http_fixture, monkeypatch):
    enable(monkeypatch, ['budget'])
    monkeypatch.setenv('FIXTURE_PASSWORD', 'private-fixture-value')
    jev._request(jev.settings(), {'request': 'use private-fixture-value with Bearer abcdefghi',
                                 'url': 'https://user:pass@host/path?token=SECRET', 'password': 'hidden'}, questions())
    sent = json.dumps(http_fixture.calls[0][2])
    assert 'private-fixture-value' not in sent and 'abcdefghi' not in sent and 'user:pass' not in sent and 'hidden' not in sent


def test_cards_do_not_share_decision_cache(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'])
    result = jev.evaluate(conn, task.id, task.current_run_id, 'budget', {'same': True}, questions(), reuse=True)
    assert result
    assert jev.evaluate(conn, 'nonexistent-other-card', task.current_run_id, 'budget', {'same': True}, questions(), reuse=True) is None
    assert len(http_fixture.calls) == 1



def test_no_http_under_dispatch_lock(task_context, monkeypatch):
    conn, task, _, _ = task_context
    enable(monkeypatch, ['budget'])
    monkeypatch.setattr(jev, '_request', lambda *a: pytest.fail('network under dispatcher lock'))
    with kb._dispatch_tick_lock(kb.kanban_db_path()) as held:
        assert held
        assert jev.evaluate(conn, task.id, task.current_run_id, 'budget', {}, questions()) is None
    assert not kb._NFOS_DISPATCH_LOCK_HELD.get()


def test_http_runs_without_sqlite_write_lock(task_context, http_fixture, monkeypatch):
    import sqlite3
    conn, task, _, _ = task_context
    enable(monkeypatch, ['budget'])
    dbpath = conn.execute('PRAGMA database_list').fetchone()[2]
    observed = []
    def write_elsewhere():
        with sqlite3.connect(dbpath, timeout=.1) as other:
            other.execute('BEGIN IMMEDIATE')
            observed.append(True)
            other.rollback()
    http_fixture.callback = write_elsewhere
    assert jev.evaluate(conn, task.id, task.current_run_id, 'budget', {}, questions())
    assert observed == [True]


def test_low_confidence_has_no_budget_effect(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['budget'])
    http_fixture.confidence = .2
    http_fixture.choices['size'] = 'G'
    before = kb.get_task(conn, task.id).max_runtime_seconds
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    assert kb.get_task(conn, task.id).max_runtime_seconds == before
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind='nfos_jev_budget_estimate'").fetchone()


def test_jev_does_not_add_review_in_record_mode(task_context, http_fixture, monkeypatch):
    from hermes_cli import nfos_principal_review as review
    conn, task, spec, _ = task_context
    spec = prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    enable(monkeypatch, ['spec'])
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    count = conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0]
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    assert conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0] == count
    assert not http_fixture.calls


def test_failed_measurement_is_not_pass_or_retried(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    probe_spec = prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    probe_spec['criteria'][0]['probe']['expect'] = {'equals': 99}
    d.save_spec(conn, task.id, task.current_run_id, probe_spec, author='worker', evidence={'fixture': True})
    accept(conn, task, 'spec_review')
    enable(monkeypatch, ['evidence'])
    save_report(conn, task, artifact)
    assert json.loads(d._artifact(conn, task.id, 'probe:C1')['content'])['state'] == 'FAIL'
    assert d.mandatory_pending(conn, task.id)[0]['status'] == 'FAIL'
    assert kb.get_task(conn, task.id).status == 'running'
    receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev_action' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert receipt['principal_calls_saved'] == 0 and receipt['fallback_reason'] == 'probe_failed_or_inconclusive'
    save_report(conn, task, artifact)
    assert http_fixture.probes == ['/count']



def test_context_budget_falls_back_instead_of_silently_truncating(http_fixture, monkeypatch):
    enable(monkeypatch, ['budget'])
    result = jev._request(jev.settings(), {'request': 'x' * 50000}, questions())
    assert result['reason'] == 'context_too_large'
    assert not http_fixture.calls


def test_scope_feedback_uses_existing_changes_loop(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'])
    http_fixture.choices['c0'] = 'scope'
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})
    decision = kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    feedback = json.loads(d.get_decision(conn, decision)['context'])['jev_spec_check']
    assert feedback['criteria'][0]['id'] == 'C1'
    d.resolve_decision(conn, decision, action='changes', answer='Revise C1 against the requested count', author='Principal')
    assert d.get_decision(conn, decision)['action'] == 'changes'
    changed = copy.deepcopy(spec)
    changed['criteria'][0]['text'] = 'The requested count is 31 at the specified destination'
    http_fixture.choices['c0'] = 'aligned'
    previous = d.get_spec(conn, task.id)['revision']
    d.save_spec(conn, task.id, task.current_run_id, changed, author='worker', evidence={'fixture': True})
    assert d.get_spec(conn, task.id)['revision'] == previous + 1
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


def test_two_owned_cards_have_separate_cache(task_context, http_fixture, monkeypatch):
    import os
    conn, task, _, _ = task_context
    request_id = d.receive_request(conn, source={'platform': 'fixture', 'chat_id': 'other', 'thread_id': 'other', 'message_id': '2'},
        text='Other project request', project={'board': 'fixture', 'profile': 'default', 'delivery_type': 'report'})
    claim = d.reserve_request(conn, capacity=5)
    other = d.bootstrap_card(conn, request_id, claim['claim_token'], pid=os.getpid())
    enable(monkeypatch, ['budget'])
    for current in (task, other):
        assert jev.evaluate(conn, current.id, current.current_run_id, 'budget', {'same': True}, questions(), reuse=True)
    assert len(http_fixture.calls) == 2
    for current in (task, other):
        assert jev.evaluate(conn, current.id, current.current_run_id, 'budget', {'same': True}, questions(), reuse=True)
    assert len(http_fixture.calls) == 2



def test_doctor_reads_explicit_smoke_receipt_without_network(http_fixture, monkeypatch, capsys):
    import sys
    enable(monkeypatch, ['budget'])
    assert jev.status()['state'] == 'configured_not_validated_live'
    monkeypatch.setattr(sys, 'argv', ['nfos_jev', 'smoke', '--live'])
    assert jev.main() == 0  # Explicit HTTP fixture, NOT a live provider validation.
    assert jev.status()['state'] == 'validated_live'
    assert len(http_fixture.calls) == 1
    assert 'synthetic-fixture-key' not in jev._smoke_path().read_text()
    monkeypatch.setenv('TYPESAFE_API_KEY', 'different-fixture-key')
    assert jev.status()['state'] == 'configured_not_validated_live'
    assert len(http_fixture.calls) == 1
    assert 'synthetic-fixture-key' not in capsys.readouterr().out


def test_tool_reports_the_principal_impediment_not_a_recovery(task_context, http_fixture, monkeypatch):
    from tools import kanban_tools as tools
    conn, task, spec, _ = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    enable(monkeypatch, ['impediment'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    result = json.loads(tools._handle_block({'task_id': task.id, 'kind': 'transient', 'reason': 'Need the configured count'}))
    assert result.get('blocked') is False, result
    assert result['status'] == 'running'
    assert result['decision_id'] == kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert 'recovery' not in result
    assert not http_fixture.probes
