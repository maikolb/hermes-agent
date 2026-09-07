"""A saved spec and publication review belong to the current card instructions."""
import json

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_candidate_delivery import delivery, approved, A, B, TREE
from tests.hermes_cli.test_nfos_report_evidence import report_task, report, approve


def correct(conn,task):
    current=kb.get_task(conn,task.id)
    kb.update_task_instruction(conn,task.id,body=current.body+'\nApply the newly specified constraint.',
        author='Principal',expected_revision=current.instruction_revision)
    assert kb.get_task(conn,task.id).instruction_revision==current.instruction_revision+1


def refresh(conn,task):
    spec=json.loads(d.get_spec(conn,task.id)['content'])
    spec['steps'].append('Verify the updated operator constraint')
    return d.save_spec(conn,task.id,task.current_run_id,spec,author='Claude TL',evidence={'session':'updated-spec-fixture'})


@pytest.mark.parametrize('operation',['merge','deploy'])
def test_changed_instructions_prevent_new_effect_under_old_approval(delivery,operation):
    conn,task=delivery;approved(conn,task)
    candidate=A
    if operation=='deploy':
        merge=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)
        d.reconcile_effect(conn,merge['id'],found=True,evidence={'readback':'Merged candidate',
            'candidate':A,'integrated_sha':B,'tree':TREE})
        candidate=B
    correct(conn,task)
    before=[dict(row) for row in conn.execute('SELECT * FROM nfos_effects ORDER BY id')]
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.begin_effect(conn,task.id,task.current_run_id,operation=operation,target='requested-destination',candidate=candidate)
    assert [dict(row) for row in conn.execute('SELECT * FROM nfos_effects ORDER BY id')]==before
    assert not d._approved(conn,task.id,1)


@pytest.mark.parametrize('review_step',['request','approve'])
def test_review_cannot_approve_stale_instruction_spec(delivery,review_step):
    conn,task=delivery;approved(conn,task)
    pending=d.ask_principal(conn,task.id,task.current_run_id,kind='review',question='Review again',context={})
    correct(conn,task)
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        if review_step=='request':
            d.ask_principal(conn,task.id,task.current_run_id,kind='review',question='Review old spec',context={})
        else:
            d.resolve_decision(conn,pending,action='approve',answer='Approve old evidence',author='Principal')
    assert d.get_decision(conn,pending)['status']=='pending'
    # Asking for a spec correction remains possible for the Principal.
    d.resolve_decision(conn,pending,action='changes',answer='Update and persist the spec',author='Principal')


def test_refresh_records_real_revision_and_restores_review_controlled_publication(delivery):
    conn,task=delivery;approved(conn,task)
    old=d.get_spec(conn,task.id)
    correct(conn,task)
    assert 'spec' in d.get_workflow(conn,task.id)['next_action'].lower()
    d.advance(conn,task.id,task.current_run_id,'analysis',next_action='Read the corrected instructions')
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.advance(conn,task.id,task.current_run_id,'implement',next_action='Implement stale spec')
    spec=json.loads(old['content']);spec['steps'].append('Verify the correction')
    supplied={'session':'updated-spec-fixture','instruction_revision':999}
    assert d.save_spec(conn,task.id,task.current_run_id,spec,author='Claude TL',evidence=supplied)==2
    assert supplied['instruction_revision']==999
    assert json.loads(d.get_spec(conn,task.id)['evidence'])['instruction_revision']==1
    assert dict(conn.execute('SELECT * FROM nfos_artifacts WHERE id=?',(old['id'],)).fetchone())==old
    d.advance(conn,task.id,task.current_run_id,'implement',next_action='Implement current spec')
    with pytest.raises(d.WorkflowError,match='review'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)
    decision=d.ask_principal(conn,task.id,task.current_run_id,kind='review',question='Review current spec',context={})
    d.resolve_decision(conn,decision,action='approve',answer='Current spec and candidate verified',author='Principal')
    assert d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)['execute']


def test_report_correction_refreshes_spec_and_report_without_git_delivery(report_task):
    conn,task,_,evidence=report_task
    d.save_report(conn,task.id,task.current_run_id,report(str(evidence)));approve(conn,task)
    checked=d.completion_evidence_check(conn,task.id)
    assert d.completion_ready(conn,task.id,evidence_check=checked)
    correct(conn,task)
    assert not d.completion_ready(conn,task.id,evidence_check=checked)
    with kb.write_txn(conn):
        assert not d.completion_ready(conn,task.id,evidence_check=checked)
    assert not kb.complete_task(conn,task.id,result='Old report')
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.save_report(conn,task.id,task.current_run_id,report(str(evidence)))
    d.advance(conn,task.id,task.current_run_id,'analysis',next_action='Apply the report clarification')
    assert refresh(conn,task)==2
    evidence.write_text('count=30\n',encoding='utf-8')
    d.save_report(conn,task.id,task.current_run_id,report(str(evidence)));approve(conn,task)
    assert d.completion_ready(conn,task.id)
    assert kb.complete_task(conn,task.id,result='Updated report')
    assert not kb.get_task(conn,task.id).requires_repo
    assert conn.execute('SELECT count(*) FROM nfos_effects').fetchone()[0]==0


def test_initial_revision_without_new_field_remains_compatible_but_not_after_change(delivery):
    conn,task=delivery;approved(conn,task)
    saved=d.get_spec(conn,task.id);evidence=json.loads(saved['evidence'])
    evidence.pop('instruction_revision',None)
    conn.execute('UPDATE nfos_artifacts SET evidence=? WHERE id=?',(json.dumps(evidence),saved['id']));conn.commit()
    assert kb.get_task(conn,task.id).instruction_revision==0 and d._approved(conn,task.id,1)
    correct(conn,task)
    assert not d._approved(conn,task.id,1)
    assert refresh(conn,task)==2
    assert json.loads(d.get_spec(conn,task.id)['evidence'])['instruction_revision']==1


def test_identical_instruction_does_not_invalidate_current_spec(delivery):
    conn,task=delivery;approved(conn,task)
    before=d.get_workflow(conn,task.id)
    kb.update_task_instruction(conn,task.id,body=task.body,author='Principal',expected_revision=0)
    assert kb.get_task(conn,task.id).instruction_revision==0
    assert d._approved(conn,task.id,1)
    assert d.get_workflow(conn,task.id)==before


def test_unknown_external_effect_can_be_reconciled_after_instruction_changes(delivery):
    conn,task=delivery;approved(conn,task)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='repo:pr1',candidate=A)
    correct(conn,task)
    d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'Prior merge confirmed',
        'candidate':A,'integrated_sha':B,'tree':TREE})
    assert d._confirmed(conn,task.id,'merge',A)
    assert not d._approved(conn,task.id,1)
    assert len(conn.execute("SELECT * FROM nfos_effects WHERE operation='merge'").fetchall())==1


def test_retry_of_old_approval_cannot_reapprove_after_refreshed_instruction_spec(delivery):
    conn,task=delivery;approved(conn,task)
    old=dict(conn.execute("SELECT * FROM nfos_decisions WHERE kind='review'").fetchone())
    correct(conn,task);refresh(conn,task)
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.resolve_decision(conn,old['id'],action='approve',answer=old['answer'],author='Principal')
    assert d.get_decision(conn,old['id'])==old
