"""A reviewed legacy binding preserves evidence and cannot approve delivery."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_report_evidence import report_task, report, approve


def legacy(conn, task):
    saved=d.get_spec(conn,task.id)
    evidence=json.loads(saved['evidence']);evidence.pop('instruction_revision')
    conn.execute('UPDATE nfos_artifacts SET evidence=? WHERE id=?',(json.dumps(evidence),saved['id']))
    conn.execute('UPDATE tasks SET body=body || ? WHERE id=?',('\nOriginal adopted request',task.id))
    conn.commit()
    return d.get_spec(conn,task.id)


def inputs(conn,task,path):
    spec=d.get_spec(conn,task.id);current=kb.get_task(conn,task.id)
    encoded=json.dumps({'title':current.title,'body':current.body},sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return dict(spec_id=spec['id'],spec_sha256=hashlib.sha256(spec['content'].encode()).hexdigest(),
        instruction_revision=current.instruction_revision,
        instruction_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        reason='Reviewed original request and saved spec; unchanged task scope, legacy metadata missing.',
        evidence=[str(path)],author='Principal')


def test_reviewed_legacy_binding_saves_report_without_rewriting_spec_or_approving(report_task):
    conn,task,_,path=report_task
    saved=legacy(conn,task)
    before=[dict(r) for r in conn.execute('SELECT * FROM nfos_decisions')]
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.save_report(conn,task.id,task.current_run_id,report(str(path)))
    args=inputs(conn,task,path)
    binding=d.reconcile_legacy_spec(conn,task.id,**args)
    assert d.reconcile_legacy_spec(conn,task.id,**args)==binding
    assert d.get_spec(conn,task.id)==saved
    assert [dict(r) for r in conn.execute('SELECT * FROM nfos_decisions')]==before
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_legacy_spec_bound'").fetchone()[0]==1
    d.save_report(conn,task.id,task.current_run_id,report(str(path)))
    assert not d.completion_ready(conn,task.id)
    approve(conn,task)
    assert d.completion_ready(conn,task.id)
    current=kb.get_task(conn,task.id)
    kb.update_task_instruction(conn,task.id,body=current.body+'\nNew user constraint',author='Principal',expected_revision=1)
    assert not d.completion_ready(conn,task.id)
    with pytest.raises(d.WorkflowError,match='instruction|Instruction'):
        d.save_report(conn,task.id,task.current_run_id,report(str(path)))


@pytest.mark.parametrize('field',['spec_id','spec_sha256','instruction_revision','instruction_sha256'])
def test_stale_review_cannot_bind_current_legacy_spec(report_task,field):
    conn,task,_,path=report_task;legacy(conn,task)
    args=inputs(conn,task,path);args[field]=999 if field.endswith('id') or field.endswith('revision') else '0'*64
    with pytest.raises(d.WorkflowError,match='changed|match'):
        d.reconcile_legacy_spec(conn,task.id,**args)
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_legacy_spec_bound'").fetchone()[0]==0


def test_existing_binding_cannot_be_overridden_and_worker_cannot_self_reconcile(report_task,monkeypatch):
    conn,task,_,path=report_task
    with pytest.raises(d.WorkflowError,match='legacy|binding'):
        d.reconcile_legacy_spec(conn,task.id,**inputs(conn,task,path))
    legacy(conn,task)
    monkeypatch.setenv('HERMES_KANBAN_TASK',task.id)
    with pytest.raises(d.WorkflowError,match='Principal'):
        d.reconcile_legacy_spec(conn,task.id,**inputs(conn,task,path))
