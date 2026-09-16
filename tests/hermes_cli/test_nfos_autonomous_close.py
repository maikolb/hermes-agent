import base64
import json

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_principal_review as review
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept


def prepare(ctx, monkeypatch):
    conn, task, spec, artifact = ctx
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False, 'result_review': True})
    accept(conn, task, 'spec_review')
    image = artifact.parent / 'result.png'
    image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII='))
    d.save_report(conn, task.id, task.current_run_id, {'summary': 'Investigation completed; original receipt absent',
        'partial_delivery': True, 'criteria': [{'id':'C1','status':'NOT_RUN','evidence':['inventory','image']}],
        'artifacts':[{'id':'inventory','path':str(artifact)},{'id':'image','path':str(image)}]})
    return conn, task, artifact, image


def assessment(artifact):
    return {'request_alignment':'Investigation exhausted the available local records',
            'scope_assessment':'No functional request abandoned and no extra mutation',
            'resolution':'Investigação concluída. O recibo histórico não está disponível.',
            'learning':'Preservar o recibo antes de alterar dados; ausência histórica não prova perda.',
            'criteria':[{'id':'C1','verdict':'observe','observation':'Sem recibo histórico, preservação permanece não comprovada.','evidence':[str(artifact)]}]}


def test_principal_can_close_with_observation_without_rewriting_not_run(task_context, monkeypatch):
    conn, task, artifact, image = prepare(task_context, monkeypatch)
    pending=d.pending_decisions(conn)
    assert len(pending)==1, 'inconclusive report must reach the Principal instead of automatic refusal'
    assert not kb.complete_task(conn,task.id,result='Worker cannot self approve')
    d.resolve_decision(conn,pending[0]['id'],action='continue',answer='Encerrar com observação',author='Principal',assessment=assessment(artifact))
    assert kb.complete_task(conn,task.id,result='Worker summary')
    assert kb.get_task(conn,task.id).status=='done'
    report=json.loads(d._artifact(conn,task.id,'report')['content'])
    assert report['criteria'][0]['status']=='NOT_RUN'
    assert review.observed_criteria(conn,task.id)=={'C1'}
    event=json.loads(conn.execute("select payload from task_events where task_id=? and kind='completed' order by id desc limit 1",(task.id,)).fetchone()['payload'])
    assert str(image) in event['artifacts']
    assert 'recibo histórico' in kb.get_task(conn,task.id).result
    assert 'Observações' in kb.get_task(conn,task.id).result
    from tools.memory_tool import get_memory_dir
    assert 'Preservar o recibo' in (get_memory_dir()/'MEMORY.md').read_text()


def test_observation_does_not_accept_missing_or_modified_evidence(task_context, monkeypatch):
    conn,task,artifact,image=prepare(task_context,monkeypatch)
    decision=d.pending_decisions(conn)[0]
    d.resolve_decision(conn,decision['id'],action='continue',answer='Encerrar com observação',author='Principal',assessment=assessment(artifact))
    artifact.write_text('changed after acceptance')
    assert not kb.complete_task(conn,task.id,result='Cannot reuse changed evidence')


def test_legacy_human_closure_question_returns_to_principal_once(task_context, monkeypatch):
    conn,task,artifact,image=prepare(task_context,monkeypatch)
    question=d.ask_principal(conn,task.id,task.current_run_id,kind='impediment',question='Should the owner authorize closure with missing old receipt?',context={})
    d.resolve_decision(conn,question,action='human',answer='Owner, authorize closure?',author='Principal')
    before=kb.get_task(conn,task.id).status
    d.reconcile_incomplete_reviews(conn)
    rows=[r for r in d.pending_decisions(conn) if json.loads(r['context']).get('closure_reconsideration_of')==question]
    assert len(rows)==1
    assert d.get_decision(conn,question)['status']=='human', 'No synthetic owner answer'
    assert kb.get_task(conn,task.id).status==before
    count=conn.execute('select count(*) from nfos_decisions').fetchone()[0]
    d.reconcile_incomplete_reviews(conn)
    assert conn.execute('select count(*) from nfos_decisions').fetchone()[0]==count


def test_memory_failure_does_not_return_closure_to_owner(task_context,monkeypatch):
    conn,task,artifact,image=prepare(task_context,monkeypatch)
    decision=d.pending_decisions(conn)[0]
    d.resolve_decision(conn,decision['id'],action='continue',answer='Close with observation',author='Principal',assessment=assessment(artifact))
    from tools.memory_tool import MemoryStore
    monkeypatch.setattr(MemoryStore,'add',lambda *a,**kw:{'success':False,'error':'Consolidation needed'})
    assert kb.complete_task(conn,task.id,result='Resolved')
    row=conn.execute("select payload from task_events where task_id=? and kind='nfos_learning_pending'",(task.id,)).fetchone()
    assert 'Preservar o recibo' in row['payload']
    assert kb.get_task(conn,task.id).status=='done'
