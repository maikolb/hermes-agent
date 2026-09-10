"""Requested-target closeout through real SQLite and public workflow functions."""
import json
import pytest
from hermes_cli import nfos_delivery as d, kanban_db as kb
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept, ask, save_report


def scope(environment='TEST', operation='homolog'):
    return dict(environment=environment,target='https://fixture.invalid/'+environment,
                source='fixture:owner:1',authorization_message='Deliver only to '+environment,
                verification_operation=operation)


def prepare(context, target_scope):
    conn, task, spec, artifact = context
    if target_scope:
        d.save_spec(conn,task.id,task.current_run_id,dict(spec,delivery_destination=target_scope),
                    author='Claude TL',evidence={'session':'fixture-tl-destination'})
    accept(conn,task,'spec_review')
    sha,tree,integrated='a'*40,'c'*40,'b'*40
    assert d.acquire_project(conn,'fixture',task.id,task.current_run_id,sha)
    d.advance(conn,task.id,task.current_run_id,'homolog',next_action='verify',state={
        'candidate_sha':sha,'candidate_tree':tree,'homolog_sha':sha,'homolog_evidence':[str(artifact)]})
    target=target_scope['target'] if target_scope and target_scope['verification_operation']=='homolog' else 'fixture:hml'
    for op,dest in [('homolog',target),('pr','fixture:pr')]:
        effect=d.begin_effect(conn,task.id,task.current_run_id,operation=op,target=dest,candidate=sha)
        d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'fixture observed',
            'candidate':sha,'tree':tree,'artifact':'fixture-image','behavior_evidence':[str(artifact)]})
    d.resolve_decision(conn,ask(conn,task,'review'),action='approve',answer='Reviewed',author='Principal')
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target='fixture:pr',candidate=sha)
    d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'integrated','candidate':sha,'tree':tree,'integrated_sha':integrated})
    save_report(conn,task,artifact)
    return conn,task,artifact


@pytest.mark.parametrize('task_context',['code'],indirect=True)
@pytest.mark.parametrize('environment',['TEST','HML','staging','preview/customer-7'])
def test_close_at_requested_environment_without_production(task_context,environment):
    conn,task,artifact=prepare(task_context,scope(environment))
    assert not d.completion_ready(conn,task.id)
    accept(conn,task,'final_review',artifact)
    assert d.completion_ready(conn,task.id)
    assert kb.complete_task(conn,task.id,result='Verified at requested destination')
    assert kb.get_task(conn,task.id).status=='done'
    assert not conn.execute("select 1 from nfos_effects where operation='deploy'").fetchone()


@pytest.mark.parametrize('task_context',['code'],indirect=True)
def test_wrong_destination_and_unauthorized_promotion_refused(task_context):
    conn,task,artifact=prepare(task_context,scope())
    for op in ('homolog','deploy'):
        with pytest.raises(d.WorkflowError,match='destination'):
            d.begin_effect(conn,task.id,task.current_run_id,operation=op,target='https://www.fixture.invalid',candidate='a'*40)
    accept(conn,task,'final_review',artifact)
    d.advance(conn,task.id,task.current_run_id,'report',next_action='reconcile',state={'homolog_sha':'d'*40})
    assert not d.completion_ready(conn,task.id)


@pytest.mark.parametrize('task_context',['code'],indirect=True)
@pytest.mark.parametrize('mutation',['spec','report','evidence','receipt'])
def test_stale_acceptance_or_evidence_cannot_close(task_context,mutation):
    conn,task,artifact=prepare(task_context,scope())
    accept(conn,task,'final_review',artifact)
    assert d.completion_ready(conn,task.id)
    if mutation=='spec':
        spec=json.loads(d.get_spec(conn,task.id)['content']);spec['delivery_destination']=scope('OTHER')
        d.save_spec(conn,task.id,task.current_run_id,spec,author='Claude TL',evidence={'session':'updated'})
    elif mutation=='report':save_report(conn,task,artifact)
    elif mutation=='evidence':artifact.write_text('changed')
    else:
        effect=conn.execute("select * from nfos_effects where operation='homolog'").fetchone()
        evidence=json.loads(effect['evidence']);evidence.pop('behavior_evidence')
        d.reconcile_effect(conn,effect['id'],found=True,evidence=evidence)
    assert not d.completion_ready(conn,task.id)


@pytest.mark.parametrize('task_context',['code'],indirect=True)
@pytest.mark.parametrize('explicit',[True,False])
def test_requested_or_legacy_production_still_requires_actual_deploy(task_context,explicit):
    selected=scope('production','deploy') if explicit else None
    conn,task,artifact=prepare(task_context,selected)
    accept(conn,task,'final_review',artifact)
    assert not d.completion_ready(conn,task.id)
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='deploy',target=selected['target'] if selected else 'fixture:prod',candidate='b'*40)
    d.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'destination observed','candidate':'b'*40,
        'tree':'c'*40,'artifact':'fixture-image','behavior_evidence':[str(artifact)]})
    accept(conn,task,'final_review',artifact)
    assert d.completion_ready(conn,task.id)


