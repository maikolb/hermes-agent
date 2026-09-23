"""Independent regression checks for escalation budgets and goal-loop handoff."""
import json
import os
from types import SimpleNamespace

import pytest

from agent.iteration_budget import IterationBudget
from hermes_cli import kanban_db as kb, nfos_principal_review as review, goals
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept


@pytest.fixture
def resumed(task_context, monkeypatch):
    conn, task, _, _ = task_context
    accept(conn, task, 'spec_review')
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *args: False)
    first = task.current_run_id
    with kb.write_txn(conn):
        state = json.loads(review.d.get_workflow(conn, task.id)['state_json'])
        state['worker_escalation'] = dict(level=1, model='gpt-5.6-luna', reasoning_effort='max',
            reason='Independent budget fixture', source_run_id=first, first_run_id=first, status='pending')
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (json.dumps(state), task.id))
        assert review.reclaim_escalation(conn, task.id)
        conn.execute('UPDATE task_runs SET started_at=100,ended_at=160,metadata=? WHERE id=?',
                     (json.dumps({'worker_session_id': 'retained-session', 'escalation_usage': {'iterations': 7, 'turns': 2}}), first))
    current = kb.claim_task(conn, task.id)
    assert current is not None
    kb._set_worker_pid(conn, task.id, os.getpid())
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=1000 WHERE id=?', (current.current_run_id,))
        conn.execute('UPDATE tasks SET max_runtime_seconds=100,goal_max_turns=3 WHERE id=?', (task.id,))
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(current.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', current.claim_lock)
    return conn, kb.get_task(conn, task.id), first


def test_usage_excludes_between_run_wait_and_current_attempt(resumed):
    conn, task, first = resumed
    assert review.escalation_usage(conn, task.id, task.current_run_id) == {'seconds': 60, 'iterations': 7, 'turns': 2}
    assert json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (first,)).fetchone()[0])['worker_session_id'] == 'retained-session'


def test_resumed_iteration_allowance_is_deducted_once_and_cannot_be_spent_again(resumed):
    conn, task, first = resumed
    agent = SimpleNamespace(model='gpt-5.6-luna', reasoning_config={'effort': 'max'}, iteration_budget=IterationBudget(10))
    assert not review.worker_checkpoint(agent, 'turn-1')
    assert agent.iteration_budget.max_total == 3
    assert agent.iteration_budget.consume()
    assert agent.iteration_budget.consume()
    assert not review.worker_checkpoint(agent, 'turn-1')
    assert agent.iteration_budget.max_total == 3
    usage = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])['escalation_usage']
    assert usage['turns'] == 1 and usage['iterations'] == 2
    assert agent.iteration_budget.consume()
    assert not agent.iteration_budget.consume()
    assert kb.get_task(conn, task.id).max_runtime_seconds == 100


def test_runtime_limit_includes_previous_active_time_but_not_wait(resumed, monkeypatch):
    conn, task, first = resumed
    monkeypatch.setattr(kb.time, 'time', lambda: 1039)
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    signals = []
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: signals.append(args)) == []
    monkeypatch.setattr(kb.time, 'time', lambda: 1041)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: signals.append(args)) == [task.id]
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='timed_out' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
    assert json.loads(event[0])['elapsed_seconds'] == 101
    assert len(signals) == 1 and signals[0][0] == os.getpid()  # Captured callback only; no real signal.


def test_goal_loop_with_prior_turns_does_not_get_a_fresh_allowance(resumed, monkeypatch):
    import cli
    conn, task, first = resumed
    calls, blocks = [], []
    monkeypatch.setattr(goals, 'judge_goal', lambda *args: ('continue', 'Still open', False, False, False))
    monkeypatch.setattr(kb, 'block_task', lambda *args, **kwargs: blocks.append(kwargs))
    agent = SimpleNamespace(run_conversation=lambda **kwargs: calls.append(kwargs) or {'final_response': ''})
    cli._run_kanban_goal_loop_q(SimpleNamespace(agent=agent, conversation_history=[], session_id='retained-session'), 'First resumed turn')
    assert calls == []  # 2 previous turns + the first resumed turn consume the allowance of 3.
    assert len(blocks) == 1


