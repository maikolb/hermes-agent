"""Native isolated SQLite + real batch finalization; HTTP replies are explicit fixtures."""
import json
import os
import time
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import nfos_jev as engine, nfos_delivery as d, kanban_db as kb
from tests.hermes_cli.test_nfos_jev import http_fixture, prepare_probe, questions
from tests.hermes_cli.test_nfos_laya import select_laya
from tests.hermes_cli.test_nfos_principal_acceptance import task_context


def configure(mode='active', classes=None, **extra):
    return engine.configure_system_one({'mode': mode, 'engine': 'laya',
        'classes': classes or ['budget', 'impediment', 'evidence'],
        'timeout_seconds': 1, 'max_decisions_per_run': 4, **extra})


def prepare(task_context, http_fixture, monkeypatch, mode='active'):
    conn, task, spec, _ = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    configure(mode)
    select_laya(conn, task, http_fixture, ['budget', 'impediment', 'evidence', 'spec'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    d.advance(conn, task.id, task.current_run_id, 'verify', next_action='Measure registered result')
    return conn, task


def test_native_batch_without_executable_evidence_uses_existing_fallback_once(task_context, http_fixture, monkeypatch):
    from agent import tool_executor
    conn, task = prepare(task_context, http_fixture, monkeypatch)
    before = conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0]
    messages = [{'role': 'tool', 'name': 'read_file', 'tool_call_id': 'synthetic', 'content': '{"result":"stage prepared"}'}]
    agent = SimpleNamespace(_interrupt_requested=False, _apply_pending_steer_to_tool_results=lambda *a: None,
        tools=[{'function': {'name': 'read_file'}}])
    monkeypatch.setattr(tool_executor, '_budget_for_agent', lambda a: tool_executor.DEFAULT_BUDGET)
    monkeypatch.setattr(tool_executor, 'get_active_env', lambda *a: None)
    tool_executor.execute_tool_calls_segmented(agent, SimpleNamespace(tool_calls=[object()]), messages, 'isolated', segments=[])
    assert http_fixture.probes == []  # guidance is not an automatic duplicate probe
    assert 'system_one' in messages[-1]['content']
    assert conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0] == before + 1
    assert engine._requirement(conn, task.id)['status'] == 'awaiting_principal'
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance'")]
    assert rows[-1]['principal_calls_saved'] == 0
    count = len(http_fixture.calls)
    tool_executor._system_one_after_batch(agent, messages, 1)
    assert len(http_fixture.calls) == count  # cheap repeated reads are not decision opportunities


def test_shadow_returns_before_http_and_never_executes(task_context, http_fixture, monkeypatch):
    conn, task = prepare(task_context, http_fixture, monkeypatch, mode='shadow')
    http_fixture.delay = .3
    started = time.monotonic()
    assert engine.worker_material_opportunity(SimpleNamespace(), [{'role':'tool','content':'ok'}], 1) is None
    assert time.monotonic() - started < .2
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' AND json_extract(payload,'$.decision_mode')='shadow'").fetchone()
        if row:
            break
        time.sleep(.02)
    assert row
    assert json.loads(row[0])['used_engine'] is None
    assert not http_fixture.probes


def test_material_failures_are_bounded_and_not_commands(task_context, http_fixture, monkeypatch):
    conn, task = prepare(task_context, http_fixture, monkeypatch)
    calls = []
    monkeypatch.setattr(engine, '_guide_worker', lambda *a: calls.append(a))
    agent = SimpleNamespace(_nfos_system_one_seen={'stage:'+str(task.current_run_id): 'verify'})
    message = [{'role':'tool','name':'terminal','content':'{"exit_code":1,"error":"connection reset"}'}]
    for _ in range(5):
        engine.worker_material_opportunity(agent, message, 1)
    assert [c[4] for c in calls] == ['recoverable_failure', 'stagnant_retry']
    assert all(c[5] == [('terminal', 'connection reset')] for c in calls)


