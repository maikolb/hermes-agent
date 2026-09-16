"""Only the dispatcher-owned process may publish worker identity and usage."""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

from agent.delegation_context import non_dispatcher_owned_context
from agent.iteration_budget import IterationBudget
from hermes_cli import kanban_db as kb, nfos_principal_review as review
from tests.hermes_cli.test_nfos_principal_acceptance import task_context


@pytest.fixture
def owner(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    for key, value in [('TASK', task.id), ('RUN_ID', task.current_run_id), ('CLAIM_LOCK', task.claim_lock)]:
        monkeypatch.setenv('HERMES_KANBAN_' + key, str(value))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    config = artifact.parent.parent / 'home' / 'config.yaml'
    value = yaml.safe_load(config.read_text())
    value['kanban']['delivery']['worker_escalation'] = True
    config.write_text(yaml.safe_dump(value))
    agent = SimpleNamespace(model='gpt-5.6-luna', provider='openai-codex',
                            reasoning_config={'effort': 'high'}, session_id='owner-session',
                            iteration_budget=IterationBudget(10))
    return conn, task, agent


def metadata(conn, task):
    return json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])


def test_real_auxiliary_process_with_inherited_claim_cannot_overwrite_owner(owner):
    conn, task, agent = owner
    assert task.worker_pid == os.getpid()
    assert not review.worker_checkpoint(agent, 'owner-turn')
    assert agent.iteration_budget.consume()
    review.record_worker_iteration(agent)
    before = metadata(conn, task)
    count = conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
    child = subprocess.run([sys.executable, '-c', '''
import os
from types import SimpleNamespace
from agent.iteration_budget import IterationBudget
from hermes_cli import nfos_principal_review as review
agent=SimpleNamespace(model='deepseek-v4.1-flash',provider='opencode-go',reasoning_config=None,
 session_id='auxiliary-session',iteration_budget=IterationBudget(10))
for _ in range(7): agent.iteration_budget.consume()
assert review.worker_checkpoint(agent,'auxiliary-turn') is False
review.record_worker_iteration(agent)
print(os.getpid())
'''], env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert int(child.stdout.strip().splitlines()[-1]) != os.getpid()
    assert metadata(conn, task) == before
    assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0] == count
    assert agent.iteration_budget.consume()
    review.record_worker_iteration(agent)
    assert metadata(conn, task)['escalation_usage']['iterations'] == 2


@pytest.mark.parametrize('fence', ['missing_pid', 'wrong_pid', 'missing_start', 'wrong_start', 'child_marker', 'context'])
def test_non_owner_cannot_record_identity_or_usage(owner, monkeypatch, fence):
    conn, task, agent = owner
    if fence == 'child_marker':
        monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1')
    elif fence != 'context':
        column, value = {'missing_pid': ('worker_pid', None), 'wrong_pid': ('worker_pid', os.getpid()+1),
                         'missing_start': ('worker_started_at', None), 'wrong_start': ('worker_started_at', 1)}[fence]
        with kb.write_txn(conn):
            conn.execute(f'UPDATE tasks SET {column}=? WHERE id=?', (value, task.id))
    before = metadata(conn, task)
    def attempt():
        assert not review.worker_checkpoint(agent, 'aux-turn')
        review.record_worker_iteration(agent)
    if fence == 'context':
        with non_dispatcher_owned_context():
            attempt()
    else:
        attempt()
    assert metadata(conn, task) == before


@pytest.mark.parametrize('phase', ['identity', 'budget', 'confirm', 'iteration'])
def test_owner_is_rechecked_inside_every_write_transaction(owner, monkeypatch, phase):
    conn, task, agent = owner
    pending = {'status': 'pending', 'source_run_id': task.current_run_id-1}
    monkeypatch.setattr(review, 'worker_escalation', lambda *args: pending if phase == 'confirm' else {})
    confirmations = []
    monkeypatch.setattr(review, 'confirm_worker_dispatch', lambda *args: confirmations.append(args))
    before = metadata(conn, task)
    original = kb.write_txn
    calls = 0
    target = {'identity': 1, 'budget': 2, 'confirm': 3, 'iteration': 1}[phase]
    @contextmanager
    def change_owner_at_write(connection, *args, **kwargs):
        nonlocal calls
        calls += 1
        with original(connection, *args, **kwargs):
            if calls == target:
                connection.execute('UPDATE tasks SET worker_started_at=1 WHERE id=?', (task.id,))
            yield
    monkeypatch.setattr(kb, 'write_txn', change_owner_at_write)
    if phase == 'iteration':
        review.record_worker_iteration(agent)
    else:
        assert not review.worker_checkpoint(agent, 'owner-turn')
    after = metadata(conn, task)
    if phase in ('identity', 'iteration'):
        assert after == before
    if phase == 'budget':
        assert after.get('escalation_usage') == before.get('escalation_usage')
    assert not confirmations
