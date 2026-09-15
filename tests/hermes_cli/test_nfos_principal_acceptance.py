"""Real SQLite acceptance path; all requests and evidence here are synthetic."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as d
from hermes_cli import nfos_principal_review as review


@pytest.fixture
def task_context(tmp_path, monkeypatch, request):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': {
        'principal_validation': True, 'worker_model': 'gpt-5.6-luna',
        'worker_provider': 'openai-codex', 'worker_reasoning_effort': 'high'}}}))
    with kb.connect_closing() as conn:
        delivery_type = getattr(request, 'param', 'report')
        request_id = d.receive_request(conn, source={'platform': 'fixture', 'chat_id': 'local', 'thread_id': 'test', 'message_id': '1'},
                                    text='Verify the count', project={'board': 'fixture', 'profile': 'default', 'delivery_type': delivery_type, 'repo_path': str(tmp_path)})
        claim = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, request_id, claim['claim_token'], pid=os.getpid())
        workspace = tmp_path / task.id
        workspace.mkdir()
        kb.set_workspace_path(conn, task.id, str(workspace))
        spec = {'goal': 'Verify count', 'criteria': [{'id': 'C1', 'text': 'The count is 31'}],
                'steps': ['Read the count and compare'], 'delivery_type': delivery_type}
        d.save_spec(conn, task.id, task.current_run_id, spec, author='Claude TL', evidence={'session': 'synthetic-tl'})
        artifact = workspace / 'count.txt'
        artifact.write_text('count=31\n')
        yield conn, kb.get_task(conn, task.id), spec, artifact


def assessment(artifact=None):
    row = {'id': 'C1', 'verdict': 'accept', 'observation': 'Fixture observation: count equals requested 31'}
    if artifact:
        row['evidence'] = [str(artifact)]
    return {'request_alignment': 'Count request covered by C1', 'scope_assessment': 'Only count verification', 'criteria': [row]}


def ask(conn, task, kind):
    return d.ask_principal(conn, task.id, task.current_run_id, kind=kind, question='Review current artifact', context={})


def accept(conn, task, kind, artifact=None):
    decision = ask(conn, task, kind)
    d.resolve_decision(conn, decision, action='continue', answer='Reviewed', author='Principal', assessment=assessment(artifact))
    return decision


def save_report(conn, task, artifact, status='PASS'):
    d.save_report(conn, task.id, task.current_run_id, {'summary': 'Count verified',
        'criteria': [{'id': 'C1', 'status': status, 'evidence': [str(artifact)]}], 'artifacts': [str(artifact)]})


def test_spec_requires_principal_and_reuses_pending_decision(task_context):
    conn, task, _, _ = task_context
    assert len(d.pending_decisions(conn)) == 1
    first = ask(conn, task, 'spec_review')
    assert ask(conn, task, 'spec_review') == first
    with pytest.raises(d.WorkflowError, match='spec acceptance'):
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='work')
    with pytest.raises(d.WorkflowError, match='spec acceptance'):
        d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='fixture', candidate='sha')
    accept(conn, task, 'spec_review')
    d.advance(conn, task.id, task.current_run_id, 'implement', next_action='work')


def test_record_mode_still_requires_real_spec_review(task_context, monkeypatch):
    conn, task, _, _ = task_context
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    assert review.required(conn, task.id)
    decision = ask(conn, task, 'spec_review')
    assert d.get_decision(conn, decision)['status'] == 'pending'
    assert not review.accepted(conn, task.id, 'spec_review')
    accept(conn, task, 'spec_review')
    assert review.accepted(conn, task.id, 'spec_review')


def test_record_mode_final_review_does_not_auto_accept(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    decision = ask(conn, task, 'final_review')
    assert d.get_decision(conn, decision)['status'] == 'pending'
    assert not review.accepted(conn, task.id, 'final_review')


def test_record_mode_rechecks_reviewed_artifact_bytes(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    accept(conn, task, 'final_review', artifact)
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    artifact.write_text('count=99\n')
    assert d.completion_evidence_check(conn, task.id) is None


def test_mandatory_artifact_can_be_reviewed_without_network_probe(task_context, monkeypatch):
    conn, task, spec, artifact = task_context
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    spec['criteria'][0]['mandatory'] = True
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'session': 'fixture'})
    assert not review.accepted(conn, task.id, 'spec_review')
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    assert not d.completion_ready(conn, task.id)
    accept(conn, task, 'final_review', artifact)
    assert d.completion_ready(conn, task.id)


def test_internal_rework_does_not_request_owner_or_create_another_card(task_context, monkeypatch):
    conn, task, spec, artifact = task_context
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    spec['criteria'][0]['mandatory'] = True
    d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'session': 'fixture'})
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    decisions = d.pending_decisions(conn)
    assert len(decisions) == 1 and decisions[0]['kind'] == 'final_review'
    d.resolve_decision(conn, decisions[0]['id'], action='changes', answer='Read the original count source', author='Principal')
    assert kb.get_task(conn, task.id).status == 'running'
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='human'").fetchone()
    artifact.write_text('Original count source read: count=31\n')
    save_report(conn, task, artifact)
    accept(conn, task, 'final_review', artifact)
    assert kb.complete_task(conn, task.id, result='Count verified from original source')
    assert d.get_workflow(conn, task.id)['stage'] == 'done'
    assert d.get_workflow(conn, task.id)['next_action'] == ''
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_green_probe_does_not_turn_failed_outcome_into_pass(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    spec = d.get_spec(conn, task.id)
    monkeypatch.setattr(d, '_mandatory_effective', lambda *a: {'C1': {'status': 'PASS', 'revision': 1}})
    report = {'summary': 'Counter passed but content is incorrect',
              'criteria': [{'id': 'C1', 'status': 'FAIL', 'evidence': [str(artifact)]}],
              'artifacts': [str(artifact)]}
    d._apply_measurements_to_report(conn, task.id, spec, report)
    assert report['criteria'][0]['status'] == 'FAIL'
    assert report['measurements']['C1']['status'] == 'PASS'


def test_owner_guidance_is_durable_idempotent_and_preserves_card(task_context):
    conn, task, _, _ = task_context
    source = {'platform':'vigilia','actor':'Maikol','message_id':'fixture-guidance-1'}
    result = d.receive_owner_guidance(conn, task.id, text='Preserve the requested destination', source=source)
    again = d.receive_owner_guidance(conn, task.id, text='Preserve the requested destination', source=source)
    assert again == dict(result, duplicate=True)
    row = d.get_decision(conn, result['decision_id'])
    assert row['status'] == 'pending' and row['kind'] == 'impediment'
    assert json.loads(row['context'])['source']['actor'] == 'Maikol'
    assert conn.execute("SELECT count(*) FROM task_comments WHERE task_id=? AND author='Maikol'", (task.id,)).fetchone()[0] == 1
    assert kb.get_task(conn, task.id).body == task.body
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    with pytest.raises(d.WorkflowError, match='different instruction'):
        d.receive_owner_guidance(conn, task.id, text='Different request', source=source)


def test_worker_cannot_self_accept_or_supply_prefilled_assessment(task_context, monkeypatch):
    conn, task, _, _ = task_context
    decision = ask(conn, task, 'spec_review')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    with pytest.raises(d.WorkflowError, match='Principal'):
        d.resolve_decision(conn, decision, action='continue', answer='self accepted', author='Principal', assessment=assessment())
    assert d.get_decision(conn, decision)['status'] == 'pending'


@pytest.mark.parametrize('invalid', [{}, {'criteria': []}, {'request_alignment': 'ok', 'scope_assessment': 'ok', 'criteria': []}])
def test_acceptance_needs_criterion_coverage(task_context, invalid):
    conn, task, _, _ = task_context
    with pytest.raises(d.WorkflowError, match='assessment|every spec'):
        d.resolve_decision(conn, ask(conn, task, 'spec_review'), action='continue', answer='ok', author='Principal', assessment=invalid)
    assert not review.accepted(conn, task.id, 'spec_review')


def test_new_spec_revokes_acceptance_and_preserves_history(task_context):
    conn, task, spec, _ = task_context
    old = accept(conn, task, 'spec_review')
    d.save_spec(conn, task.id, task.current_run_id, dict(spec, goal='Verify updated count'), author='Claude TL', evidence={'session': 'new-tl'})
    assert not review.accepted(conn, task.id, 'spec_review')
    assert d.get_decision(conn, old)['status'] == 'resolved'
    assert len(d.pending_decisions(conn)) == 1


def test_final_review_is_mandatory_and_report_replacement_revokes_it(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    publication = ask(conn, task, 'review')
    d.resolve_decision(conn, publication, action='approve', answer='Report approved', author='Principal')
    assert not d.completion_ready(conn, task.id)
    final = accept(conn, task, 'final_review', artifact)
    assert d.completion_ready(conn, task.id)
    save_report(conn, task, artifact)
    assert not d.completion_ready(conn, task.id)
    assert d.get_decision(conn, final)['status'] == 'resolved'


def test_rework_on_same_card_then_real_completion(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    decision = ask(conn, task, 'final_review')
    d.resolve_decision(conn, decision, action='changes', answer='Add proof detail', author='Principal')
    assert not d.completion_ready(conn, task.id)
    artifact.write_text('count=31\nSource fixture rechecked.\n')
    save_report(conn, task, artifact)
    publication = ask(conn, task, 'review')
    d.resolve_decision(conn, publication, action='approve', answer='Report approved', author='Principal')
    accept(conn, task, 'final_review', artifact)
    assert kb.complete_task(conn, task.id, result='Verified fixture')
    assert kb.get_task(conn, task.id).status == 'done'
    assert conn.execute('select count(*) from tasks').fetchone()[0] == 1


def test_changed_evidence_is_rejected_before_and_after_final_acceptance(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    decision = ask(conn, task, 'final_review')
    artifact.write_text('count=99\n')
    with pytest.raises(d.WorkflowError, match='evidence changed'):
        d.resolve_decision(conn, decision, action='continue', answer='ok', author='Principal', assessment=assessment(artifact))
    save_report(conn, task, artifact)
    accept(conn, task, 'final_review', artifact)
    publication = ask(conn, task, 'review')
    d.resolve_decision(conn, publication, action='approve', answer='Report approved', author='Principal')
    artifact.write_text('changed again')
    assert not d.completion_ready(conn, task.id)


@pytest.mark.parametrize('status', ['FAIL', 'NOT_RUN'])
def test_partial_evidence_cannot_receive_final_acceptance(task_context, status):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact, status)
    with pytest.raises(d.WorkflowError, match='Unproven'):
        accept(conn, task, 'final_review', artifact)


def test_final_review_rejects_changed_candidate_and_wrong_evidence(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    decision = ask(conn, task, 'final_review')
    with pytest.raises(d.WorkflowError, match='inspected local'):
        d.resolve_decision(conn, decision, action='continue', answer='ok', author='Principal', assessment=assessment(artifact.parent / 'other.txt'))
    d.advance(conn, task.id, task.current_run_id, 'report', next_action='review', state={'candidate_sha': 'different'})
    with pytest.raises(d.WorkflowError, match='changed'):
        d.resolve_decision(conn, decision, action='continue', answer='ok', author='Principal', assessment=assessment(artifact))


def test_config_applies_to_retained_tasks_without_changing_card_overrides(task_context):
    conn, task, _, _ = task_context
    assert task.model_override is None
    assert review.required(conn, task.id)
    assert review.worker_model_args(task) == ['-m', 'gpt-5.6-luna', '--provider', 'openai-codex', '--reasoning', 'high']


def test_worker_spawn_uses_role_policy(task_context, monkeypatch):
    conn, task, _, _ = task_context
    # Model a retained spec predating the new policy: no acceptance request yet.
    conn.execute("DELETE FROM nfos_decisions WHERE task_id=?",(task.id,));conn.commit()
    captured = []
    monkeypatch.setattr(kb, '_retag_legacy_worker_sessions', lambda path: None)
    monkeypatch.setattr(kb, '_resolve_worker_cli_toolsets', lambda path: [])
    monkeypatch.setattr(kb, '_worker_resume_context', lambda *a, **kw: (None, ''))
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: captured.append(cmd) or SimpleNamespace(pid=123))
    kb._default_spawn(task, str(Path(os.environ['HERMES_HOME'])))
    cmd = captured[0]
    assert cmd[cmd.index('-m', 3) + 1] == 'gpt-5.6-luna'
    assert cmd[cmd.index('--reasoning') + 1] == 'high'
    assert len(d.pending_decisions(conn)) == 1
    assert d.pending_decisions(conn)[0]['kind'] == 'spec_review'


def test_decision_cli_persists_principal_assessment(task_context, tmp_path):
    conn, task, _, _ = task_context
    decision = ask(conn, task, 'spec_review')
    payload = tmp_path / 'answer.json'
    payload.write_text(json.dumps({'answer': 'Reviewed', 'assessment': assessment()}))
    result = subprocess.run([sys.executable, str(Path(d.__file__)), 'decide', '--decision', decision,
                             '--resolution', 'continue', '--input', str(payload)], capture_output=True, text=True,
                            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert result.returncode == 0, result.stderr
    assert review.accepted(conn, task.id, 'spec_review')


@pytest.mark.parametrize('task_context', ['code'], indirect=True)
def test_code_publication_approval_does_not_replace_final_review(task_context):
    conn, task, _, artifact = task_context
    sha, integrated, tree = 'a' * 40, 'b' * 40, 'c' * 40
    accept(conn, task, 'spec_review')
    assert d.acquire_project(conn, 'fixture', task.id, task.current_run_id, sha)
    d.advance(conn, task.id, task.current_run_id, 'homolog', next_action='review',
              state={'homolog_sha': sha, 'candidate_tree': tree, 'homolog_evidence': [str(artifact)]})
    pr = d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='fixture:pr', candidate=sha)
    d.reconcile_effect(conn, pr['id'], found=True, evidence={'readback': 'Synthetic PR', 'candidate': sha, 'url': 'https://example.test/pr'})
    approval = ask(conn, task, 'review')
    d.resolve_decision(conn, approval, action='approve', answer='Fixture publication reviewed', author='Principal')
    merge = d.begin_effect(conn, task.id, task.current_run_id, operation='merge', target='fixture:pr', candidate=sha)
    d.reconcile_effect(conn, merge['id'], found=True, evidence={'readback': 'Synthetic merge', 'candidate': sha, 'integrated_sha': integrated, 'tree': tree})
    deploy = d.begin_effect(conn, task.id, task.current_run_id, operation='deploy', target='fixture:prod', candidate=integrated)
    d.reconcile_effect(conn, deploy['id'], found=True, evidence={'readback': 'Synthetic deployment', 'candidate': integrated, 'tree': tree, 'artifact': 'fixture-image', 'behavior_evidence': [str(artifact)]})
    save_report(conn, task, artifact)
    assert not d.completion_ready(conn, task.id)
    accept(conn, task, 'final_review', artifact)
    assert d.completion_ready(conn, task.id)


def test_instruction_change_invalidates_spec_acceptance(task_context):
    conn, task, _, _ = task_context
    accept(conn, task, 'spec_review')
    kb.update_task_instruction(conn, task.id, body='Verify another count instead', author='Principal', expected_revision=0)
    assert not review.accepted(conn, task.id, 'spec_review')


def test_native_command_does_not_start_before_spec_acceptance(task_context):
    from hermes_cli.nfos_tool import run_command
    conn, task, _, artifact = task_context
    argv=[sys.executable, '-c', 'print("native-proof")']
    kwargs=dict(task_id=task.id,run_id=task.current_run_id,argv=argv,cwd=artifact.parent,timeout_seconds=10)
    with pytest.raises(d.WorkflowError, match='spec acceptance'):
        run_command(os.environ['HERMES_KANBAN_DB'],**kwargs)
    assert conn.execute('SELECT count(*) FROM nfos_tool_calls').fetchone()[0] == 0
    accept(conn,task,'spec_review')
    result=run_command(os.environ['HERMES_KANBAN_DB'],**kwargs)
    assert result['status']=='succeeded'
