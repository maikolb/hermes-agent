import json
import sys

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_principal_review as review, nfos_tool
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, _enable_escalation


def ask(conn, task, question='Which correction addresses the repeated mismatch?', context=None, kind='impediment'):
    return d.ask_principal(conn, task.id, task.current_run_id, kind=kind, question=question,
                           context=context if context is not None else {'cause': 'count-mismatch'})


def test_same_cause_pending_rephrasing_reuses_but_new_evidence_does_not(task_context):
    conn, task, _, artifact = task_context
    context={'cause':'count-mismatch','evidence':[str(artifact)]}
    first=ask(conn,task,context=context)
    assert ask(conn,task,'How should this same count mismatch be corrected?',context)==first
    assert ask(conn,task,context={'cause':'other-mismatch'})!=first
    artifact.write_text('new observed result')
    assert ask(conn,task,context=context)!=first
    assert len(conn.execute("SELECT * FROM nfos_decisions WHERE kind='impediment'").fetchall())==3


def test_question_without_explicit_cause_and_changed_context_are_not_swallowed(task_context):
    conn,task,_,_=task_context
    first=ask(conn,task,'Question one?',{})
    assert ask(conn,task,'Question two?',{})!=first
    assert ask(conn,task,'Question one?',{'observed':'new evidence'})!=first


def attempt(conn,task,artifact):
    # Real subprocess/tool output in the existing board, not fabricated receipts.
    db=conn.execute('PRAGMA database_list').fetchone()[2]
    return nfos_tool.run_command(db,task_id=task.id,run_id=task.current_run_id,
        argv=[sys.executable,'-c',"print('observed 30; expected 31')"],cwd=artifact.parent,timeout_seconds=10)['id']


def test_new_native_tool_evidence_is_not_deduplicated(task_context):
    conn,task,_,artifact=task_context
    with kb.write_txn(conn):conn.execute("UPDATE nfos_workflows SET stage='analysis' WHERE task_id=?",(task.id,))
    first=ask(conn,task)
    attempt(conn,task,artifact)
    assert ask(conn,task)!=first


def stagnation(conn,task,artifact,kind='impediment'):
    first=ask(conn,task,kind=kind)
    d.resolve_decision(conn,first,action='changes',answer='Read the source count and correct the assumption',author='Principal')
    tool=attempt(conn,task,artifact)
    second=ask(conn,task,kind=kind)
    return second,{'failure':{'kind':'stagnation','cause':'model_reasoning',
        'reason':'The worker repeated the disproved assumption after the explicit correction; source remains 31.',
        'prior_decision_id':first,'tool_call_ids':[tool]}}


@pytest.mark.parametrize('kind',['impediment','spec_review'])
def test_principal_stagnation_uses_existing_ladder_without_report(task_context,monkeypatch,kind):
    conn,task,_,artifact=task_context
    _enable_escalation(monkeypatch)
    # This diagnostic tool is allowed before implementation; spec work remains analysis.
    with kb.write_txn(conn):conn.execute("UPDATE nfos_workflows SET stage='analysis' WHERE task_id=?",(task.id,))
    decision,assessment=stagnation(conn,task,artifact,kind)
    d.resolve_decision(conn,decision,action='changes',answer='Escalate this demonstrated reasoning failure',author='Principal',assessment=assessment)
    state=review.worker_escalation(conn,task.id)
    assert state['level']==1 and state['model']=='gpt-5.6-luna' and state['reasoning_effort']=='max'
    assert state['source_run_id']==task.current_run_id
    assert state.get('report_id') is None
    assert kb.get_task(conn,task.id).current_run_id==task.current_run_id
    assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE status='human'").fetchone()[0]==0
    # A second assessment on the same real attempt does not spend another tier.
    next_decision=ask(conn,task,kind=kind)
    d.resolve_decision(conn,next_decision,action='changes',answer='Same attempt',author='Principal',assessment=assessment)
    assert review.worker_escalation(conn,task.id)==state
    with kb.write_txn(conn):assert review.reclaim_escalation(conn,task.id)
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending',lambda *a:False)
    next_task=kb.claim_task(conn,task.id)
    assert next_task.workspace_path==task.workspace_path
    with kb.write_txn(conn):review.confirm_worker_dispatch(conn,task.id,next_task.current_run_id,'gpt-5.6-luna','max')
    assert review.worker_escalation(conn,task.id)['status']=='applied'
    decision,assessment=stagnation(conn,next_task,artifact,kind)
    d.resolve_decision(conn,decision,action='changes',answer='The dispatched max tier repeated the same disproved assumption',author='Principal',assessment=assessment)
    state=review.worker_escalation(conn,task.id)
    assert state['level']==2 and state['model']=='gpt-6-astra' and state['reasoning_effort']=='low'
    assert state['source_run_id']==next_task.current_run_id
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1


