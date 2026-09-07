"""Real SQLite contract: intake, ownership, specs, review and uncertain effects."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path/'kanban.db'


def receive(conn, message='42'):
    return delivery.receive_request(conn,
        source={'platform':'telegram','chat_id':'-10001','thread_id':'8','message_id':message},
        text='Auditar a contagem e entregar o relatório.',
        project={'board':'pilot','profile':'default','delivery_type':'report'},
        attachments=[{'original':'/cache/photo.jpg','mime_type':'image/jpeg'}])


def started(conn):
    rid=receive(conn)
    request=delivery.reserve_request(conn, capacity=2)
    assert request['id']==rid
    task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
    return task


def spec(conn, task):
    return delivery.save_spec(conn,task.id,task.current_run_id,
        {'goal':'Contagem verificada','criteria':[{'id':'AC1','text':'Total reconciliado'}],
         'steps':['Consultar','Reconciliar'], 'delivery_type':'report'},
        author='Claude TL', evidence={'session':'tl-001','output':'/evidence/tl.jsonl'})


def test_receive_is_durable_idempotent_and_does_not_create_card(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        assert receive(conn)==rid
        assert conn.execute('select count(*) from tasks').fetchone()[0]==0
    with kb.connect_closing(board) as conn:
        row=delivery.get_request(conn,rid)
        assert row['status']=='pending'
        assert json.loads(row['payload'])['attachments'][0]['original']=='/cache/photo.jpg'


def test_two_connections_cannot_reserve_same_worker_request(board):
    with kb.connect_closing(board) as conn:
        receive(conn)
    def claim(_):
        with kb.connect_closing(board) as conn:
            return delivery.reserve_request(conn,capacity=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,range(2)))
    assert sum(row is not None for row in results)==1


def test_bootstrap_retransmission_keeps_card_run_and_owner(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        request=delivery.reserve_request(conn,capacity=2)
        first=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        second=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert (first.id,first.current_run_id)==(second.id,second.current_run_id)
        assert first.worker_pid==os.getpid()
        assert first.created_by=='worker:default'
        with pytest.raises(delivery.OwnershipConflict):
            delivery.bootstrap_card(conn,rid,'different-worker',pid=os.getpid())
        assert conn.execute('select count(*) from tasks').fetchone()[0]==1
        assert conn.execute('select count(*) from task_runs').fetchone()[0]==1


def test_bootstrap_failure_rolls_back_card_event_and_forwarding(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        request=delivery.reserve_request(conn,capacity=2)
        conn.execute("CREATE TRIGGER fail_bootstrap BEFORE INSERT ON nfos_workflows BEGIN SELECT RAISE(ABORT,'power-loss'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError,match='power-loss'):
            delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert conn.execute('select count(*) from tasks').fetchone()[0]==0
        assert conn.execute('select count(*) from task_events').fetchone()[0]==0
        assert delivery.get_request(conn,rid)['task_id'] is None


def test_implementation_requires_persisted_spec_and_current_run(board):
    with kb.connect_closing(board) as conn:
        task=started(conn)
        with pytest.raises(delivery.WorkflowError,match='spec'):
            delivery.advance(conn,task.id,task.current_run_id,'implement',next_action='Write test')
        spec(conn,task)
        delivery.advance(conn,task.id,task.current_run_id,'implement',next_action='Write test')
        with pytest.raises(delivery.OwnershipConflict):
            delivery.advance(conn,task.id,task.current_run_id+1,'homolog',next_action='Verify')
    with kb.connect_closing(board) as conn:
        wf=delivery.get_workflow(conn,task.id)
        assert wf['stage']=='implement'
        assert wf['next_action']=='Write test'
        assert delivery.get_spec(conn,task.id)['author']=='Claude TL'


def test_pending_principal_decisions_survive_disconnect_and_keep_worker(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,
            kind='impediment',question='Qual origem usar?',context={'tried':['Read config']})
    with kb.connect_closing(board) as conn:
        assert delivery.pending_decisions(conn)[0]['id']==decision
        delivery.resolve_decision(conn,decision,action='continue',answer='Use existing source',author='Principal')
        assert kb.get_task(conn,task.id).current_run_id==task.current_run_id
        assert delivery.get_decision(conn,decision)['status']=='resolved'


def test_lost_external_response_requires_destination_read_before_retry(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        first=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='pr',target='repo:branch',candidate='a'*40)
        assert first['execute'] is True
        again=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='pr',target='repo:branch',candidate='a'*40)
        assert again['execute'] is False
        assert again['reconcile'] is True
        delivery.reconcile_effect(conn,first['id'],found=True,
            evidence={'readback':'github PR 80','url':'https://example.test/pull/80'})
        final=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='pr',target='repo:branch',candidate='a'*40)
        assert final['execute'] is False and final['reconcile'] is False


def test_done_requires_review_and_report_and_reports_need_no_pr(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        assert kb.complete_task(conn,task.id,result='Completed') is False
        delivery.save_report(conn,task.id,task.current_run_id,
            {'criteria':[{'id':'AC1','status':'PASS','evidence':['/evidence/count.txt']}],
             'summary':'Total reconciled','artifacts':['/evidence/count.txt']})
        assert kb.complete_task(conn,task.id,result='Completed') is False
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,
            kind='review',question='Review the report',context={})
        delivery.resolve_decision(conn,decision,action='approve',answer='Count and evidence checked',author='Principal')
        assert kb.complete_task(conn,task.id,result='Total reconciled') is True


def test_readback_absence_allows_one_retry_and_confirmation_is_final(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        args=dict(operation='pr',target='repo:feature',candidate='b'*40)
        effect=delivery.begin_effect(conn,task.id,task.current_run_id,**args)
        delivery.reconcile_effect(conn,effect['id'],found=False,evidence={'readback':'No matching PR at destination'})
        assert delivery.begin_effect(conn,task.id,task.current_run_id,**args)['execute'] is True
        assert delivery.begin_effect(conn,task.id,task.current_run_id,**args)['execute'] is False
        delivery.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'PR found','url':'https://example.test/pr/2'})
        with pytest.raises(delivery.WorkflowError):
            delivery.reconcile_effect(conn,effect['id'],found=False,evidence={'readback':'stale empty lookup'})


def test_project_homolog_and_publication_have_one_owner(board):
    with kb.connect_closing(board) as conn:
        first=started(conn);spec(conn,first)
        receive(conn,'43')
        request=delivery.reserve_request(conn,capacity=2)
        second=delivery.bootstrap_card(conn,request['id'],request['claim_token'],pid=os.getpid())
        spec(conn,second)
        assert delivery.acquire_project(conn,'pilot',first.id,first.current_run_id,'a'*40)
        assert not delivery.acquire_project(conn,'pilot',second.id,second.current_run_id,'b'*40)
        delivery.release_project(conn,'pilot',first.id,first.current_run_id)
        assert delivery.acquire_project(conn,'pilot',second.id,second.current_run_id,'b'*40)