def test_off_and_interrupt_do_not_call_engine(task_context, http_fixture, monkeypatch):
    from agent.tool_executor import _system_one_after_batch
    conn, task = prepare(task_context, http_fixture, monkeypatch)
    monkeypatch.setattr(engine, 'collect_missing_probe', lambda *a, **kw: pytest.fail('unexpected decision'))
    _system_one_after_batch(SimpleNamespace(_interrupt_requested=True), [{'content':'timeout'}], 1)
    configure('off')
    assert engine.worker_material_opportunity(SimpleNamespace(), [{'content':'timeout'}], 1) is None


def test_unvalidated_spec_excluded_and_policy_drift_fails_closed(task_context, http_fixture):
    conn, task, spec, _ = task_context
    configure(classes=['spec','budget'])
    select_laya(conn, task, http_fixture, ['spec','budget'])
    assert not engine.primary_enabled(conn, task.id)
    assert engine.primary_spec_review(conn, task.id, task.current_run_id) is None
    configure('shadow', classes=['spec','budget'])
    assert not engine.settings(conn, task.id)['enabled']  # active card never silently migrates policy
    assert engine.settings(conn, task.id)['error'] == 'selection_config_changed'


def test_permission_removed_before_model_selection(task_context, http_fixture, monkeypatch):
    conn, task = prepare(task_context, http_fixture, monkeypatch)
    monkeypatch.setattr(d, '_local_probe_problem', lambda *a, **kw: 'outside authorized target')
    assert engine.collect_missing_probe(conn, task.id, task.current_run_id, use='evidence') is None
    assert not http_fixture.calls and not http_fixture.probes


def test_per_run_limit_keeps_native_fallback(task_context, http_fixture):
    conn, task, _, _ = task_context
    configure(classes=['budget'], max_decisions_per_run=1)
    select_laya(conn, task, http_fixture, ['budget'])
    assert engine.evaluate(conn, task.id, task.current_run_id, 'budget', {'input':1}, questions())
    assert engine.evaluate(conn, task.id, task.current_run_id, 'budget', {'input':2}, questions()) is None
    assert len(http_fixture.calls) == 1
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert payload['fallback_reason'] == 'decision_limit'


@pytest.mark.parametrize('bad', [{'mode':'exception'}, {'classes':['delete']}, {'timeout_seconds':0}, {'max_decisions_per_run':100}, {'api_key':'secret'}])
def test_config_rejects_unsupported_authority(bad):
    with pytest.raises(ValueError):
        configure(**bad)


def test_shadow_can_observe_ineligible_spec_without_acceptance(task_context, http_fixture):
    conn, task, spec, _ = task_context
    configure('shadow', classes=['spec'])
    select_laya(conn, task, http_fixture, ['spec'])
    assert not engine.primary_enabled(conn, task.id)
    result = engine.spec_decisions(conn, task.id, task.current_run_id, spec)
    assert not any(result.values())
    deadline = time.monotonic() + 3
    while not http_fixture.calls and time.monotonic() < deadline:
        time.sleep(.02)
    assert http_fixture.calls
    assert engine.primary_spec_review(conn, task.id, task.current_run_id) is None
    # Complete the observer before the isolated fixture/home is closed.
    assert engine._SHADOW_SLOT.acquire(timeout=3)
    engine._SHADOW_SLOT.release()


def test_provider_key_uses_canonical_protected_file(task_context, monkeypatch):
    from hermes_constants import get_hermes_home
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    path = get_hermes_home() / '.env'
    path.write_text('OPENROUTER_API_KEY=synthetic-key-only-for-test\n')
    assert engine._provider_key('OPENROUTER_API_KEY') == 'synthetic-key-only-for-test'
    assert 'synthetic-key-only-for-test' not in json.dumps(engine.system_one_status())


def test_editing_laya_classes_preserves_jev_four_uses(task_context):
    configure(classes=['budget', 'evidence'])
    assert engine.settings(engine='laya')['uses'] == ['budget', 'evidence']
    assert set(engine.settings(engine='jev')['uses']) == {'budget','spec','impediment','evidence'}