def test_explicit_luna_pin_prevents_stagnation_override(task_context,monkeypatch):
    conn,task,_,artifact=task_context;_enable_escalation(monkeypatch)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_workflows SET stage='analysis' WHERE task_id=?",(task.id,))
        conn.execute("UPDATE tasks SET model_override='gpt-5.6-luna',provider_override='openai-codex',reasoning_effort='high' WHERE id=?",(task.id,))
    task=kb.get_task(conn,task.id)
    decision,assessment=stagnation(conn,task,artifact)
    d.resolve_decision(conn,decision,action='changes',answer='Keep explicit pin',author='Principal',assessment=assessment)
    assert not review.worker_escalation(conn,task.id)
    assert review.worker_model_args(task,conn)[-1]=='high'


@pytest.mark.parametrize('cause',['billing','external_access','external_ci'])
def test_external_failure_never_escalates(task_context,monkeypatch,cause):
    conn,task,_,_=task_context;_enable_escalation(monkeypatch)
    decision=ask(conn,task)
    d.resolve_decision(conn,decision,action='changes',answer='Resolve external condition',author='Principal',
        assessment={'failure':{'kind':'stagnation','cause':cause,'reason':'External obstacle'}})
    assert not review.worker_escalation(conn,task.id)


def test_stagnation_requires_persisted_tool_evidence(task_context,monkeypatch):
    conn,task,_,_=task_context;_enable_escalation(monkeypatch)
    decision=ask(conn,task)
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn,decision,action='changes',answer='Repeated requests alone are not proof',author='Principal',
            assessment={'failure':{'kind':'stagnation','cause':'model_reasoning','reason':'Repeated','prior_decision_id':'missing','tool_call_ids':['missing']}})
    assert not review.worker_escalation(conn,task.id)


@pytest.mark.parametrize('invalid',['different_cause','wrong_run','stale_scope'])
def test_stagnation_rejects_unrelated_or_stale_attempt(task_context,monkeypatch,invalid):
    conn,task,_,artifact=task_context;_enable_escalation(monkeypatch)
    with kb.write_txn(conn):conn.execute("UPDATE nfos_workflows SET stage='analysis' WHERE task_id=?",(task.id,))
    decision,assessment=stagnation(conn,task,artifact)
    with kb.write_txn(conn):
        if invalid=='different_cause':
            row=d.get_decision(conn,decision);context=json.loads(row['context']);context['cause']='another problem'
            conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?',(json.dumps(context),decision))
        elif invalid=='wrong_run':
            other_run=conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES(?,'done',0)",(task.id,)).lastrowid
            conn.execute('UPDATE nfos_tool_calls SET run_id=? WHERE id=?',(other_run,assessment['failure']['tool_call_ids'][0]))
        else:conn.execute('UPDATE tasks SET instruction_revision=instruction_revision+1 WHERE id=?',(task.id,))
    with pytest.raises(d.WorkflowError):
        d.resolve_decision(conn,decision,action='changes',answer='Do not escalate unrelated evidence',author='Principal',assessment=assessment)
    assert not review.worker_escalation(conn,task.id)
