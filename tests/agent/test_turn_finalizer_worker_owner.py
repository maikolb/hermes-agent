"""An auxiliary process's exhausted budget cannot terminate its parent's run."""
import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_nfos_principal_acceptance import task_context
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent, _finalize


@pytest.fixture
def worker(task_context, monkeypatch):
    conn, task, _, _ = task_context
    for key, value in [('TASK', task.id), ('RUN_ID', task.current_run_id), ('CLAIM_LOCK', task.claim_lock)]:
        monkeypatch.setenv('HERMES_KANBAN_' + key, str(value))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', lambda *_a, **_kw: [])
    return conn, task


@pytest.mark.parametrize('exit_reason', ['unknown', 'all_retries_exhausted_no_response'])
def test_real_auxiliary_budget_one_does_not_close_parent_run(worker, exit_reason, tmp_path):
    conn, task = worker
    assert task.worker_pid == os.getpid()
    before_task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone())
    before_run = dict(conn.execute('SELECT * FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone())
    before_events = conn.execute('SELECT count(*) FROM task_events WHERE task_id=?', (task.id,)).fetchone()[0]
    child = subprocess.run([sys.executable, '-c', '''
import json,os,sys
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent,_finalize
from hermes_cli import plugins
plugins.invoke_hook=lambda *args,**kwargs: []
agent=_LimitAgent(max_iterations=1)
result=_finalize(agent,final_response=None,exit_reason=sys.argv[1],api_call_count=1)
print(json.dumps({'pid':os.getpid(),'task':os.environ['HERMES_KANBAN_TASK'],
 'run':os.environ['HERMES_KANBAN_RUN_ID'],'reason':result['turn_exit_reason']}))
''', exit_reason], env=os.environ.copy(), capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert child.returncode == 0, child.stderr
    observed = json.loads(child.stdout.strip().splitlines()[-1])
    assert observed['pid'] != os.getpid()
    assert observed['task'] == task.id and int(observed['run']) == task.current_run_id
    after_task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone())
    after_run = dict(conn.execute('SELECT * FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone())
    (tmp_path/'auxiliary-budget-proof.json').write_text(json.dumps({
        'parent_pid':os.getpid(), 'child':observed, 'before_run':before_run,
        'after_run':after_run, 'before_task':before_task, 'after_task':after_task}, indent=2))
    assert after_run == before_run, 'Auxiliary finalizer changed the dispatcher-owned parent run'
    assert after_task == before_task
    assert conn.execute('SELECT count(*) FROM task_events WHERE task_id=?', (task.id,)).fetchone()[0] == before_events


@pytest.mark.parametrize('continuation', [False, True])
def test_actual_owner_can_still_record_its_budget_terminal_outcome(worker, monkeypatch, continuation):
    conn, task = worker
    monkeypatch.setattr(kb, '_budget_continuation_cap', lambda: 3 if continuation else 0)
    _finalize(_LimitAgent(max_iterations=1), final_response=None, exit_reason='unknown', api_call_count=1)
    run = conn.execute('SELECT ended_at,status FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
    assert run['ended_at'] is not None
    assert kb.get_task(conn, task.id).claim_lock is None


@pytest.mark.parametrize('continuation', [False, True])
def test_replaced_claim_is_rechecked_inside_terminal_write(worker, monkeypatch, continuation):
    conn, task = worker
    monkeypatch.setattr(kb, '_budget_continuation_cap', lambda: 3 if continuation else 0)
    original = kb.write_txn
    entered = []

    @contextmanager
    def replace_claim_at_write(connection, *args, **kwargs):
        with original(connection, *args, **kwargs):
            if not entered:
                connection.execute('UPDATE tasks SET claim_lock=? WHERE id=?', ('replacement-claim', task.id))
            entered.append(True)
            yield connection

    monkeypatch.setattr(kb, 'write_txn', replace_claim_at_write)
    _finalize(_LimitAgent(max_iterations=1), final_response=None, exit_reason='unknown', api_call_count=1)
    assert entered
    assert conn.execute('SELECT ended_at FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0] is None
    current = kb.get_task(conn, task.id)
    assert current.status == 'running' and current.claim_lock == 'replacement-claim'
    assert current.worker_pid == task.worker_pid
