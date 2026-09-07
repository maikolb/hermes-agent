"""Principal corrects its own human escalation without inventing a human reply."""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_candidate_delivery import delivery, homolog, A, B
from tests.hermes_cli.test_nfos_recovery_ownership import set_prior_worker


def human_block(conn, task, *, kind='review', confirmed_pr=True, pid=987654321):
    homolog(conn, task)
    if confirmed_pr:
        effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:pr1',candidate=A)
        d.reconcile_effect(conn,effect['id'],found=True,
            evidence={'readback':'Fixture PR head verified','candidate':A,'url':'https://example.test/pr/1'})
    decision=d.ask_principal(conn,task.id,task.current_run_id,kind=kind,
        question='Review homolog and authorize the requested publication',context={'checkpoint':'preserved'})
    d.resolve_decision(conn,decision,action='human',answer='Wait for owner to approve again',author='Principal')
    set_prior_worker(conn,task,pid)
    assert kb.block_task(conn,task.id,kind='needs_input',reason='Wait for owner to approve again')
    return decision


def reconsider(conn, decision, *, action='approve', **kwargs):
    return d.reconsider_decision(conn,decision,action=action,
        reason=kwargs.pop('reason','The authorized spec requires deploy before production verification'),
        answer=kwargs.pop('answer','Review passed; continue the already authorized publication'),
        author=kwargs.pop('author','Principal'), **kwargs)


def snapshot(conn):
    return {table:[dict(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        for table in ('tasks','task_runs','nfos_workflows','nfos_decisions','task_events')}


def test_reconsideration_preserves_old_answer_and_workspace_and_restores_same_card(delivery,tmp_path):
    conn,task=delivery
    workspace=tmp_path/'saved-work';workspace.mkdir()
    (workspace/'uncommitted.txt').write_text('Keep this existing work')
    kb.set_workspace_path(conn,task.id,workspace)
    old_id=human_block(conn,task)
    old=d.get_decision(conn,old_id)
    new_id=reconsider(conn,old_id)
    saved=d.get_decision(conn,old_id);new=d.get_decision(conn,new_id)
    assert new_id!=old_id and saved['status']=='superseded'
    for field in ('answer','author','action','resolved_at','created_at','spec_revision'):
        assert saved[field]==old[field]
    assert json.loads(saved['context'])['superseded_by']==new_id
    assert json.loads(new['context'])['supersedes']==old_id
    assert new['status']=='resolved' and new['action']=='approve'
    assert d._approved(conn,task.id,1)
    assert kb.get_task(conn,task.id).status=='ready'
    assert (workspace/'uncommitted.txt').read_text()=='Keep this existing work'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0]==1
    next_action=d.get_workflow(conn,task.id)['next_action']
    assert new_id in next_action and new['answer'] in next_action
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_principal_reconsidered'").fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind LIKE 'nfos_human_answer%'").fetchone()[0]==0
    after=snapshot(conn)
    assert reconsider(conn,old_id)==new_id
    assert snapshot(conn)==after


@pytest.mark.parametrize('failure', ['missing_pr','non_review'])
def test_failed_reconsidered_approval_rolls_back_decision_unblock_and_events(delivery,failure):
    conn,task=delivery
    old=human_block(conn,task,kind='impediment' if failure=='non_review' else 'review',
                   confirmed_pr=failure!='missing_pr')
    before=snapshot(conn)
    with pytest.raises(d.WorkflowError,match='confirmed PR|Only a delivery review'):
        reconsider(conn,old)
    assert snapshot(conn)==before


@pytest.mark.parametrize('field,value', [('homolog_sha',B),('candidate_tree','d'*40)])
def test_repeated_reconsideration_cannot_reuse_approval_for_changed_candidate(delivery,field,value):
    conn,task=delivery
    old=human_block(conn,task)
    reconsider(conn,old)
    wf=d.get_workflow(conn,task.id);state=json.loads(wf['state_json']);state[field]=value
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',(json.dumps(state),task.id));conn.commit()
    before=snapshot(conn)
    with pytest.raises(d.WorkflowError,match='changed'):
        reconsider(conn,old)
    assert not d._approved(conn,task.id,1)
    assert snapshot(conn)==before


def test_reconsideration_keeps_other_human_question_blocked(delivery):
    conn,task=delivery
    other=d.ask_principal(conn,task.id,task.current_run_id,kind='impediment',question='Which account is authorized?',context={})
    d.resolve_decision(conn,other,action='human',answer='Need the actual tenant identity',author='Principal')
    old=human_block(conn,task)
    reconsider(conn,old)
    assert kb.get_task(conn,task.id).status=='blocked'
    assert d.get_decision(conn,other)['status']=='human'


def test_previous_live_process_blocks_reconsideration_until_exit(delivery):
    conn,task=delivery
    proc=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.readline()'],
        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        old=human_block(conn,task,pid=proc.pid)
        before=snapshot(conn)
        with pytest.raises(d.OwnershipConflict,match='previous|termination'):
            reconsider(conn,old)
        assert snapshot(conn)==before and proc.poll() is None
        proc.communicate('exit\n',timeout=10)
        assert reconsider(conn,old)
        assert kb.get_task(conn,task.id).status=='ready'
    finally:
        if proc.poll() is None:proc.kill();proc.wait(timeout=10)


@pytest.mark.parametrize('invalid', ['reason','answer','worker_context'])
def test_reconsideration_requires_principal_and_concrete_reason_answer(delivery,monkeypatch,invalid):
    conn,task=delivery;old=human_block(conn,task)
    kwargs={invalid:' '} if invalid!='worker_context' else {}
    if invalid=='worker_context':monkeypatch.setenv('HERMES_KANBAN_TASK',task.id)
    before=snapshot(conn)
    with pytest.raises(d.WorkflowError,match='Principal|reason|answer'):
        reconsider(conn,old,**kwargs)
    assert snapshot(conn)==before


def test_cli_reconsider_uses_same_guarded_operation(delivery,tmp_path,monkeypatch):
    conn,task=delivery;old=human_block(conn,task,kind='impediment')
    request=tmp_path/'reconsider.json'
    request.write_text(json.dumps({'reason':'Existing spec already resolves this question',
                                  'answer':'Continue using the persisted scope'}))
    db=conn.execute('PRAGMA database_list').fetchone()[2]
    monkeypatch.setattr('sys.argv',['nfos_delivery','reconsider','--db',db,'--decision',old,
        '--resolution','continue','--input',str(request)])
    out=io.StringIO()
    with redirect_stdout(out):d.main()
    result=json.loads(out.getvalue())
    assert d.get_decision(conn,result['decision_id'])['action']=='continue'
    assert kb.get_task(conn,task.id).status=='ready'


def test_existing_decide_cannot_reverse_a_human_escalation(delivery):
    conn,task=delivery;old=human_block(conn,task)
    assert all(d._review_identity(conn,task.id).values())
    assert d._confirmed(conn,task.id,'pr',A)
    before=snapshot(conn)
    with pytest.raises(d.WorkflowError,match='already resolved'):
        d.resolve_decision(conn,old,action='approve',answer='The current spec already authorizes this',author='Principal')
    assert snapshot(conn)==before and kb.get_task(conn,task.id).status=='blocked'


def test_live_recorded_child_prevents_resume_after_worker_exit(delivery):
    conn,task=delivery;old=human_block(conn,task)
    child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.readline()'],
        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        receipt={'status':'stopping','worker_pid':987654321,'worker_started_at':1,
                 'descendants_json':json.dumps([{'pid':child.pid,'started_at':kb._process_start_time(child.pid)}])}
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
            (json.dumps({'nfos_cleanup':receipt}),task.current_run_id));conn.commit()
        before=snapshot(conn)
        with pytest.raises(d.OwnershipConflict,match='termination'):
            reconsider(conn,old)
        assert snapshot(conn)==before and child.poll() is None
        child.communicate('exit\n',timeout=10)
        assert reconsider(conn,old)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


