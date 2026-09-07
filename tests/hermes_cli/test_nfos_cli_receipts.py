"""The real JSON CLI preserves diagnosis and fences stale receipt writers."""
import json
import sys
import pytest
from hermes_cli import nfos_delivery as d
from tests.hermes_cli.test_nfos_candidate_delivery import delivery, homolog, A


def cli(monkeypatch, tmp_path, payload, *args):
    path=tmp_path/'input.json'
    path.write_text(json.dumps(payload),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['nfos_delivery',*args,'--input',str(path)])
    d.main()


def test_impediment_cli_preserves_facts_attempts_and_explicit_context(delivery,tmp_path,monkeypatch):
    conn,task=delivery
    payload={'question':'Where is existing test access?',
             'facts':'HML uses the existing test tenant.',
             'attempted':['Read current private file names'],
             'next_decision':'Continue independent HML work while checking production access.',
             'context':{'checkpoint':'saved-state.json'}}
    cli(monkeypatch,tmp_path,payload,'ask','--task',task.id,'--run',str(task.current_run_id),'--kind','impediment')
    saved=d.pending_decisions(conn)[0]
    context=json.loads(saved['context'])
    assert context['facts']==payload['facts']
    assert context['attempted']==payload['attempted']
    assert context['next_decision']==payload['next_decision']
    assert context['checkpoint']=='saved-state.json'


@pytest.mark.parametrize('wrong',['run','task'])
def test_cli_cannot_reconcile_from_another_task_or_execution(delivery,tmp_path,monkeypatch,wrong):
    conn,task=delivery
    homolog(conn,task)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=A)
    payload={'found':True,'evidence':{'readback':'Actual PR response','candidate':A,'url':'https://example.test/pr/1'}}
    task_id='t_another' if wrong=='task' else task.id
    run_id=task.current_run_id+1 if wrong=='run' else task.current_run_id
    with pytest.raises(d.WorkflowError):
        cli(monkeypatch,tmp_path,payload,'reconcile','--task',task_id,'--run',str(run_id),'--effect-id',effect['id'])
    assert conn.execute('SELECT status FROM nfos_effects WHERE id=?',(effect['id'],)).fetchone()[0]=='unknown'


def test_current_execution_can_reconcile_the_previous_runs_unknown_effect(delivery,tmp_path,monkeypatch):
    conn,task=delivery
    homolog(conn,task)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=A)
    conn.execute('UPDATE nfos_effects SET run_id=0 WHERE id=?',(effect['id'],));conn.commit()
    cli(monkeypatch,tmp_path,{'found':True,'evidence':{'readback':'Exact remote PR','candidate':A}},
        'reconcile','--task',task.id,'--run',str(task.current_run_id),'--effect-id',effect['id'])
    assert conn.execute('SELECT status FROM nfos_effects WHERE id=?',(effect['id'],)).fetchone()[0]=='confirmed'
