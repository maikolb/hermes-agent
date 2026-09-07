"""Saved reports distinguish claimed outcomes from accessible, linked evidence."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d


@pytest.fixture
def report_task(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    db = tmp_path / 'home' / 'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with kb.connect_closing(db) as conn:
        rid = d.receive_request(conn,
            source={'platform':'telegram','chat_id':'-1','thread_id':'4','message_id':'evidence'},
            text='Audit counts and save the evidence',
            project={'profile':'default','delivery_type':'report'}, attachments=[])
        request = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
        workspace = tmp_path / task.id
        workspace.mkdir()
        kb.set_workspace_path(conn, task.id, str(workspace))
        task = kb.get_task(conn, task.id)
        d.save_spec(conn, task.id, task.current_run_id,
            {'goal':'Audited count','criteria':[{'id':'AC1','text':'Count verified'}],
             'steps':['Read','Compare'], 'delivery_type':'report'},
            author='Claude TL', evidence={'session':'test-tl'})
        evidence = workspace / 'count.txt'
        evidence.write_text('count=31\n', encoding='utf-8')
        yield conn, task, workspace, evidence


def report(reference, *, status='PASS'):
    return {'summary':'Count checked',
            'criteria':[{'id':'AC1','status':status,'evidence':[reference] if reference else []}],
            'artifacts':[reference] if reference else []}


def approve(conn, task):
    decision = d.ask_principal(conn, task.id, task.current_run_id,
        kind='review', question='Review the current report', context={})
    d.resolve_decision(conn, decision, action='approve', answer='Evidence reviewed', author='Principal')


def test_missing_local_file_is_not_accepted_as_evidence(report_task):
    conn, task, workspace, _ = report_task
    with pytest.raises(d.WorkflowError, match='accessible|unavailable|missing'):
        d.save_report(conn, task.id, task.current_run_id, report(str(workspace / 'missing.png')))
    assert d._artifact(conn, task.id, 'report') is None


@pytest.mark.parametrize('status', ['FAIL', 'NOT_RUN'])
def test_partial_report_preserves_status_but_cannot_complete(report_task, status):
    conn, task, _, _ = report_task
    value = report(None, status=status)
    value['criteria'][0]['reason'] = 'The requested behavior has not been proved'
    d.save_report(conn, task.id, task.current_run_id, value)
    assert json.loads(d._artifact(conn, task.id, 'report')['content']) == value
    approve(conn, task)
    assert not d.completion_ready(conn, task.id)
    assert not kb.complete_task(conn, task.id, result='Not finished')


def test_accessible_local_file_is_hashed_and_bound_to_task_run_spec_and_criterion(report_task):
    conn, task, _, evidence = report_task
    d.save_report(conn, task.id, task.current_run_id, report(str(evidence)))
    saved = d._artifact(conn, task.id, 'report')
    check = json.loads(saved['evidence'])['artifact_checks'][0]
    assert check['status'] == 'verified_local'
    assert check['path'] == str(evidence.resolve())
    assert check['sha256'] == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert check['size_bytes'] == evidence.stat().st_size
    assert check['task_id'] == task.id
    assert check['run_id'] == task.current_run_id
    assert check['spec_revision'] == 1
    assert check['criteria'] == ['AC1']
    approve(conn, task)
    assert kb.complete_task(conn, task.id, result='Count=31')


def test_criterion_must_link_to_a_declared_report_artifact(report_task):
    conn, task, workspace, evidence = report_task
    other = workspace / 'different.txt'
    other.write_text('different result', encoding='utf-8')
    value = report(str(evidence))
    value['criteria'][0]['evidence'] = [str(other)]
    with pytest.raises(d.WorkflowError, match='declared|link'):
        d.save_report(conn, task.id, task.current_run_id, value)


def test_relative_object_references_resolve_in_the_task_workspace(report_task):
    conn, task, _, evidence = report_task
    value = report('result-1')
    value['artifacts'] = [{'id':'result-1','path':evidence.name,'label':'Count result'}]
    d.save_report(conn, task.id, task.current_run_id, value)
    check = json.loads(d._artifact(conn, task.id, 'report')['evidence'])['artifact_checks'][0]
    assert check['path'] == str(evidence.resolve())
    approve(conn, task)
    assert d.completion_ready(conn, task.id)


@pytest.mark.parametrize('change', ['removed', 'modified'])
def test_completion_checks_current_local_evidence_after_review(report_task, change):
    conn, task, _, evidence = report_task
    d.save_report(conn, task.id, task.current_run_id, report(str(evidence)))
    approve(conn, task)
    if change == 'removed':
        evidence.unlink()
    else:
        evidence.write_text('different unreviewed result', encoding='utf-8')
    assert not kb.complete_task(conn, task.id, result='Claimed complete')
    assert kb.get_task(conn, task.id).status == 'running'


def test_remote_url_alone_is_not_automatically_verified(report_task):
    conn, task, _, _ = report_task
    d.save_report(conn, task.id, task.current_run_id, report('https://private.example.invalid/result'))
    saved = d._artifact(conn, task.id, 'report')
    check = json.loads(saved['evidence'])['artifact_checks'][0]
    assert check['status'] == 'external_unchecked'
    approve(conn, task)
    assert not d.completion_ready(conn, task.id)


def test_external_reference_can_supplement_a_local_readback_without_network_request(report_task):
    conn, task, _, evidence = report_task
    url='https://private.example.invalid/result'
    value=report(str(evidence))
    value['criteria'][0]['evidence'].append({'url':url})
    value['artifacts'].append({'url':url,'label':'External source'})
    d.save_report(conn,task.id,task.current_run_id,value)
    approve(conn,task)
    assert d.completion_ready(conn,task.id)
    checks=json.loads(d._artifact(conn,task.id,'report')['evidence'])['artifact_checks']
    assert [row['status'] for row in checks]==['verified_local','external_unchecked']


def test_save_and_complete_read_files_without_holding_the_board_writer(report_task,monkeypatch):
    conn,task,_,evidence=report_task
    db=conn.execute('PRAGMA database_list').fetchone()['file']
    opened=[];original=Path.open
    def observe(path,*args,**kwargs):
        if path==evidence:
            assert not conn.in_transaction
            with sqlite3.connect(db,timeout=0,isolation_level=None) as other:
                other.execute('BEGIN IMMEDIATE');other.execute('ROLLBACK')
            opened.append(str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',observe)
    d.save_report(conn,task.id,task.current_run_id,report(str(evidence)))
    approve(conn,task)
    assert kb.complete_task(conn,task.id,result='Count verified')
    assert len(opened)==2


def test_directory_cannot_masquerade_as_accessible_evidence_file(report_task):
    conn,task,workspace,_=report_task
    with pytest.raises(d.WorkflowError,match='regular file'):
        d.save_report(conn,task.id,task.current_run_id,report(str(workspace)))


def test_absolute_existing_report_outside_workspace_remains_valid(report_task,tmp_path):
    conn,task,_,_=report_task
    retained=tmp_path/'retained-report.txt'
    retained.write_text('Existing authorized audit evidence',encoding='utf-8')
    d.save_report(conn,task.id,task.current_run_id,report(str(retained)))
    approve(conn,task)
    assert d.completion_ready(conn,task.id)


@pytest.mark.parametrize('explicit_input',[False,True])
def test_another_task_workspace_needs_explicit_input_association(report_task,tmp_path,explicit_input):
    conn,task,_,_=report_task
    other=kb.create_task(conn,title='Other audit',assignee='default',delivery_type='report',requires_repo=False)
    other_workspace=tmp_path/other;other_workspace.mkdir()
    kb.set_workspace_path(conn,other,str(other_workspace))
    other_result=other_workspace/'other.txt'
    other_result.write_text('Previous audit result',encoding='utf-8')
    if explicit_input:
        wf=d.get_workflow(conn,task.id)
        request=d.get_request(conn,wf['request_id'])
        payload=json.loads(request['payload'])
        payload['attachments'].append({'original':str(other_result)})
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?',(json.dumps(payload),request['id']))
        conn.commit()
        d.save_report(conn,task.id,task.current_run_id,report(str(other_result)))
        approve(conn,task)
        assert d.completion_ready(conn,task.id)
    else:
        with pytest.raises(d.WorkflowError,match='another task workspace'):
            d.save_report(conn,task.id,task.current_run_id,report(str(other_result)))


def test_completion_snapshot_cannot_approve_another_report_revision(report_task):
    conn,task,_,evidence=report_task
    d.save_report(conn,task.id,task.current_run_id,report(str(evidence)))
    snapshot=d.completion_evidence_check(conn,task.id)
    d.save_report(conn,task.id,task.current_run_id,report(str(evidence)))
    approve(conn,task)
    with kb.write_txn(conn):
        assert not d.completion_ready(conn,task.id,evidence_check=snapshot)
    assert d.completion_ready(conn,task.id)


def test_spec_changed_during_file_read_does_not_receive_a_stale_report(report_task,monkeypatch):
    conn,task,_,evidence=report_task
    original=d._inspect_local_evidence
    def advance_spec(path):
        check=original(path)
        spec=json.loads(d.get_spec(conn,task.id)['content'])
        spec['goal']='Updated requested audit'
        d.save_spec(conn,task.id,task.current_run_id,spec,author='Claude TL',evidence={'session':'updated-tl'})
        return check
    monkeypatch.setattr(d,'_inspect_local_evidence',advance_spec)
    with pytest.raises(d.WorkflowError,match='Spec or workspace changed'):
        d.save_report(conn,task.id,task.current_run_id,report(str(evidence)))
    assert d._artifact(conn,task.id,'report') is None