def test_goal_loop_yield_during_later_turn_preserves_claim_without_block(resumed, monkeypatch):
    import cli
    conn, task, first = resumed
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET goal_max_turns=8 WHERE id=?', (task.id,))
    calls, blocks, judged = [], [], []
    monkeypatch.setattr(goals, 'judge_goal', lambda *args: judged.append(args) or ('continue', 'One correction', False, False, False))
    monkeypatch.setattr(kb, 'block_task', lambda *args, **kwargs: blocks.append(kwargs))
    agent = SimpleNamespace()
    def turn(**kwargs):
        calls.append(kwargs)
        agent._nfos_escalation_yield = True
        return {'final_response': ''}
    agent.run_conversation = turn
    cli._run_kanban_goal_loop_q(SimpleNamespace(agent=agent, conversation_history=[], session_id='retained-session'), 'First turn')
    assert len(calls) == len(judged) == 1
    assert blocks == []
    after = kb.get_task(conn, task.id)
    assert after.current_run_id == task.current_run_id and after.claim_lock == task.claim_lock
    assert after.status == 'running'


def test_last_consumed_iteration_is_preserved_when_tool_completes_run(resumed):
    conn, task, first = resumed
    agent = SimpleNamespace(model='gpt-5.6-luna',reasoning_config={'effort':'max'},iteration_budget=IterationBudget(10))
    assert review.worker_checkpoint(agent,'final-turn') is False
    assert agent.iteration_budget.consume()
    review.record_worker_iteration(agent)
    with kb.write_txn(conn): kb._end_run(conn,task.id,outcome='completed',status='done')
    usage=json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',(task.current_run_id,)).fetchone()[0])['escalation_usage']
    assert usage['iterations']==1
    # No later loop checkpoint exists after a terminal tool closes this run.
    assert kb.get_task(conn,task.id).current_run_id is None


def test_explicit_grant_restores_exhausted_card_without_resetting_history(resumed, monkeypatch):
    conn, task, first = resumed
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                     (json.dumps({'worker_session_id': 'retained-session', 'escalation_usage': {'iterations': 400}}), first))
    agent = SimpleNamespace(model='gpt-5.6-luna', reasoning_config={'effort': 'max'}, iteration_budget=IterationBudget(400))
    review.worker_checkpoint(agent)
    assert not agent.iteration_budget.consume()
    with kb.write_txn(conn):
        kb._end_run(conn, task.id, outcome='blocked', status='blocked')
        conn.execute("UPDATE tasks SET status='blocked',claim_lock=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?", (task.id,))
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    snapshot = [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
    payload = dict(grant_id='owner-approval-1', iterations=20, actor='Principal', reason='Owner requested bounded continuation',
                   expected_run_id=task.current_run_id, expected_instruction_revision=task.instruction_revision)
    receipt = review.grant_iteration_budget(conn, task.id, **payload)
    assert review.grant_iteration_budget(conn, task.id, **payload) == receipt
    assert snapshot == [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
    assert kb.get_task(conn, task.id).status == 'blocked'
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task.id,))
    current = kb.claim_task(conn, task.id)
    assert current
    kb._set_worker_pid(conn, task.id, os.getpid())
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(current.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', current.claim_lock)
    agent = SimpleNamespace(model='gpt-5.6-luna', reasoning_config={'effort': 'max'}, iteration_budget=IterationBudget(400))
    review.worker_checkpoint(agent)
    assert agent.iteration_budget.max_total == 20
    assert all(agent.iteration_budget.consume() for _ in range(20))
    assert not agent.iteration_budget.consume()
    review.worker_checkpoint(agent)
    assert agent.iteration_budget.max_total == 20
    meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (current.current_run_id,)).fetchone()[0])
    assert meta['iteration_budget']['prior_iterations'] == 400
    assert meta['iteration_budget']['granted_iterations'] == 20
    assert meta['iteration_budget']['effective_iterations'] == 20
    # Another attempt cannot spend those same granted iterations again.
    assert review.escalation_usage(conn, task.id, current.current_run_id + 1)['iterations'] == 420
    assert 400 + receipt['granted_iterations'] - review.escalation_usage(conn, task.id, current.current_run_id + 1)['iterations'] == 0


