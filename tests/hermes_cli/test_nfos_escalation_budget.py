"""Independent regression checks for escalation budgets and goal-loop handoff."""
import json
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
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=1000 WHERE id=?', (current.current_run_id,))
        conn.execute('UPDATE tasks SET max_runtime_seconds=100,goal_max_turns=3,worker_pid=987654 WHERE id=?', (task.id,))
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
    assert len(signals) == 1 and signals[0][0] == 987654  # Captured callback only; no real signal.


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


