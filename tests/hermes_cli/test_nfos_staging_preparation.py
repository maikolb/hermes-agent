"""Preparatory staging authority never substitutes for production approval."""
import json
import os
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d

A='a'*40
B='b'*40
TREE='c'*40
REPO='https://github.com/example/product'
TARGET=REPO+'/tree/staging'
PR=REPO+'/pull/123'

@pytest.fixture
def delivery(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=d.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'3'},
            text='Fix mentor link',project={'board':'pilot','profile':'default','delivery_type':'code','repo_path':str(tmp_path)})
        req=d.reserve_request(conn,capacity=2)
        task=d.bootstrap_card(conn,rid,req['claim_token'],pid=os.getpid())
        save_spec(conn,task)
        d.advance(conn,task.id,task.current_run_id,'implement',next_action='Prepare staging',
            state={'candidate_sha':A,'candidate_tree':TREE})
        yield conn,task

def save_spec(conn,task):
    d.save_spec(conn,task.id,task.current_run_id,{'goal':'Correct destination','criteria':[{'id':'C1','text':'Correct href'}],
        'steps':['Test','Prepare staging','Validate HML','Review production'],'delivery_type':'code'},
        author='Claude TL',evidence={'session':'test-fixture'})

def ask(conn,task,**overrides):
    preparation={'candidate_sha':A,'candidate_tree':TREE,'repository':REPO,'base_ref':'staging','target':TARGET}
    preparation.update(overrides)
    return d.ask_principal(conn,task.id,task.current_run_id,kind='preparation',question='Prepare only staging',
        context={'preparation':preparation})

def prepare(conn,task):
    decision=ask(conn,task)
    d.resolve_decision(conn,decision,action='continue',answer='Checked candidate; staging only',author='Principal')
    assert d.acquire_project(conn,'pilot',task.id,task.current_run_id,A)
    return decision

def receipt(**updates):
    result={'readback':'Actual staging destination','candidate':A,'tree':TREE,'repository':REPO,
        'base_ref':'staging','target':TARGET,'url':PR}
    result.update(updates)
    return result

def start(conn,task,operation='staging_pr',**updates):
    args={'operation':operation,'target':TARGET,'candidate':A};args.update(updates)
    return d.begin_effect(conn,task.id,task.current_run_id,**args)

def test_cycle_resolved_without_fabricating_homolog(delivery):
    conn,task=delivery
    with pytest.raises(d.WorkflowError,match='homologation'):
        d.ask_principal(conn,task.id,task.current_run_id,kind='review',question='Production review premature',context={})
    prepare(conn,task)
    pr=start(conn,task)
    assert pr['execute']
    d.reconcile_effect(conn,pr['id'],found=True,evidence=receipt())
    merge=start(conn,task,'staging_merge')
    d.reconcile_effect(conn,merge['id'],found=True,evidence=receipt(integrated_sha=B))
    state=json.loads(d.get_workflow(conn,task.id)['state_json'])
    assert state['staging_sha']==B
    assert not state.get('homolog_sha') and not state.get('integrated_sha')
    assert not d._confirmed(conn,task.id,'pr',A) and not d._confirmed(conn,task.id,'merge',A)
    assert not d.completion_ready(conn,task.id)

@pytest.mark.parametrize('operation',['pr','merge','deploy'])
def test_preparation_never_authorizes_production(delivery,operation):
    conn,task=delivery;prepare(conn,task)
    with pytest.raises(d.WorkflowError):
        d.begin_effect(conn,task.id,task.current_run_id,operation=operation,target=REPO+'/tree/main',candidate=A)

@pytest.mark.parametrize('changes',[{'base_ref':'main'},{'base_ref':'production'},
    {'target':REPO+'/tree/main'},{'repository':'https://github.com/other/product'},
    {'target':TARGET+'?next=main'},{'candidate_sha':B},{'candidate_tree':B},{'candidate_sha':'bogus'}])
