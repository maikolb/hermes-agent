"""Administrative closure uses the same board without delivery evidence."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_runtime as runtime
from hermes_cli.kanban_cancellation import completion_refusal
from tests.hermes_cli.test_nfos_worker_shutdown import board, worker, enrolled, assert_exited


def authorization(revision=0):
    return {'disposition':'cancelled_by_owner','functional_delivery':False,
            'reason':'Owner cancelled the obsolete QR migration','author':'Maikol',
            'source':'fixture:owner-message-15289','authorization_message':'Cancel obsolete QR work and move to delivered with [CANCELADO]',
            'expected_instruction_revision':revision}


@pytest.fixture
def task_context(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    with kb.connect_closing() as conn:
        rid=d.receive_request(conn,source={'platform':'test','chat_id':'1','thread_id':'2','message_id':'cancel'},
            text='Implement obsolete migration',project={'board':'test','profile':'default','delivery_type':'code','repo_path':str(tmp_path)})
        claim=d.reserve_request(conn,capacity=2)
        task_id=d.bootstrap_card(conn,rid,claim['claim_token'],pid=os.getpid()).id
        yield conn,task_id,tmp_path


@pytest.mark.parametrize('state',['running','blocked','ready','todo','backlog','review','archived'])
def test_cancel_all_open_states_without_spec_report_or_deploy(task_context,state):
    conn,tid,_=task_context
    conn.execute('UPDATE tasks SET status=? WHERE id=?',(state,tid));conn.commit()
    before=conn.execute('select count(*) from task_events where task_id=?',(tid,)).fetchone()[0]
    assert kb.complete_task(conn,tid,metadata=authorization())
    task=kb.get_task(conn,tid)
    assert task.status=='done' and task.title.startswith('[CANCELADO]')
    assert task.instruction_revision==1 and task.current_run_id is None
    assert 'Implement obsolete migration' in task.body
    assert conn.execute('select count(*) from task_events where task_id=?',(tid,)).fetchone()[0]>before
    run=kb.latest_run(conn,tid)
    assert run.outcome=='cancelled'
    assert (json.loads(run.metadata) if isinstance(run.metadata,str) else run.metadata)['functional_delivery'] is False
    assert not d.get_spec(conn,tid)


def test_cancel_satisfies_dependency_but_not_another_open_parent(task_context):
    conn,tid,_=task_context
    other=kb.create_task(conn,title='Other obligation',assignee='default')
    child=kb.create_task(conn,title='Next step',assignee='default',parents=[tid])
    waiting=kb.create_task(conn,title='Needs both',assignee='default',parents=[tid,other])
    assert kb.complete_task(conn,tid,metadata=authorization())
    assert kb.get_task(conn,child).status=='ready'
    assert kb._parents_satisfied(conn,child)
    assert kb.get_task(conn,waiting).status=='todo'
    assert not kb._parents_satisfied(conn,waiting)


def test_cancel_is_idempotent_and_refuses_stale_owner_instruction(task_context):
    conn,tid,_=task_context
    kb.update_task_instruction(conn,tid,body='New owner scope',author='Owner',expected_revision=0)
    with pytest.raises(ValueError,match='instruction changed'):
        kb.complete_task(conn,tid,metadata=authorization())
    assert kb.complete_task(conn,tid,metadata=authorization(1))
    events=conn.execute('select count(*) from task_events').fetchone()[0]
    assert kb.complete_task(conn,tid,metadata=authorization(1))
    assert conn.execute('select count(*) from task_events').fetchone()[0]==events


def test_cancel_does_not_require_its_own_parent_to_be_done(task_context):
    conn,tid,_=task_context
    parent=kb.create_task(conn,title='Unfinished parent',assignee='default')
    kb.link_tasks(conn,parent,tid)
    assert not kb._parents_satisfied(conn,tid)
    assert kb.complete_task(conn,tid,metadata=authorization())
    assert kb.get_task(conn,tid).status=='done'


@pytest.mark.parametrize('field',['author','reason','source','authorization_message','expected_instruction_revision'])
def test_cancel_requires_auditable_authorization(task_context,field):
    conn,tid,_=task_context
    metadata=authorization();metadata.pop(field)
    with pytest.raises(ValueError,match=field):
        kb.complete_task(conn,tid,metadata=metadata)
    assert kb.get_task(conn,tid).status!='done'


def test_cancel_supersedes_old_human_hold_without_rewriting_it(task_context):
    conn,tid,_=task_context
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,status,question,context,answer,author,action,spec_revision,created_at) VALUES('old',?,1,'impediment','human','Need QR','{}','Scan QR','Principal','human',1,1)",(tid,));conn.commit()
    assert kb.complete_task(conn,tid,metadata=authorization())
    decision=d.get_decision(conn,'old')
    assert decision['status']=='superseded' and decision['answer']=='Scan QR'
    assert decision['author']=='Principal'
    assert d.active_suspension(conn,tid) is None
    assert d.reconcile_human_answers(conn)==[]
    assert kb.get_task(conn,tid).status=='done'


def test_functional_completion_still_requires_evidence(task_context):
    conn,tid,_=task_context
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(tid,));conn.commit()
    assert not kb.complete_task(conn,tid,result='Migration done')
    assert 'functional_delivery_not_validated' in completion_refusal(conn,tid)


def test_cancel_tool_bypasses_old_implementation_judge(task_context,monkeypatch):
    from tools import kanban_tools as kt
    conn,tid,_=task_context
    monkeypatch.setattr(kt,'_goal_mode_handoff_rejection',lambda *a:pytest.fail('Cancelled work must not reach the implementation judge'))
    result=json.loads(kt._handle_complete({'task_id':tid,'summary':'[CANCELADO] Owner cancelled','metadata':authorization()}))
    assert not result.get('error'),result
    assert kb.get_task(conn,tid).status=='done'


def test_normal_tool_completion_still_uses_judge(task_context,monkeypatch):
    from tools import kanban_tools as kt
    conn,tid,_=task_context
    monkeypatch.setattr(kt,'_goal_mode_handoff_rejection',lambda *a:'Missing functional proof')
    result=json.loads(kt._handle_complete({'task_id':tid,'summary':'Migration delivered'}))
    assert 'Missing functional proof' in result['error']
    assert kb.get_task(conn,tid).status!='done'


def test_cancel_cli_records_administrative_result(task_context):
    conn,tid,tmp=task_context
    path=tmp/'cancel.json';path.write_text(json.dumps(authorization()))
    result=subprocess.run([sys.executable,str(Path(d.__file__)),'cancel','--task',tid,'--input',str(path)],
        capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['disposition']=='cancelled_by_owner'
    assert kb.get_task(conn,tid).status=='done'


def test_cancel_active_worker_preserves_files_and_stops_tree(board,worker):
    conn,directory=board
    proc,identities=worker
    task=enrolled(conn,proc.pid)
    file=directory/'preserved.txt';file.write_text('Original evidence')
    assert kb.complete_task(conn,task.id,metadata=authorization())
    runtime.reconcile_terminal_workers(conn,worker_exit_grace_seconds=0)
    assert_exited(identities)
    assert file.read_text()=='Original evidence'
    assert kb.get_task(conn,task.id).status=='done'
