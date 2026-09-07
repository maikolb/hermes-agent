"""An enabled project's retained cards must enter NFOS without replaying work."""
import json
import os
import sqlite3

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    (tmp_path/'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': {'projects': {
        'pilot': {'enabled': True, 'profile': 'default', 'workers': 2, 'delivery_type': 'report'}
    }}}}), encoding='utf-8')
    with kb.connect_closing() as conn:
        yield conn


def retained(conn, title='Retained report'):
    tid=kb.create_task(conn, title=title, body='Original scope. Keep existing evidence.',
                       assignee='default', delivery_type='report', requires_repo=False)
    kb.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='-11',
                      thread_id='22', notifier_profile='default', delivery_mode='notify+wake')
    kb.add_comment(conn, tid, author='original-worker', body='Report: saved-result.md; no repeat requested.')
    return tid


def tick(conn):
    # Zero free slots must not prevent durable migration or Principal review.
    return kb.dispatch_once(conn, board='pilot', max_spawn=0, reconcile_orphans=False)


def test_enabled_project_adopts_same_card_before_dispatch_without_rewriting_history(board):
    tid=retained(board)
    before=dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    comments=[tuple(r) for r in board.execute('SELECT * FROM task_comments WHERE task_id=?', (tid,))]
    tick(board)
    workflow=delivery.get_workflow(board, tid)
    assert workflow is not None, 'Enabled project left its retained ready card in the legacy path'
    after=dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert after.pop('goal_mode')==1
    before.pop('goal_mode')
    assert before == after
    assert comments == [tuple(r) for r in board.execute('SELECT * FROM task_comments WHERE task_id=?', (tid,))]
    request=delivery.get_request(board, workflow['request_id'])
    assert request['status']=='attached' and request['task_id']==tid
    assert json.loads(request['payload'])['source']['message_identity_kind']=='retained-card'
    assert board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]==0
    tick(board)
    assert board.execute('SELECT COUNT(*) FROM nfos_requests').fetchone()[0]==1
    assert board.execute('SELECT COUNT(*) FROM nfos_workflows').fetchone()[0]==1


@pytest.mark.parametrize('action', ['continue', 'human'])
def test_retained_block_reaches_principal_once_and_only_its_answer_resumes(board, action):
    tid=retained(board)
    kb.block_task(board, tid, reason='Tool unavailable; partial report retained', kind='transient')
    tick(board)
    decisions=delivery.pending_decisions(board)
    assert len(decisions)==1, 'Retained blocker never reached the new Principal queue'
    decision=decisions[0]
    assert decision['task_id']==tid and decision['kind']=='impediment'
    assert kb.get_task(board,tid).status=='blocked'
    assert board.execute('SELECT COUNT(*) FROM nfos_artifacts').fetchone()[0]==0
    tick(board)
    assert len(delivery.pending_decisions(board))==1
    delivery.resolve_decision(board,decision['id'],action=action,
                              answer='Use the saved report' if action=='continue' else 'Which account owns this task?',
                              author='Principal')
    assert kb.get_task(board,tid).status==('ready' if action=='continue' else 'blocked')
    assert delivery.get_spec(board,tid) is None
    assert not delivery.completion_ready(board,tid)


def test_rollout_does_not_enroll_or_interrupt_a_live_executor(board):
    tid=retained(board)
    task=kb.claim_task(board,tid)
    board.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',
                  (os.getpid(),kb._process_start_time(os.getpid()),tid));board.commit()
    tick(board)
    assert delivery.get_workflow(board,tid) is None
    assert kb.get_task(board,tid).current_run_id==task.current_run_id
    assert kb.get_task(board,tid).worker_pid==os.getpid()


def test_rollout_keeps_backlog_dependencies_and_completed_cards_in_place(board):
    parent=retained(board,'Parent')
    child=retained(board,'Child')
    held=retained(board,'Unrequested backlog')
    completed=retained(board,'Delivered')
    board.execute("UPDATE tasks SET status='backlog',assignee=NULL WHERE id=?",(held,))
    board.execute("UPDATE tasks SET status='done' WHERE id=?",(completed,))
    board.commit()
    kb.link_tasks(board, parent, child)
    tick(board)
    assert delivery.get_workflow(board,child) is not None
    assert kb.get_task(board,child).status=='todo'
    assert kb.get_task(board,held).status=='backlog'
    assert delivery.get_workflow(board,completed) is None
    assert delivery.pending_decisions(board)==[]


