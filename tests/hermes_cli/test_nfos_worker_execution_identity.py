"""Effective run identity is recorded by the initialized worker, never inferred."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, nfos_principal_review as review
from tests.hermes_cli.test_nfos_principal_acceptance import task_context


def bind_env(monkeypatch, task):
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', task.claim_lock)


def metadata(conn, run):
    return json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',(run,)).fetchone()[0] or '{}')


@pytest.mark.parametrize('model,provider,effort', [('deepseek-v4.1-flash','opencode-go','max'),
    ('gpt-5.6-luna','openai-codex','high'), ('gpt-6-astra','openai-codex','medium')])
def test_identity_without_escalation_is_actual_and_idempotent(task_context, monkeypatch, model, provider, effort):
    conn, task, _, _ = task_context
    bind_env(monkeypatch,task)
    monkeypatch.setattr(review,'settings',lambda:{'worker_escalation':False,'worker_model':'wrong-global-model'})
    agent=SimpleNamespace(model=model,provider=provider,reasoning_config={'effort':effort},session_id='same-session')
    assert review.worker_checkpoint(agent) is False
    first=metadata(conn,task.current_run_id)['worker_execution']
    assert first == dict(model=model,provider=provider,reasoning_effort=effort,session_id='same-session',
                         recorded_at=first['recorded_at'],source='agent_pre_request')
    assert first['recorded_at'] > 0
    assert review.worker_checkpoint(agent) is False
    assert metadata(conn,task.current_run_id)['worker_execution'] == first
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_worker_execution'").fetchone()[0] == 1


@pytest.mark.parametrize('fence',['claim','run','delegated'])
def test_stale_or_delegated_worker_cannot_publish_identity(task_context, monkeypatch, fence):
    conn,task,_,_=task_context
    bind_env(monkeypatch,task)
    if fence=='claim':monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK','wrong')
    elif fence=='run':monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(task.current_run_id+1))
    else:monkeypatch.setattr('agent.delegation_context.is_dispatcher_owned_worker_context',lambda:False)
    agent=SimpleNamespace(model='gpt-6-astra',provider='openai-codex',reasoning_config={'effort':'medium'},session_id='delegated')
    assert review.worker_checkpoint(agent) is False
    assert 'worker_execution' not in metadata(conn,task.current_run_id)


def test_shared_session_and_fallback_preserve_each_run_history(task_context, monkeypatch):
    conn,task,_,_=task_context
    bind_env(monkeypatch,task)
    monkeypatch.setattr(review,'settings',lambda:{'worker_escalation':False})
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending',lambda *a:False)
    agent=SimpleNamespace(model='gpt-5.6-luna',provider='openai-codex',reasoning_config={'effort':'high'},session_id='retained-session')
    review.worker_checkpoint(agent)
    first=task.current_run_id; saved=metadata(conn,first)['worker_execution']
    # The fixture owns this pytest PID, so close the synthetic run without signaling it.
    with kb.write_txn(conn):
        kb._end_run(conn,task.id,outcome='reclaimed',status='reclaimed')
        conn.execute("UPDATE tasks SET status='ready',worker_pid=NULL,claim_lock=NULL,claim_expires=NULL WHERE id=?",(task.id,))
    task=kb.claim_task(conn,task.id)
    kb._set_worker_pid(conn, task.id, os.getpid())
    bind_env(monkeypatch,task)
    agent.model='gpt-6-astra';agent.reasoning_config={'effort':'low'}
    review.worker_checkpoint(agent)
    assert metadata(conn,first)['worker_execution']==saved
    assert metadata(conn,task.current_run_id)['worker_execution']['model']=='gpt-6-astra'
    agent.reasoning_config={'effort':'medium'}
    review.worker_checkpoint(agent)
    events=[json.loads(x[0]) for x in conn.execute("SELECT payload FROM task_events WHERE kind='nfos_worker_execution' ORDER BY id")]
    assert [x['reasoning_effort'] for x in events]==['high','low','medium']
    assert len({x['session_id'] for x in events})==1
