"""Real native executor/SQLite; inference fixtures here are explicitly not live Jev."""
import json
from types import SimpleNamespace
import pytest

from hermes_cli import nfos_jev as engine, nfos_delivery as d
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept
from tests.hermes_cli.test_nfos_jev import http_fixture
from tests.hermes_cli.test_nfos_laya import select_laya


def native_call(monkeypatch, agent, name, args, callback, call_id='attempt'):
    from agent import tool_executor as executor
    # Disable UI/telemetry only. The actual shared dispatch middleware executes.
    for hook in ['_begin_tool_execution', '_emit_terminal_post_tool_call', '_checkpoint_tool_attempt']:
        monkeypatch.setattr(executor, hook, lambda *a, **kw: None)
    return executor._run_agent_tool_execution_middleware(agent,
        function_name=name, function_args=args, effective_task_id='fixture',
        tool_call_id=call_id, execute=callback)


def selected_context(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    accept(conn, task, 'spec_review')
    engine.configure_system_one({'mode': 'active', 'engine': 'laya',
        'classes': ['budget', 'impediment', 'evidence'], 'max_decisions_per_run': 8})
    select_laya(conn, task, http_fixture, ['budget', 'impediment', 'evidence'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    conn.execute('UPDATE tasks SET body=? WHERE id=?', ('Read count.txt and verify count=31', task.id))
    conn.commit()
    monkeypatch.setattr(engine, '_request', lambda cfg, state, questions: {
        'answers': {'route': {'type': 'choice', 'choice': 'acquire_context', 'confidence': .99}},
        'model': cfg['model'], 'usage': {'input_tokens': 12, 'output_tokens': 3}, 'latency_ms': 1})
    agent = SimpleNamespace(tools=[{'function': {'name': n}} for n in ['read_file', 'write_file']],
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)),
        _touch_activity=lambda *a, **kw: None, session_id='enforcement-test')
    engine.worker_material_opportunity(agent,
        [{'role': 'tool', 'name': 'read_file', 'content': 'context'}], 1)
    return conn, task, artifact, agent


def test_selected_route_blocks_unrelated_write_before_side_effect(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    forbidden = artifact.parent / 'unrelated.txt'
    result = native_call(monkeypatch, agent, 'write_file',
        {'path': str(forbidden), 'content': 'unauthorized progress'},
        lambda args: forbidden.write_text(args['content']))
    assert not forbidden.exists(), 'Noncompliant action reached the actual execution callback'
    assert result.blocked


def test_compliant_native_action_discharges_durably_and_refuses_duplicate(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    calls = []
    def read(args):
        calls.append(args['path'])
        return artifact.read_text()
    result = native_call(monkeypatch, agent, 'read_file', {'path': str(artifact)}, read, 'real-read')
    assert not result.blocked and result.result == 'count=31\n'
    assert engine._requirement(conn, task.id)['status'] == 'completed'
    # Simulate a reconstructed agent object. SQLite owns the requirement.
    del agent._nfos_system_one_pending
    again = native_call(monkeypatch, agent, 'read_file', {'path': str(artifact)}, read, 'real-read')
    assert again.blocked and len(calls) == 1


@pytest.mark.parametrize('name,args', [('terminal', {'command': 'echo completed'}),
    ('execute_code', {'code': 'print("completed")'}), ('read_file', {'path': 'unrelated.txt'})])
def test_generic_and_unrelated_targets_cannot_discharge(task_context, http_fixture, monkeypatch, name, args):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    (artifact.parent / 'unrelated.txt').write_text('unrelated')
    result = native_call(monkeypatch, agent, name, args, lambda _: pytest.fail('bypass executed'))
    assert result.blocked and engine._requirement(conn, task.id)['status'] == 'pending'


@pytest.mark.parametrize('response', ['', '{"error":"unavailable"}', '{"result":""}'])
def test_negative_or_empty_canonical_result_is_not_completion(task_context, http_fixture, monkeypatch, response):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    native_call(monkeypatch, agent, 'read_file', {'path': str(artifact)}, lambda _: response)
    requirement = engine._requirement(conn, task.id)
    assert requirement['status'] == 'awaiting_principal'
    assert d.get_decision(conn, requirement['fallback_decision_id'])['status'] == 'pending'


def test_native_transition_cannot_forge_completion(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    with pytest.raises(d.WorkflowError, match='System One action'):
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='skip',
                  state={'system_one_requirement': {'status': 'completed'}})