def test_migration_event_failure_rolls_back_the_entire_adoption(board):
    tid=retained(board)
    board.execute("CREATE TRIGGER reject_rollout BEFORE INSERT ON task_events WHEN NEW.kind='nfos_legacy_adopted' BEGIN SELECT RAISE(ABORT,'rollout power loss'); END")
    board.commit()
    with pytest.raises(sqlite3.IntegrityError, match='rollout power loss'):
        tick(board)
    assert delivery.get_workflow(board,tid) is None
    assert board.execute('SELECT COUNT(*) FROM nfos_requests').fetchone()[0]==0
    board.execute('DROP TRIGGER reject_rollout');board.commit()
    tick(board)
    assert delivery.get_workflow(board,tid) is not None


def test_retained_review_waits_for_principal_then_resumes_without_a_second_review_worker(board):
    tid=retained(board)
    board.execute("UPDATE tasks SET status='review' WHERE id=?",(tid,))
    board.execute('INSERT OR IGNORE INTO task_git_delivery(task_id,required_at,required) VALUES(?,1,1)',(tid,))
    board.execute("UPDATE task_git_delivery SET candidate_digest='retained-candidate',receipt_json='{}',receipt_fingerprint='fixture-fingerprint',verified_at=1 WHERE task_id=?",(tid,))
    board.commit()
    tick(board)
    decision=delivery.pending_decisions(board)[0]
    assert kb.get_task(board,tid).status=='review'
    with pytest.raises(delivery.WorkflowError):
        delivery.resolve_decision(board,decision['id'],action='approve',answer='Delivered',author='Principal')
    delivery.resolve_decision(board,decision['id'],action='continue',answer='Formalize the retained evidence',author='Principal')
    assert kb.get_task(board,tid).status=='ready'
    assert delivery.get_spec(board,tid) is None
    old=board.execute('SELECT required,candidate_digest,receipt_json FROM task_git_delivery WHERE task_id=?',(tid,)).fetchone()
    assert tuple(old)==(0,'retained-candidate','{}')


def test_rollout_does_not_compete_with_an_explicit_recovery_request(board):
    tid=retained(board)
    rid=delivery.receive_request(board,source={'platform':'telegram','chat_id':'-11','thread_id':'22','message_id':'99'},
        text='Resume the same report',project={'profile':'default','delivery_type':'report','existing_task_id':tid})
    tick(board)
    assert delivery.get_workflow(board,tid) is None
    assert delivery.get_request(board,rid)['status']=='pending'
    assert board.execute('SELECT COUNT(*) FROM nfos_requests').fetchone()[0]==1


def test_disabled_project_keeps_its_existing_protocol(board, tmp_path):
    tid=retained(board)
    (tmp_path/'config.yaml').write_text('kanban: {}',encoding='utf-8')
    tick(board)
    assert delivery.get_workflow(board,tid) is None


def test_retained_human_answer_restores_same_card_and_dependencies(board):
    parent=retained(board,'Parent')
    tid=retained(board)
    kb.block_task(board,tid,reason='Need the owner account',kind='needs_input')
    tick(board)
    decision=delivery.pending_decisions(board)[0]
    delivery.resolve_decision(board,decision['id'],action='human',answer='Which account?',author='Principal')
    kb.link_tasks(board,parent,tid)
    delivery.resume_after_answer(board,tid,answer='My test account',
        source={'platform':'telegram','chat_id':'-11','thread_id':'22','message_id':'100'})
    assert kb.get_task(board,tid).status=='todo'
    assert delivery.get_workflow(board,tid) is not None


def test_resolution_and_resume_roll_back_together(board):
    tid=retained(board)
    kb.block_task(board,tid,reason='A retained operational error',kind='transient')
    tick(board)
    decision=delivery.pending_decisions(board)[0]
    board.execute("CREATE TRIGGER reject_resume BEFORE INSERT ON task_events WHEN NEW.kind='unblocked' BEGIN SELECT RAISE(ABORT,'resume power loss'); END")
    board.commit()
    with pytest.raises(sqlite3.IntegrityError,match='resume power loss'):
        delivery.resolve_decision(board,decision['id'],action='continue',answer='Resolved',author='Principal')
    assert delivery.get_decision(board,decision['id'])['status']=='pending'
    assert kb.get_task(board,tid).status=='blocked'


def test_reused_pid_does_not_hold_an_idle_retained_card(board):
    tid=retained(board)
    board.execute('UPDATE tasks SET worker_pid=?,worker_started_at=-1 WHERE id=?',(os.getpid(),tid));board.commit()
    tick(board)
    assert delivery.get_workflow(board,tid) is not None
