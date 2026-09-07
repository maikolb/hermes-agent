"""Candidate identity, Principal review and publication use the same SQLite authority."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d

A='a'*40
B='b'*40
TREE='c'*40


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=d.receive_request(conn, source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'3'},
            text='Fix accepted layout',project={'board':'pilot','profile':'default','delivery_type':'code','repo_path':str(tmp_path)})
        req=d.reserve_request(conn,capacity=2)
        task=d.bootstrap_card(conn,rid,req['claim_token'],pid=os.getpid())
        d.save_spec(conn,task.id,task.current_run_id,
            {'goal':'Readable layout','criteria':[{'id':'AC1','text':'No overlap'}],
             'steps':['Reproduce','Fix','Verify'], 'delivery_type':'code'},
            author='Claude TL',evidence={'session':'tl-real-test-fixture'})
        yield conn,task


def homolog(conn,task):
    assert d.acquire_project(conn,'pilot',task.id,task.current_run_id,A)
    d.advance(conn,task.id,task.current_run_id,'homolog',next_action='Review candidate',
        state={'homolog_sha':A,'candidate_tree':TREE,'homolog_evidence':['hml-result.json']})


def approved(conn,task):
    homolog(conn,task)
    pr=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=A)
    d.reconcile_effect(conn,pr['id'],found=True,evidence={'readback':'PR head verified','candidate':A,'url':'https://example.test/pr/1'})
    review=d.ask_principal(conn,task.id,task.current_run_id,kind='review',question='Review tested candidate',context={})
    d.resolve_decision(conn,review,action='approve',answer='Verified spec, diff and HML behavior',author='Principal')


def test_merge_cannot_start_before_current_candidate_review(delivery):
    conn,task=delivery
    homolog(conn,task)
    with pytest.raises(d.WorkflowError,match='review'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)


def test_candidate_change_invalidates_principal_approval(delivery):
    conn,task=delivery
    approved(conn,task)
    d.advance(conn,task.id,task.current_run_id,'implement',next_action='New correction',state={'homolog_sha':B})
    with pytest.raises(d.WorkflowError,match='review'):
        d.advance(conn,task.id,task.current_run_id,'publish',next_action='Merge changed candidate')


def test_homolog_requires_the_project_publication_slot(delivery):
    conn,task=delivery
    with pytest.raises(d.WorkflowError,match='project'):
        d.advance(conn,task.id,task.current_run_id,'homolog',next_action='Deploy HML',state={'homolog_sha':A})


def test_changed_merge_tree_requires_rehomologation(delivery):
    conn,task=delivery
    approved(conn,task)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)
    with pytest.raises(d.WorkflowError,match='tree'):
        d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'Merged PR','candidate':A,'integrated_sha':B,'tree':'d'*40})


def test_deploy_accepts_only_the_confirmed_integrated_tree(delivery):
    conn,task=delivery
    approved(conn,task)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)
    d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'Merged PR','candidate':A,'integrated_sha':B,'tree':TREE})
    with pytest.raises(d.WorkflowError,match='integrated'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='deploy',target='prod',candidate=A)
    deploy=d.begin_effect(conn,task.id,task.current_run_id,operation='deploy',target='prod',candidate=B)
    with pytest.raises(d.WorkflowError,match='artifact'):
        d.reconcile_effect(conn,deploy['id'],found=True,evidence={'readback':'HTTP 200','candidate':B,'tree':TREE})
    d.reconcile_effect(conn,deploy['id'],found=True,evidence={'readback':'Live version and actual behavior verified',
        'candidate':B,'tree':TREE,'artifact':'artifact-sha256','behavior_evidence':['real-browser.png']})
    assert not d.completion_ready(conn,task.id)  # Report is still required.
    wf=json.loads(d.get_workflow(conn,task.id)['state_json'])
    assert wf['integrated_sha']==B
    assert wf['artifact']=='artifact-sha256'