@pytest.mark.parametrize('task_context',['code'],indirect=True)
@pytest.mark.parametrize('field',['environment','target','source','authorization_message','verification_operation'])
def test_incomplete_destination_refused(task_context,field):
    conn,task,spec,_=task_context
    invalid=scope();invalid.pop(field)
    with pytest.raises(d.WorkflowError,match='delivery_destination'):
        d.save_spec(conn,task.id,task.current_run_id,dict(spec,delivery_destination=invalid),author='Claude TL',evidence={'session':'fixture'})


REPOSITORY='https://github.com/fixture-org/fixture-repo'


def review_scope():
    return dict(environment='review',target=REPOSITORY,source='fixture:owner:2',
                authorization_message='Branch, CI and review PR only; no HML, merge or deploy',
                verification_operation='pr')


def pr_evidence(sha,tree,**overrides):
    evidence={'readback':'PR observed','candidate':sha,'tree':tree,
              'pull_request':REPOSITORY+'/pull/7','ci_status':'success'}
    evidence.update(overrides)
    return evidence


def prepare_review_pr(context,target_scope=None):
    """Accepted local candidate with no homologation, as a review-PR-only phase has."""
    conn,task,spec,artifact=context
    if target_scope:
        d.save_spec(conn,task.id,task.current_run_id,dict(spec,delivery_destination=target_scope),
                    author='Claude TL',evidence={'session':'fixture-tl-review-pr'})
    accept(conn,task,'spec_review')
    sha,tree='a'*40,'c'*40
    assert d.acquire_project(conn,'fixture',task.id,task.current_run_id,sha)
    d.advance(conn,task.id,task.current_run_id,'implement',next_action='open review PR',
              state={'candidate_sha':sha,'candidate_tree':tree})
    return conn,task,artifact,sha,tree


@pytest.mark.parametrize('task_context',['code'],indirect=True)
def test_review_pr_destination_closes_without_homolog_merge_or_deploy(task_context):
    conn,task,artifact,sha,tree=prepare_review_pr(task_context,review_scope())
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target=REPOSITORY,candidate=sha)
    assert effect['execute']
    d.reconcile_effect(conn,effect['id'],found=True,evidence=pr_evidence(sha,tree))
    assert json.loads(d.get_workflow(conn,task.id)['state_json'])['review_pr']['pull_request']==REPOSITORY+'/pull/7'
    for op in ('homolog','merge','deploy','staging_pr'):
        with pytest.raises(d.WorkflowError,match='review PR'):
            d.begin_effect(conn,task.id,task.current_run_id,operation=op,target=REPOSITORY,candidate=sha)
    d.resolve_decision(conn,ask(conn,task,'review'),action='approve',answer='Reviewed',author='Principal')
    save_report(conn,task,artifact)
    assert not d.completion_ready(conn,task.id)
    accept(conn,task,'final_review',artifact)
    assert d.completion_ready(conn,task.id)
    assert kb.complete_task(conn,task.id,result='Review PR opened with green CI')
    assert kb.get_task(conn,task.id).status=='done'
    assert not conn.execute("select 1 from nfos_effects where operation in ('homolog','merge','deploy')").fetchone()


@pytest.mark.parametrize('task_context',['code'],indirect=True)
def test_review_pr_destination_requires_exact_repository_and_candidate(task_context):
    conn,task,artifact,sha,tree=prepare_review_pr(task_context,review_scope())
    with pytest.raises(d.WorkflowError,match='exact delivery destination'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target=REPOSITORY+'-other',candidate=sha)
    with pytest.raises(d.WorkflowError,match='accepted local candidate'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target=REPOSITORY,candidate='f'*40)


@pytest.mark.parametrize('task_context',['code'],indirect=True)
@pytest.mark.parametrize('broken',[{'pull_request':'https://github.com/fixture-org/fixture-repo'},
                                   {'ci_status':'failure'},{'ci_status':None},{'tree':'d'*40}])
def test_review_pr_destination_requires_pr_and_ci_evidence(task_context,broken):
    conn,task,artifact,sha,tree=prepare_review_pr(task_context,review_scope())
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target=REPOSITORY,candidate=sha)
    with pytest.raises(d.WorkflowError,match='Review PR readback'):
        d.reconcile_effect(conn,effect['id'],found=True,evidence={**pr_evidence(sha,tree),**broken})
    assert not d.completion_ready(conn,task.id)


@pytest.mark.parametrize('task_context',['code'],indirect=True)
def test_review_pr_destination_needs_repository_url(task_context):
    conn,task,spec,_=task_context
    with pytest.raises(d.WorkflowError,match='GitHub repository'):
        d.save_spec(conn,task.id,task.current_run_id,dict(spec,delivery_destination=dict(review_scope(),target='https://fixture.invalid/review')),
                    author='Claude TL',evidence={'session':'fixture'})


@pytest.mark.parametrize('task_context',['code'],indirect=True)
def test_pr_without_destination_still_requires_homologation(task_context):
    conn,task,artifact,sha,tree=prepare_review_pr(task_context,None)
    with pytest.raises(d.WorkflowError,match='homologation acceptance'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target=REPOSITORY,candidate=sha)