@pytest.mark.parametrize('runtime_seconds', [0, 3600])
@pytest.mark.parametrize('mode', ['worker', 'child', 'active', 'stale_run', 'stale_instruction', 'zero', 'bool', 'replay_conflict', 'cleanup'])
def test_grant_rejects_unsafe_or_stale_requests(resumed, monkeypatch, mode, runtime_seconds):
    conn, task, first = resumed
    payload = dict(grant_id='explicit-owner-request', iterations=20, runtime_seconds=runtime_seconds, actor='Principal', reason='Bounded repair',
                   expected_run_id=task.current_run_id, expected_instruction_revision=task.instruction_revision)
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    if mode != 'active':
        with kb.write_txn(conn):
            kb._end_run(conn, task.id, outcome='blocked', status='blocked')
            conn.execute("UPDATE tasks SET status='blocked',claim_lock=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?", (task.id,))
    if mode == 'worker': monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    if mode == 'child': monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1')
    if mode == 'stale_run': payload['expected_run_id'] = first
    if mode == 'stale_instruction': payload['expected_instruction_revision'] += 1
    if mode == 'zero': payload['iterations'] = 0
    if mode == 'bool': payload['iterations'] = True
    if mode == 'cleanup': monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *args: True)
    if mode == 'replay_conflict':
        review.grant_iteration_budget(conn, task.id, **payload)
        payload['iterations'] += 1
    count = len(review.iteration_grants(conn, task.id))
    with pytest.raises(review.d.WorkflowError):
        review.grant_iteration_budget(conn, task.id, **payload)
    assert len(review.iteration_grants(conn, task.id)) == count


def test_cli_grant_and_deepseek_pin_preserved(resumed, monkeypatch, tmp_path, capsys):
    import sys
    conn, task, first = resumed
    with kb.write_txn(conn):
        kb._end_run(conn, task.id, outcome='blocked', status='blocked')
        conn.execute("UPDATE tasks SET status='blocked',claim_lock=NULL,worker_pid=NULL,worker_started_at=NULL,"
                     "model_override='deepseek-v4.1-flash',provider_override='opencode-go' WHERE id=?", (task.id,))
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    payload = dict(grant_id='cli-approval', iterations=20, runtime_seconds=3600, actor='Principal', reason='Owner authorization',
                   expected_run_id=task.current_run_id, expected_instruction_revision=task.instruction_revision)
    path = tmp_path / 'grant.json'
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'grant-budget', '--task', task.id, '--input', str(path)])
    review.d.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['grant_id'] == 'cli-approval'
    current = kb.get_task(conn, task.id)
    assert current.status == 'blocked'
    assert current.max_runtime_seconds == task.max_runtime_seconds + 3600
    assert review.worker_model_args(current, conn)[:4] == ['-m', 'deepseek-v4.1-flash', '--provider', 'opencode-go']
    assert review.iteration_grants(conn, 'another-card') == []
    with kb.write_txn(conn):
        state = json.loads(review.d.get_workflow(conn, task.id)['state_json'])
        state['worker_escalation']['first_run_id'] = task.current_run_id
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (json.dumps(state), task.id))
    assert not [g for g in review.iteration_grants(conn, task.id)
                if g['first_run_id'] == review.worker_escalation(conn, task.id)['first_run_id']]


@pytest.fixture
def exhausted_idle(resumed, monkeypatch):
    conn, task, first = resumed
    with kb.write_txn(conn):
        kb._end_run(conn, task.id, outcome='blocked', status='blocked')
        conn.execute("UPDATE tasks SET status='blocked',block_kind='maintenance_pause',claim_lock=NULL,"
                     "worker_pid=NULL,worker_started_at=NULL,max_runtime_seconds=7200 WHERE id=?", (task.id,))
        conn.execute('UPDATE task_runs SET started_at=100,ended_at=24152,metadata=? WHERE id=?',
                     (json.dumps({'worker_session_id': 'retained-session', 'escalation_usage': {'iterations': 400}}), first))
        conn.execute('UPDATE task_runs SET started_at=30000,ended_at=30000 WHERE id=?', (task.current_run_id,))
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    payload = dict(grant_id='runtime-recovery', iterations=200, runtime_seconds=20452,
                   actor='Principal', reason='Owner approved 3600 seconds remaining',
                   expected_run_id=task.current_run_id, expected_instruction_revision=task.instruction_revision)
    return conn, task, payload


