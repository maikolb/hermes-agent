"""Distinct candidate/HML identities require a Principal evidence decision."""
import json

import pytest

from hermes_cli import nfos_delivery as d
from tests.hermes_cli.test_nfos_candidate_delivery import A, B, TREE, delivery, homolog


def binding(conn, task, tmp_path):
    homolog(conn, task)
    effect = d.begin_effect(conn, task.id, task.current_run_id, operation='homolog', target='hml', candidate=A)
    d.reconcile_effect(conn, effect['id'], found=True, evidence={'readback': 'Actual deployed version', 'candidate': A, 'tree': 'd'*40, 'artifact': 'image:hml'})
    proof = tmp_path/'equivalence.json'
    proof.write_text(json.dumps({'diff': 'All candidate changes compared with tested HML; baseline reviewed'}))
    identity = {'candidate_sha': B, 'candidate_tree': TREE, 'homolog_sha': A, 'homolog_tree': 'd'*40,
                'baseline_sha': 'e'*40, 'scope': 'limited_delta', 'criteria': ['AC1'], 'evidence': [str(proof)]}
    decision = d.ask_principal(conn, task.id, task.current_run_id, kind='homologation',
        question='Review limited delta and remaining baseline; not publication', context={'homologation': identity})
    return decision, proof


def test_distinct_shas_keep_real_hml_and_require_separate_release_review(delivery, tmp_path):
    conn, task = delivery
    decision, _ = binding(conn, task, tmp_path)
    d.resolve_decision(conn, decision, action='continue', answer='Reviewed limited delta, baseline and evidence', author='Principal')
    d.release_project(conn, 'pilot', task.id, task.current_run_id)
    assert d.acquire_project(conn, 'pilot', task.id, task.current_run_id, B)
    pr = d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='pr1', candidate=B)
    d.reconcile_effect(conn, pr['id'], found=True, evidence={'readback': 'PR head confirmed', 'candidate': B})
    with pytest.raises(d.WorkflowError, match='review'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='pr1', candidate=B)
    review = d.ask_principal(conn, task.id, task.current_run_id, kind='review', question='Approve candidate?', context={})
    d.resolve_decision(conn, review, action='approve', answer='Candidate reviewed', author='Principal')
    merge = d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='pr1', candidate=B)
    d.reconcile_effect(conn, merge['id'], found=True, evidence={'readback': 'Integrated candidate', 'candidate': B, 'tree': TREE, 'integrated_sha': 'f'*40})
    state = json.loads(d.get_workflow(conn, task.id)['state_json'])
    assert state['homolog_sha'] == A
    assert state['candidate_sha'] == B
    assert state['homolog_deployment']['candidate'] == A
    assert not d.completion_ready(conn, task.id)
    d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Different candidate', state={'candidate_sha': '9'*40})
    with pytest.raises(d.WorkflowError):
        d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='pr1', candidate='9'*40)


def test_equivalence_cannot_be_self_declared_or_approve_publication(delivery, tmp_path):
    conn, task = delivery
    decision, _ = binding(conn, task, tmp_path)
    with pytest.raises(d.WorkflowError, match='review'):
        d.resolve_decision(conn, decision, action='approve', answer='Skip release review', author='Principal')
    d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Attempt unsupported binding', state={'candidate_sha': B})
    with pytest.raises(d.WorkflowError, match='homologation'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='pr1', candidate=B)


def test_changed_evidence_cannot_receive_stale_acceptance(delivery, tmp_path):
    conn, task = delivery
    decision, proof = binding(conn, task, tmp_path)
    proof.write_text('Changed after review request')
    with pytest.raises(d.WorkflowError, match='evidence'):
        d.resolve_decision(conn, decision, action='continue', answer='Accept old proof', author='Principal')
    assert d.get_decision(conn, decision)['status'] == 'pending'


def test_new_spec_invalidates_pending_homologation(delivery, tmp_path):
    conn, task = delivery
    decision, _ = binding(conn, task, tmp_path)
    spec = json.loads(d.get_spec(conn, task.id)['content'])
    spec['goal'] = 'Revised acceptance'
    d.save_spec(conn, task.id, task.current_run_id, spec, author='Claude TL', evidence={'session': 'new-spec'})
    with pytest.raises(d.WorkflowError, match='spec'):
        d.resolve_decision(conn, decision, action='continue', answer='Accept stale revision', author='Principal')


def test_evidence_changed_after_acceptance_prevents_pr(delivery, tmp_path):
    conn, task = delivery
    decision, proof = binding(conn, task, tmp_path)
    d.resolve_decision(conn, decision, action='continue', answer='Accepted recorded comparison', author='Principal')
    proof.write_text('Different evidence')
    with pytest.raises(d.WorkflowError, match='evidence'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='pr1', candidate=B)
