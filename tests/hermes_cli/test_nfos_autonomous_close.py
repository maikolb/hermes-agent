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


def test_retained_financial_escalation_has_one_open_question(task_context, monkeypatch):
    conn, task, _, _ = prepare(task_context, monkeypatch)
    root = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
        question='GitHub billing prevents the required CI from running', context={})
    original = 'Maikol, can you regularize GitHub billing and confirm when CI is available?'
    d.resolve_decision(conn, root, action='human', answer=original, author='Principal')
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone())
    d.reconcile_incomplete_reviews(conn)
    review_id = json.loads(d.get_decision(conn, root)['context'])['autonomous_closure_review']
    answer = 'The original billing question remains indispensable; do not send it again.'
    d.resolve_decision(conn, review_id, action='human', answer=answer, author='Principal')
    count = conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0]
    for _ in range(3):
        d.reconcile_incomplete_reviews(conn)
        d.resolve_decision(conn, review_id, action='human', answer=answer, author='Principal')
    assert conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0] == count
    assert [r['id'] for r in conn.execute("SELECT id FROM nfos_decisions WHERE status='human'")] == [root]
    assert d.get_decision(conn, root)['answer'] == original
    reviewed = d.get_decision(conn, review_id)
    assert reviewed['answer'] == answer and reviewed['author'] == 'Principal'
    assert json.loads(reviewed['context'])['superseded_by'] == root
    assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone()) == before
    events = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE kind='nfos_principal_resolved'")]
    assert not any(e['decision_id'] == review_id for e in events), 'No duplicate human notification'


def test_reconciliation_consolidates_legacy_review_chain_without_unblocking(task_context, monkeypatch):
    conn, task, _, _ = prepare(task_context, monkeypatch)
    root = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
        question='GitHub billing prevents CI', context={})
    d.resolve_decision(conn, root, action='human', answer='Owner, can you fix billing?', author='Principal')
    d.reconcile_incomplete_reviews(conn)
    child = json.loads(d.get_decision(conn, root)['context'])['autonomous_closure_review']
    # Persist the state produced by the old reconciler, without invoking the fix.
    ctx = json.loads(d.get_decision(conn, child)['context'])
    ctx['autonomous_closure_review'] = 'legacy-grandchild'
    conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer='Billing still requires the owner',context=? WHERE id=?", (json.dumps(ctx), child))
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,'impediment','Review of the review',?,?,1)",
        ('legacy-grandchild', task.id, task.current_run_id, json.dumps({'closure_reconsideration_of': child}), d.get_workflow(conn, task.id)['spec_revision']))
    conn.commit()
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone())
    d.reconcile_incomplete_reviews(conn)
    assert d.get_decision(conn, root)['status'] == 'human'
    assert d.get_decision(conn, child)['status'] == 'superseded'
    assert d.get_decision(conn, child)['answer'] == 'Billing still requires the owner'
    assert d.get_decision(conn, 'legacy-grandchild')['status'] == 'superseded'
    assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone()) == before
    count = conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0]
    d.reconcile_incomplete_reviews(conn)
    assert conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0] == count


def test_distinct_human_causes_are_not_merged(task_context, monkeypatch):
    conn, task, _, _ = prepare(task_context, monkeypatch)
    causes = [('GitHub billing prevents CI', 'Owner, can you regularize billing?'),
              ('Destination account is unavailable', 'Owner, which account should receive access?')]
    questions = [d.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question=question, context={})
                 for question, _ in causes]
    for did, (_, answer) in zip(questions, causes):
        d.resolve_decision(conn, did, action='human', answer=answer, author='Principal')
    d.reconcile_incomplete_reviews(conn)
    assert {r['id'] for r in conn.execute("SELECT id FROM nfos_decisions WHERE status='human'")} == set(questions)
    assert all(json.loads(d.get_decision(conn, did)['context']).get('autonomous_closure_review') for did in questions)


def test_escalation_lineage_rejects_missing_cross_card_and_cyclic_links(task_context, monkeypatch):
    conn, task, _, _ = prepare(task_context, monkeypatch)
    root = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Billing prevents CI', context={})
    d.resolve_decision(conn, root, action='human', answer='Owner, can you regularize billing?', author='Principal')
    for task_id, parent in [(task.id, 'missing'), ('different-card', root), (task.id, 'test-review')]:
        row = {'id': 'test-review', 'task_id': task_id, 'context': json.dumps({'closure_reconsideration_of': parent})}
        assert d._open_human_escalation_root(conn, row)[0] is None
    assert d.get_decision(conn, root)['status'] == 'human'