def test_wrong_preparation_identity_rejected(delivery,changes):
    conn,task=delivery
    with pytest.raises(d.WorkflowError): ask(conn,task,**changes)

@pytest.mark.parametrize('change',['candidate','tree','spec','instruction'])
def test_stale_pending_preparation_cannot_be_accepted(delivery,change):
    conn,task=delivery;decision=ask(conn,task)
    if change=='spec': save_spec(conn,task)
    elif change=='instruction':
        conn.execute('UPDATE tasks SET instruction_revision=1 WHERE id=?',(task.id,));conn.commit()
    else:
        d.advance(conn,task.id,task.current_run_id,'implement',next_action='Changed',
            state={('candidate_sha' if change=='candidate' else 'candidate_tree'):B})
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn,decision,action='continue',answer='Stale',author='Principal')

@pytest.mark.parametrize('change',['candidate','tree','spec','instruction','target'])
def test_accepted_preparation_cannot_be_reused(delivery,change):
    conn,task=delivery;prepare(conn,task)
    target=TARGET
    if change=='spec': save_spec(conn,task)
    elif change=='instruction':
        conn.execute('UPDATE tasks SET instruction_revision=1 WHERE id=?',(task.id,));conn.commit()
    elif change=='target': target=REPO+'/tree/main'
    else:
        d.advance(conn,task.id,task.current_run_id,'implement',next_action='Changed',
            state={('candidate_sha' if change=='candidate' else 'candidate_tree'):B})
    with pytest.raises(d.WorkflowError): start(conn,task,target=target)

def test_authority_slot_and_confirmed_pr_required(delivery):
    conn,task=delivery
    with pytest.raises(d.WorkflowError): start(conn,task)
    decision=ask(conn,task)
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn,decision,action='continue',answer='Self approval',author='worker')
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn,decision,action='approve',answer='No production',author='Principal')
    d.resolve_decision(conn,decision,action='continue',answer='Staging only',author='Principal')
    with pytest.raises(d.WorkflowError,match='project'): start(conn,task)
    assert d.acquire_project(conn,'pilot',task.id,task.current_run_id,A)
    with pytest.raises(d.WorkflowError,match='PR'): start(conn,task,'staging_merge')

def test_retry_is_readback_first_and_pins_original_identity(delivery):
    conn,task=delivery;prepare(conn,task)
    effect=start(conn,task)
    again=start(conn,task)
    assert not again['execute'] and again['reconcile'] and again['id']==effect['id']
    with pytest.raises(d.WorkflowError,match='destination'): d.release_project(conn,'pilot',task.id,task.current_run_id)
    d.reconcile_effect(conn,effect['id'],found=False,evidence=receipt())
    assert start(conn,task)['execute']
    d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt())
    assert not start(conn,task)['execute']
    d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt())
    with pytest.raises(d.WorkflowError):
        d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt(url=REPO+'/pull/999'))

@pytest.mark.parametrize('changes',[{'candidate':B},{'tree':B},{'base_ref':'main'},
    {'target':REPO+'/tree/main'},{'url':'https://github.com/other/product/pull/123'},
    {'repository':'https://github.com/other/product'}])
def test_staging_readback_must_match_prepared_destination(delivery,changes):
    conn,task=delivery;prepare(conn,task);effect=start(conn,task)
    with pytest.raises(d.WorkflowError): d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt(**changes))

def test_merge_requires_exact_tree_sha_and_prepared_pr(delivery):
    conn,task=delivery;prepare(conn,task);pr=start(conn,task)
    d.reconcile_effect(conn,pr['id'],found=True,evidence=receipt())
    merge=start(conn,task,'staging_merge')
    for updates in ({},{'integrated_sha':'invalid'},{'integrated_sha':B,'tree':B},{'integrated_sha':B,'url':REPO+'/pull/999'}):
        with pytest.raises(d.WorkflowError): d.reconcile_effect(conn,merge['id'],found=True,evidence=receipt(**updates))


