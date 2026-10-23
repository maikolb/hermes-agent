"""A worker run that reaches the soft call ceiling yields to the Principal with a checkpoint instead of dying."""
import json
import os
from types import SimpleNamespace

import pytest
import yaml

from agent.iteration_budget import IterationBudget
from hermes_cli import goals, kanban_db as kb, nfos_principal_review as review
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept


@pytest.fixture
def running(task_context, monkeypatch):
    conn, task, _, _ = task_context
    accept(conn, task, 'spec_review')
    current = kb.get_task(conn, task.id)
    if current.status != 'running':
        current = kb.claim_task(conn, task.id)
    kb._set_worker_pid(conn, task.id, os.getpid())
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(current.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', current.claim_lock)
    return conn, kb.get_task(conn, task.id)


def agent_with(used, ceiling_total=400):
    budget = IterationBudget(ceiling_total)
    for _ in range(used):
        assert budget.consume()
    return SimpleNamespace(model='gpt-5.6-luna', reasoning_config={'effort': 'high'}, iteration_budget=budget, session_id='sess-1')


def ceiling_decisions(conn, task_id):
    return conn.execute("SELECT id,status,action,context FROM nfos_decisions WHERE task_id=? "
                        "AND json_extract(context,'$.call_ceiling') IS NOT NULL ORDER BY rowid", (task_id,)).fetchall()


def test_below_the_ceiling_the_run_goes_on(running):
    conn, task = running
    assert review.worker_checkpoint(agent_with(299), 'turn-1') is False
    assert ceiling_decisions(conn, task.id) == []


def test_at_the_ceiling_the_run_yields_with_a_checkpoint_and_is_not_a_failure(running):
    conn, task = running
    agent = agent_with(300)
    assert review.worker_checkpoint(agent, 'turn-1') is True
    [decision] = ceiling_decisions(conn, task.id)
    context = json.loads(decision['context'])['call_ceiling']
    assert decision['status'] == 'pending'
    assert context['calls'] == 300 and context['ceiling'] == 300 and context['session_id'] == 'sess-1'
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id) == decision['id']
    after = kb.get_task(conn, task.id)
    assert after.status == 'running' and after.current_run_id == task.current_run_id and after.claim_lock == task.claim_lock
    assert after.consecutive_failures == 0
    assert conn.execute('SELECT ended_at FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0] is None
    # Asking again, as the goal loop's wait turn does, files no second question.
    assert review.worker_checkpoint(agent, 'turn-2') is True
    assert len(ceiling_decisions(conn, task.id)) == 1


def test_goal_loop_leaves_as_awaiting_decision_after_the_yield(running, monkeypatch):
    conn, task = running
    agent = agent_with(300)
    turns = []
    def run_turn(prompt):
        turns.append(prompt)
        review.worker_checkpoint(agent, 'turn-%d' % len(turns))
        return ''
    monkeypatch.setattr(goals, 'judge_goal', lambda *args: ('continue', 'Still open', False, False, False))
    assert review.worker_checkpoint(agent, 'turn-0') is True
    outcome = goals.run_kanban_goal_loop(
        task_id=task.id, goal_text='Verify count', run_turn=run_turn,
        task_status_fn=lambda: kb.get_task(conn, task.id).status, block_fn=lambda reason: None,
        pending_decision_fn=lambda: kb._nfos_pending_decision(conn, task.id, task.current_run_id), first_response='')
    assert outcome['outcome'] == 'awaiting_decision'
    assert len(ceiling_decisions(conn, task.id)) == 1


def test_continue_answer_lets_the_run_resume_for_another_ceiling(running):
    conn, task = running
    agent = agent_with(300)
    assert review.worker_checkpoint(agent, 'turn-1') is True
    [decision] = ceiling_decisions(conn, task.id)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer='Keep going from the checkpoint' WHERE id=?",
                     (decision['id'],))
    assert kb._nfos_pending_decision(conn, task.id, task.current_run_id) is None
    assert review.worker_checkpoint(agent, 'turn-2') is False
    assert review.worker_checkpoint(agent_with(399), 'turn-3') is False
    assert len(ceiling_decisions(conn, task.id)) == 1


def test_ceiling_comes_from_config_and_zero_turns_it_off(running, monkeypatch):
    conn, task = running
    monkeypatch.setattr(review, 'settings', lambda: {'call_ceiling': 5})
    assert review.worker_checkpoint(agent_with(4), 'turn-1') is False
    assert review.worker_checkpoint(agent_with(5), 'turn-2') is True
    assert json.loads(ceiling_decisions(conn, task.id)[0]['context'])['call_ceiling']['ceiling'] == 5


def test_zero_ceiling_disables_the_yield(running, monkeypatch):
    conn, task = running
    monkeypatch.setattr(review, 'settings', lambda: {'call_ceiling': 0})
    assert review.worker_checkpoint(agent_with(350), 'turn-1') is False
    assert ceiling_decisions(conn, task.id) == []