def test_reconsideration_reviews_current_spec_then_rejects_stale_retry(delivery):
    conn,task=delivery;old=human_block(conn,task)
    # A persisted current revision can differ from the original escalation.
    # Reconsideration is a new review; an HTTP/CLI retry is not another review.
    conn.execute('UPDATE nfos_workflows SET spec_revision=2 WHERE task_id=?',(task.id,));conn.commit()
    new=reconsider(conn,old)
    assert d.get_decision(conn,old)['spec_revision']==1
    assert d.get_decision(conn,new)['spec_revision']==2
    assert d._approved(conn,task.id,2)
    conn.execute('UPDATE nfos_workflows SET spec_revision=3 WHERE task_id=?',(task.id,));conn.commit()
    with pytest.raises(d.WorkflowError,match='changed'):
        reconsider(conn,old)
    assert not d._approved(conn,task.id,3)


def test_changes_resume_same_card_without_publication_approval(delivery):
    conn,task=delivery;old=human_block(conn,task)
    new=reconsider(conn,old,action='changes',answer='Correct the requested behavior and repeat homologation')
    assert d.get_decision(conn,new)['action']=='changes'
    assert kb.get_task(conn,task.id).status=='ready'
    assert not d._approved(conn,task.id,1)


def test_old_human_question_can_be_reconsidered_after_a_later_run_ends(delivery):
    conn,task=delivery;old=human_block(conn,task)
    assert kb.unblock_task(conn,task.id)
    later=kb.claim_task(conn,task.id)
    assert later and later.current_run_id!=task.current_run_id
    assert kb.reclaim_task(conn,task.id)
    assert kb.block_task(conn,task.id,kind='needs_input',reason='Original question still needs a decision')
    # Blocking a ready card records its own terminal run as well.
    latest=conn.execute('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',(task.id,)).fetchone()[0]
    assert latest>=later.current_run_id
    new=reconsider(conn,old)
    assert d.get_decision(conn,new)['run_id']==latest
    assert d.get_decision(conn,old)['run_id']==task.current_run_id
    assert kb.get_task(conn,task.id).status=='ready'


def test_later_run_with_live_worker_still_prevents_reconsideration(delivery):
    conn,task=delivery;old=human_block(conn,task)
    assert kb.unblock_task(conn,task.id)
    later=kb.claim_task(conn,task.id)
    child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.readline()'],
        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?',(child.pid,later.current_run_id))
        kb._append_event(conn,task.id,'spawned',
            {'pid':child.pid,'worker_started_at':kb._process_start_time(child.pid)},run_id=later.current_run_id)
        conn.commit()
        assert kb.reclaim_task(conn,task.id,signal_fn=lambda *args:None)
        assert kb.block_task(conn,task.id,kind='needs_input',reason='Original question remains')
        before=snapshot(conn)
        with pytest.raises(d.OwnershipConflict,match='termination'):
            reconsider(conn,old)
        assert snapshot(conn)==before and child.poll() is None
        child.communicate('exit\n',timeout=10)
        assert reconsider(conn,old)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