@pytest.mark.parametrize('grant', [False, True])
def test_runtime_grant_debits_history_and_timeout_uses_remaining_balance(exhausted_idle, monkeypatch, grant):
    conn, task, payload = exhausted_idle
    before = [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
    if grant:
        receipt = review.grant_iteration_budget(conn, task.id, **payload)
        assert receipt['prior_runtime_seconds'] == 24052
        assert receipt['previous_max_runtime_seconds'] == 7200
        assert receipt['max_runtime_seconds'] == 27652
        assert receipt['remaining_runtime_seconds'] == 3600
        assert review.grant_iteration_budget(conn, task.id, **payload) == receipt
    current = kb.get_task(conn, task.id)
    assert current.status == 'blocked' and current.block_kind == 'maintenance_pause'
    assert before == [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
    # Explicit fixture resume is separate from the grant, which cannot clear maintenance.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready',block_kind=NULL WHERE id=?", (task.id,))
    current = kb.claim_task(conn, task.id)
    assert current
    kb._set_worker_pid(conn, task.id, os.getpid())
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=40000 WHERE id=?', (current.current_run_id,))
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(current.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', current.claim_lock)
    agent = SimpleNamespace(model='gpt-5.6-luna', reasoning_config={'effort': 'max'}, iteration_budget=IterationBudget(400))
    review.worker_checkpoint(agent)
    assert agent.iteration_budget.max_total == (200 if grant else 0)
    signals = []
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(kb.time, 'time', lambda: 43599 if grant else 40001)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: signals.append(args)) == ([] if grant else [task.id])
    if grant:
        assert signals == []
        monkeypatch.setattr(kb.time, 'time', lambda: 43601)
        assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: signals.append(args)) == [task.id]


@pytest.mark.parametrize('seconds', [-1, True, 1.5, '3600'])
def test_runtime_grant_rejects_invalid_seconds(exhausted_idle, seconds):
    conn, task, payload = exhausted_idle
    payload['runtime_seconds'] = seconds
    with pytest.raises(review.d.WorkflowError):
        review.grant_iteration_budget(conn, task.id, **payload)
    assert kb.get_task(conn, task.id).max_runtime_seconds == 7200
    assert not review.iteration_grants(conn, task.id)


@pytest.mark.parametrize('limit', [None, 0, -1])
def test_runtime_grant_requires_explicit_positive_limit(exhausted_idle, limit):
    conn, task, payload = exhausted_idle
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?', (limit, task.id))
    with pytest.raises(review.d.WorkflowError):
        review.grant_iteration_budget(conn, task.id, **payload)
    assert not review.iteration_grants(conn, task.id)


def test_runtime_grant_replay_conflict_and_legacy_receipt(exhausted_idle):
    conn, task, payload = exhausted_idle
    receipt = review.grant_iteration_budget(conn, task.id, **payload)
    with pytest.raises(review.d.WorkflowError, match='different parameters'):
        review.grant_iteration_budget(conn, task.id, **dict(payload, runtime_seconds=20453))
    assert kb.get_task(conn, task.id).max_runtime_seconds == receipt['max_runtime_seconds']
    legacy = dict(payload, grant_id='legacy', runtime_seconds=0)
    saved = review.grant_iteration_budget(conn, task.id, **legacy)
    del saved['runtime_seconds']
    with kb.write_txn(conn):
        row = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='nfos_iteration_budget_granted' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(saved), row['id']))
    legacy.pop('runtime_seconds')
    assert review.grant_iteration_budget(conn, task.id, **legacy) == saved
