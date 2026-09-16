import base64
import json
from contextlib import nullcontext

import pytest

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


def functional_partial(ctx, monkeypatch, other_observation=False):
    conn, task, spec, artifact = ctx
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False, 'result_review': True})
    spec['criteria'][0].update(mandatory=True, text='The requested Basic license is active')
    if other_observation:
        spec['criteria'].append({'id': 'C2', 'text': 'Historical receipt available'})
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'source': 'original request'})
    if other_observation:
        decision = d.pending_decisions(conn)[0]
        d.resolve_decision(conn, decision['id'], action='continue', answer='Review both criteria', author='Principal',
                           assessment={'request_alignment': 'License and historical receipt',
                                       'scope_assessment': 'Original requirement and supporting evidence',
                                       'criteria': [{'id': c['id'], 'verdict': 'accept', 'observation': c['text']}
                                                    for c in spec['criteria']]})
    else:
        accept(conn, task, 'spec_review')
    child = kb.create_task(conn, title='Finish the same Basic license request',
                           assignee=task.assignee, parents=[task.id], workspace_kind='scratch')
    report = {
        'summary': 'Accounts delivered; Basic license still missing',
        'criteria': [{'id': 'C1', 'status': 'FAIL', 'evidence': ['readback']}],
        'artifacts': [{'id': 'readback', 'path': str(artifact)}],
        'blockers': [{'id': 'license-missing', 'status': 'unresolved'}],
        'delivery': {'partial_delivery': not other_observation, 'follow_ups': [child]},
    }
    if other_observation:
        report['criteria'].append({'id': 'C2', 'status': 'NOT_RUN', 'evidence': ['readback']})
    d.save_report(conn, task.id, task.current_run_id, report)
    proposed = assessment(artifact)
    proposed.update(resolution='Partial delivery; child keeps the missing license',
                    scope_assessment='The original mandatory license remains unfulfilled')
    proposed['criteria'][0]['observation'] = 'FAIL retained; functional remainder transferred to the child'
    if other_observation:
        proposed['criteria'].append({'id': 'C2', 'verdict': 'observe',
                                     'observation': 'Historical receipt unavailable', 'evidence': [str(artifact)]})
    return conn, task, child, proposed


def test_functional_failure_cannot_close_as_observation_with_dependent_child(task_context, monkeypatch):
    conn, task, child, proposed = functional_partial(task_context, monkeypatch)
    decision = d.pending_decisions(conn)[0]
    assert kb.get_task(conn, child).status == 'todo'
    assert d.continuation_links(conn, task.id)['children'] == []
    with pytest.raises(d.WorkflowError, match='functional') as refused:
        d.resolve_decision(conn, decision['id'], action='continue', answer='Close partial',
                           author='Principal', assessment=proposed)
    assert 'same card' in str(refused.value) and 'continuation_of' in str(refused.value)
    assert not kb.complete_task(conn, task.id, result='Partial accounts')
    assert kb.get_task(conn, task.id).status == 'running'
    assert d.get_decision(conn, decision['id'])['status'] == 'pending'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='human'").fetchone()
    # Finish the original obligation in place, rather than wait for the gated child.
    artifact = task_context[3]
    artifact.write_text('Basic license active; entitlement allowed\n')
    d.save_report(conn, task.id, task.current_run_id, {
        'summary': 'Basic license active',
        'criteria': [{'id': 'C1', 'status': 'PASS', 'evidence': ['readback']}],
        'artifacts': [{'id': 'readback', 'path': str(artifact)}],
        'blockers': [{'id': 'license-missing', 'status': 'resolved'}],
    })
    accept(conn, task, 'final_review', artifact)
    assert kb.complete_task(conn, task.id, result='Basic license verified')
    assert kb.get_task(conn, child).status == 'ready'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='human'").fetchone()


@pytest.mark.parametrize('other_observation', [False, True])
def test_legacy_observation_does_not_hide_functional_failure_or_partial_label(task_context, monkeypatch, other_observation):
    from gateway.kanban_watchers import _progress_outcome
    conn, task, _, proposed = functional_partial(task_context, monkeypatch, other_observation)
    decision = d.pending_decisions(conn)[0]
    # Persist a previously accepted review, as in the incident, in this isolated DB.
    context = json.loads(decision['context'])
    context['assessment'] = proposed
    conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',author='Principal',context=? WHERE id=?",
                 (json.dumps(context), decision['id']))
    conn.commit()
    assert review.final_assessment(conn, task.id) == proposed
    assert review.observed_criteria(conn, task.id) == ({'C2'} if other_observation else set())
    assert not kb.complete_task(conn, task.id, result='Legacy partial')
    monkeypatch.setattr(kb, 'connect_closing', lambda **kwargs: nullcontext(conn))
    assert _progress_outcome('pilot', task.id) == 'parcial'


def test_explicit_partial_label_takes_precedence_over_documentary_observation(task_context, monkeypatch):
    from gateway.kanban_watchers import _progress_outcome
    conn, task, artifact, _ = prepare(task_context, monkeypatch)
    decision = d.pending_decisions(conn)[0]
    d.resolve_decision(conn, decision['id'], action='continue', answer='Close with historical limitation',
                       author='Principal', assessment=assessment(artifact))
    assert kb.complete_task(conn, task.id, result='Investigation complete')
    monkeypatch.setattr(kb, 'connect_closing', lambda **kwargs: nullcontext(conn))
    assert _progress_outcome('pilot', task.id) == 'parcial'


def test_mitigated_historical_failure_can_close_with_functional_delivery(task_context, monkeypatch):
    from gateway.kanban_watchers import _progress_outcome
    conn, task, artifact, image = prepare(task_context, monkeypatch)
    spec = json.loads(d.get_spec(conn, task.id)['content'])
    spec['criteria'][0].update(mandatory=True, text='Historical internal evidence contained no secret')
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'source': 'original request'})
    accept(conn, task, 'spec_review')
    artifact.write_text('Functional outcome verified. Shareable evidence sanitized; historical failure retained.\n')
    d.save_report(conn, task.id, task.current_run_id, {
        'summary': 'Functional delivery verified; historical evidence issue mitigated',
        'criteria': [{'id': 'C1', 'status': 'FAIL', 'evidence': ['inventory', 'image']}],
        'artifacts': [{'id': 'inventory', 'path': str(artifact)}, {'id': 'image', 'path': str(image)}],
        'blockers': [{'id': 'evidence-hygiene', 'status': 'resolved'}],
        'delivery': {'functional_delivery': True, 'partial_delivery': False},
    })
    proposed = assessment(artifact)
    proposed.update(resolution='Functional outcome verified; retain the mitigated historical failure',
                    scope_assessment='No functional blocker and no outstanding work transferred')
    proposed['criteria'][0]['observation'] = 'Historical hygiene FAIL retained; shareable evidence sanitized'
    decision = d.pending_decisions(conn)[0]
    d.resolve_decision(conn, decision['id'], action='continue', answer='Close with explicit observation',
                       author='Principal', assessment=proposed)
    assert kb.complete_task(conn, task.id, result='Verified delivery with historical observation')
    assert kb.get_task(conn, task.id).status == 'done'
    assert json.loads(d._artifact(conn, task.id, 'report')['content'])['criteria'][0]['status'] == 'FAIL'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='human'").fetchone()
    monkeypatch.setattr(kb, 'connect_closing', lambda **kwargs: nullcontext(conn))
    assert _progress_outcome('pilot', task.id) == 'concluído com observações'


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
