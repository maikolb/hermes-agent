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


def dead_pid():
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, '-c', ''])
    proc.wait()
    return proc.pid


def test_interrupted_execution_is_reconciled_instead_of_freezing_every_tool(task_context, http_fixture, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    args = {'path': str(artifact)}
    blocked, ticket = engine.before_worker_tool(agent, 'read_file', args, 'attempt-a')
    assert blocked is None and ticket
    assert engine._requirement(conn, task.id)['status'] == 'executing'
    # While the reservation is live, a second dispatch cannot produce a side effect.
    other = native_call(monkeypatch, agent, 'read_file', args,
        lambda a: pytest.fail('duplicate side effect'), 'attempt-b')
    assert other.blocked and engine._requirement(conn, task.id)['status'] == 'executing'
    # The reserving worker process dies mid-tool (crash/OOM/stop): the dead pid stays.
    pid = dead_pid()
    assert not kb._pid_alive(pid)
    requirement = engine._requirement(conn, task.id)
    requirement['execution_pid'] = pid
    engine._save_requirement(conn, task, requirement, 'fixture_crash')
    # A noncompliant action is still refused before its side effect...
    forbidden = artifact.parent / 'unrelated.txt'
    bad = native_call(monkeypatch, agent, 'write_file',
        {'path': str(forbidden), 'content': 'unauthorized progress'},
        lambda a: pytest.fail('bypass executed'), 'after-restart')
    assert bad.blocked and not forbidden.exists()
    # ...and the validated action can be re-issued to completion, not deadlocked.
    calls = []

    def read(a):
        calls.append(a['path'])
        return artifact.read_text()

    done = native_call(monkeypatch, agent, 'read_file', args, read, 'restart-read')
    assert not done.blocked and len(calls) == 1
    requirement = engine._requirement(conn, task.id)
    assert requirement['status'] == 'completed' and requirement['typed_action_executed'] is True
    assert requirement['interrupted_attempts'] == 1


def test_escalated_requirement_releases_native_loop_and_transitions(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    forbidden = artifact.parent / 'unrelated.txt'
    attempts = [native_call(monkeypatch, agent, 'write_file',
        {'path': str(forbidden), 'content': 'unauthorized progress'},
        lambda a: pytest.fail('bypass executed'), 'attempt-' + str(i)) for i in range(3)]
    assert all(attempt.blocked for attempt in attempts) and not forbidden.exists()
    requirement = engine._requirement(conn, task.id)
    assert requirement['status'] == 'awaiting_principal'
    assert requirement['fallback_reason'] == 'noncompliant_attempt_limit'
    assert d.get_decision(conn, requirement['fallback_decision_id'])['status'] == 'pending'
    # Scoped fallback keeps the sole native loop live: other canonical actions and
    # native transitions proceed under prior semantics without forging completion.
    notes = artifact.parent / 'notes.txt'
    notes.write_text('notes')
    result = native_call(monkeypatch, agent, 'read_file', {'path': str(notes)},
        lambda a: notes.read_text(), 'native-read')
    assert not result.blocked and result.result == 'notes'
    assert engine._requirement(conn, task.id)['status'] == 'awaiting_principal'
    # System One released the transition gate. The card's unrelated native
    # instruction-revision contract (the fixture rewrote the card body) is the
    # only refusal that may remain, never the escalated requirement.
    assert engine.require_worker_action(conn, task.id) is None
    try:
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='continue')
    except d.WorkflowError as refusal:
        assert 'System One' not in str(refusal)
    else:
        assert d.get_workflow(conn, task.id)['stage'] == 'implement'
    assert engine._requirement(conn, task.id)['typed_action_executed'] is not True


def test_guidance_consumed_record_requires_the_bound_target_not_only_the_tool_name(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent = selected_context(task_context, http_fixture, monkeypatch)
    requirement = dict(engine._requirement(conn, task.id), enforced=False)
    unrelated = artifact.parent / 'unrelated.txt'
    unrelated.write_text('unrelated')

    def messages(path):
        return [{'role': 'assistant', 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': path})}}]},
                {'role': 'tool', 'name': 'read_file', 'tool_call_id': 't1', 'content': 'unrelated'}]

    agent._nfos_system_one_pending = dict(requirement)
    for _ in range(3):
        engine._observe_guidance(agent, conn, task, messages(str(unrelated)), 1)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance_consumed' ORDER BY id DESC LIMIT 1").fetchone()
    record = json.loads(row[0])
    assert record['acknowledgment'] == 'not_observed'
    assert record['observation'] == 'different_tool_or_no_action'
    agent._nfos_system_one_pending = dict(requirement)
    engine._observe_guidance(agent, conn, task, messages(str(artifact)), 1)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance_consumed' ORDER BY id DESC LIMIT 1").fetchone()
    matched = json.loads(row[0])
    assert matched['acknowledgment'] == 'implicit_tool_match'
    assert matched['observation'] == 'matching_tool_succeeded'
