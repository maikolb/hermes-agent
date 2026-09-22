"""Real native executor/SQLite; inference fixtures here are explicitly not live Jev.

Worker guidance is advisory: it never blocks a tool, never gates a native
transition and never opens a Principal question on its own.
"""
import json
from types import SimpleNamespace

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


def reply(route, seen=None):
    def request(cfg, state, questions):
        if seen is not None:
            seen.append(questions)
        return {'answers': {'route': {'type': 'choice', 'choice': route, 'confidence': .99}},
                'model': cfg['model'], 'usage': {'input_tokens': 12, 'output_tokens': 3}, 'latency_ms': 1}
    return request


def selected_context(task_context, http_fixture, monkeypatch, route='acquire_context', tools=('read_file', 'write_file'),
                     stage=None, seen=None):
    conn, task, spec, artifact = task_context
    accept(conn, task, 'spec_review')
    engine.configure_system_one({'mode': 'active', 'engine': 'laya',
        'classes': ['budget', 'impediment', 'evidence'], 'max_decisions_per_run': 8})
    select_laya(conn, task, http_fixture, ['budget', 'impediment', 'evidence'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    if stage:
        d.advance(conn, task.id, task.current_run_id, stage, next_action='Measure registered result')
    conn.execute('UPDATE tasks SET body=? WHERE id=?', ('Read count.txt and verify count=31', task.id))
    conn.commit()
    monkeypatch.setattr(engine, '_request', reply(route, seen))
    agent = SimpleNamespace(tools=[{'function': {'name': n}} for n in tools],
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)),
        _touch_activity=lambda *a, **kw: None, session_id='advisory-test')
    receipt = engine.worker_material_opportunity(agent,
        [{'role': 'tool', 'name': 'read_file', 'content': 'context'}], 1)
    return conn, task, artifact, agent, receipt


def guidance(conn):
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def test_advice_never_blocks_a_different_worker_tool(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent, receipt = selected_context(task_context, http_fixture, monkeypatch)
    assert receipt and guidance(conn)['route'] == 'acquire_context'
    assert guidance(conn)['advisory'] is True and guidance(conn)['enforced'] is False
    other = artifact.parent / 'notes.txt'
    result = native_call(monkeypatch, agent, 'write_file', {'path': str(other), 'content': 'worker choice'},
        lambda args: other.write_text(args['content']))
    assert not result.blocked and other.read_text() == 'worker choice'


def test_native_transitions_carry_no_system_one_gate(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent, _ = selected_context(task_context, http_fixture, monkeypatch)
    assert guidance(conn)
    try:
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='continue')
    except d.WorkflowError as refusal:
        # The fixture rewrote the card body; only that unrelated native contract may refuse.
        assert 'System One' not in str(refusal)
    else:
        assert d.get_workflow(conn, task.id)['stage'] == 'implement'


def test_choosing_escalation_opens_no_principal_question(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent, receipt = selected_context(task_context, http_fixture, monkeypatch, route='escalate_existing')
    assert receipt and guidance(conn)['route'] == 'escalate_existing'
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE kind='impediment'").fetchone()
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='pending'").fetchone()
    assert getattr(agent, '_nfos_system_one_pending', None) is None


def test_python_repository_keeps_check_and_continue_routes_at_verify(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    root = artifact.parent
    for name in ('package.json',):
        (root / name).unlink(missing_ok=True)
    (root / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
    seen = []
    conn, task, artifact, agent, receipt = selected_context(task_context, http_fixture, monkeypatch,
        route='run_relevant_checks', tools=('read_file', 'terminal'), stage='verify', seen=seen)
    offered = set(seen[-1]['route']['criteria'])
    assert {'run_relevant_checks', 'continue_worker'} <= offered
    record = guidance(conn)
    assert record['trigger'] == 'next_evidence' and record['route'] == 'run_relevant_checks'
    assert record['commands'] == [] and record['tools'] == ['terminal']  # no invented command
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE kind='impediment'").fetchone()


def test_exact_route_observation_requires_the_bound_target(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent, _ = selected_context(task_context, http_fixture, monkeypatch)
    receipt = guidance(conn)
    assert receipt['targets'].get(str(artifact))
    unrelated = artifact.parent / 'unrelated.txt'
    unrelated.write_text('unrelated')

    def messages(path):
        return [{'role': 'assistant', 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': path})}}]},
                {'role': 'tool', 'name': 'read_file', 'tool_call_id': 't1', 'content': 'unrelated'}]

    agent._nfos_system_one_pending = dict(receipt)
    for _ in range(3):
        engine._observe_guidance(agent, conn, task, messages(str(unrelated)), 1)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance_consumed' ORDER BY id DESC LIMIT 1").fetchone()
    record = json.loads(row[0])
    assert record['acknowledgment'] == 'not_observed'
    assert record['observation'] == 'different_tool_or_no_action'
    agent._nfos_system_one_pending = dict(receipt)
    engine._observe_guidance(agent, conn, task, messages(str(artifact)), 1)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance_consumed' ORDER BY id DESC LIMIT 1").fetchone()
    matched = json.loads(row[0])
    assert matched['acknowledgment'] == 'implicit_tool_match'
    assert matched['observation'] == 'matching_tool_succeeded'
    assert matched['principal_calls_saved'] == 0


def test_category_route_is_observed_by_tool_category_only(task_context, http_fixture, monkeypatch):
    conn, task, artifact, agent, _ = selected_context(task_context, http_fixture, monkeypatch)
    receipt = dict(guidance(conn), route='run_relevant_checks', tools=['terminal'], targets={}, commands=[])
    agent._nfos_system_one_pending = receipt
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 't9', 'type': 'function', 'function': {
                    'name': 'terminal', 'arguments': json.dumps({'command': 'python -m pytest -q'})}}]},
                {'role': 'tool', 'name': 'terminal', 'tool_call_id': 't9', 'content': json.dumps({'output': '3 passed', 'exit_code': 0})}]
    engine._observe_guidance(agent, conn, task, messages, 1)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_guidance_consumed' ORDER BY id DESC LIMIT 1").fetchone()
    record = json.loads(row[0])
    assert record['acknowledgment'] == 'category_tool_match'
    assert record['observation'] == 'matching_tool_succeeded'
    assert record['semantic_outcome'] == 'NOT_PROVEN'