def test_cli_routes_preparation_and_preserves_worker_decision_guard(delivery,tmp_path,monkeypatch,capsys):
    import sys
    conn,task=delivery
    monkeypatch.setenv('HERMES_KANBAN_TASK',task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(task.current_run_id))
    path=tmp_path/'question.json'
    path.write_text(json.dumps({'question':'Staging only','context':{'preparation':{
        'candidate_sha':A,'candidate_tree':TREE,'repository':REPO,'base_ref':'staging','target':TARGET}}}))
    monkeypatch.setattr(sys,'argv',['nfos_delivery.py','ask','--kind','preparation','--input',str(path)])
    d.main()
    decision=json.loads(capsys.readouterr().out)['decision_id']
    path.write_text(json.dumps({'answer':'Not allowed from worker'}))
    monkeypatch.setattr(sys,'argv',['nfos_delivery.py','decide','--decision',decision,'--resolution','continue','--input',str(path)])
    with pytest.raises(d.WorkflowError,match='coordinator'): d.main()
    d.resolve_decision(conn,decision,action='continue',answer='Staging only',author='Principal')
    assert d.acquire_project(conn,'pilot',task.id,task.current_run_id,A)
    monkeypatch.setattr(sys,'argv',['nfos_delivery.py','effect','--operation','staging_pr','--target',TARGET,'--candidate',A])
    d.main()
    assert json.loads(capsys.readouterr().out)['execute']


def test_cross_task_candidate_mismatch_and_latest_rejection(delivery):
    conn,task=delivery;prepare(conn,task)
    with pytest.raises(d.WorkflowError): start(conn,task,candidate=B)
    decision=ask(conn,task)
    d.resolve_decision(conn,decision,action='changes',answer='Preparation withdrawn for now',author='Principal')
    with pytest.raises(d.WorkflowError): start(conn,task)


def test_separate_task_needs_own_review_and_project_slot(delivery,tmp_path):
    conn,task=delivery;prepare(conn,task)
    rid=d.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'other'},
        text='Other candidate',project={'board':'pilot','profile':'default','delivery_type':'code','repo_path':str(tmp_path)})
    req=d.reserve_request(conn,capacity=3)
    other=d.bootstrap_card(conn,rid,req['claim_token'],pid=os.getpid())
    save_spec(conn,other)
    d.advance(conn,other.id,other.current_run_id,'implement',next_action='Prepare own candidate',
        state={'candidate_sha':A,'candidate_tree':TREE})
    with pytest.raises(d.WorkflowError,match='preparation'): start(conn,other)
    decision=ask(conn,other)
    d.resolve_decision(conn,decision,action='continue',answer='Own review',author='Principal')
    assert not d.acquire_project(conn,'pilot',other.id,other.current_run_id,A)
    with pytest.raises(d.WorkflowError,match='project'): start(conn,other)
    effect=start(conn,task)
    with pytest.raises(d.OwnershipConflict):
        d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt(),caller_task_id=other.id,caller_run_id=other.current_run_id)


def test_unknown_staging_merge_blocks_slot_release(delivery):
    conn,task=delivery;prepare(conn,task)
    pr=start(conn,task);d.reconcile_effect(conn,pr['id'],found=True,evidence=receipt())
    effect=start(conn,task,'staging_merge')
    assert not d.acquire_project(conn,'pilot',task.id,task.current_run_id,B)
    again=start(conn,task,'staging_merge')
    assert again['id']==effect['id'] and again['reconcile'] and not again['execute']
    with pytest.raises(d.WorkflowError,match='destination'): d.release_project(conn,'pilot',task.id,task.current_run_id)
    d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt(integrated_sha=B))
    assert d.release_project(conn,'pilot',task.id,task.current_run_id)


def test_unknown_receipt_recovery_records_history_not_new_authority(delivery):
    conn,task=delivery;prepare(conn,task);effect=start(conn,task)
    save_spec(conn,task)
    d.reconcile_effect(conn,effect['id'],found=True,evidence=receipt())
    with pytest.raises(d.WorkflowError): start(conn,task,'staging_merge')
