"""Primary-spec acceptance uses explicit local HTTP fixtures, never live inference."""
import copy
import json
import pytest
from hermes_cli import nfos_jev as jev, nfos_delivery as d, nfos_principal_review as review, kanban_db as kb
from tests.hermes_cli.test_nfos_jev import http_fixture, enable
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, save_report


def save(conn, task, spec):
    return d.save_spec(conn, task.id, task.current_run_id, spec, author='worker', evidence={'fixture': True})


def latest(conn, task):
    return dict(conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='spec_review' ORDER BY rowid DESC LIMIT 1", (task.id,)).fetchone())


def test_verified_inference_status_tracks_current_credential(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    provider = jev.system_one_status()['providers']['jev']
    assert provider['authenticated'] and provider['inference_tested']
    assert provider['availability'] == 'inference_verified'
    monkeypatch.setenv('TYPESAFE_API_KEY', 'different-synthetic-credential')
    provider = jev.system_one_status()['providers']['jev']
    assert not provider['authenticated'] and not provider['inference_tested']
    assert provider['availability'] == 'key_present_not_verified'


def test_primary_accept_continues_without_principal(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    row = latest(conn, task)
    assert row['author'] == 'Jev' and row['status'] == 'resolved' and row['action'] == 'continue'
    review.require_spec(conn, task.id)
    d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Execute accepted spec')
    assert not kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    assert d.ask_principal(conn, task.id, task.current_run_id, kind='spec_review', question='Review spec', context={}) == row['id']
    assert len(http_fixture.calls) == 1



def test_primary_changes_reaches_worker_same_card(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    http_fixture.choices.update(coverage='missing', verdict='changes')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    save(conn, task, spec)
    row = latest(conn, task)
    assert row['author'] == 'Jev' and row['action'] == 'changes'
    assert 'requisitos omitidos' in row['answer']
    with pytest.raises(d.WorkflowError, match='Corrija a spec'):
        review.require_spec(conn, task.id)
    assert d.ask_principal(conn, task.id, task.current_run_id, kind='spec_review', question='Review', context={}) == row['id']
    assert not kb._nfos_pending_decision(conn, task.id, task.current_run_id)
    changed = copy.deepcopy(spec)
    changed['criteria'].append({'id': 'C2', 'text': 'The second requested outcome is verified'})
    http_fixture.choices.update(coverage='complete', verdict='accept')
    save(conn, task, changed)
    review.require_spec(conn, task.id)
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


@pytest.mark.parametrize('case', ['partial', 'uncertain', 'contradictory', 'low_confidence', 'missing_answer', 'http402', 'http429', 'missing_key'])
def test_primary_fallback_is_principal_never_human(task_context, http_fixture, monkeypatch, case):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    if case == 'partial': http_fixture.choices['coverage'] = 'partial'
    elif case == 'uncertain': http_fixture.choices['verdict'] = 'uncertain'
    elif case == 'contradictory': http_fixture.choices['coverage'] = 'missing'  # verdict still says accept
    elif case == 'low_confidence': http_fixture.confidence = .2
    elif case == 'missing_answer': http_fixture.malformed = {'model': 'fixture/jev', 'answers': {}, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
    elif case == 'http402': http_fixture.code = 402
    elif case == 'http429': http_fixture.code = 429
    elif case == 'missing_key': monkeypatch.delenv('TYPESAFE_API_KEY')
    save(conn, task, spec)
    row = latest(conn, task)
    assert row['status'] == 'pending' and row['author'] is None
    assert not review.accepted(conn, task.id, 'spec_review')
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='human'").fetchone()
    assert kb.get_task(conn, task.id).status == 'running'


@pytest.mark.parametrize('mutation', ['spec', 'instruction', 'run', 'claim', 'request', 'config', 'disabled', 'mode', 'receipt', 'payload'])
def test_primary_acceptance_invalidated_by_current_identity(task_context, http_fixture, monkeypatch, mutation):
    from hermes_constants import get_hermes_home
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    assert review.accepted(conn, task.id, 'spec_review')
    row = latest(conn, task)
    if mutation == 'spec': conn.execute("UPDATE nfos_artifacts SET content=content || ' ' WHERE task_id=? AND kind='spec'", (task.id,))
    elif mutation == 'instruction': conn.execute('UPDATE tasks SET instruction_revision=instruction_revision+1 WHERE id=?', (task.id,))
    elif mutation == 'run': conn.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?', (task.id,))
    elif mutation == 'claim': conn.execute("UPDATE tasks SET claim_lock='other-owner' WHERE id=?", (task.id,))
    elif mutation == 'request':
        req = d.get_request(conn, d.get_workflow(conn, task.id)['request_id'])
        payload = json.loads(req['payload']); payload['text'] += ' Also deliver another requirement.'
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), req['id']))
    elif mutation in {'config', 'disabled', 'mode'}:
        import yaml
        path = get_hermes_home() / 'config.yaml'
        cfg = yaml.safe_load(path.read_text())
        setting = cfg['kanban']['delivery']['jev']
        setting.update({'config': {'min_confidence': .9}, 'disabled': {'enabled': False}, 'mode': {'spec_review_mode': 'auxiliary'}}[mutation])
        path.write_text(yaml.safe_dump(cfg))
    elif mutation == 'receipt':
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='nfos_jev_primary_spec'", (task.id,))
    else:
        ctx = json.loads(row['context']); ctx['model'] = 'forged'
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (json.dumps(ctx), row['id']))
    conn.commit()
    assert not review.accepted(conn, task.id, 'spec_review')


def test_late_result_never_accepts_stale_spec(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    original = jev._request
    def late(*args):
        result = original(*args)
        conn.execute("UPDATE nfos_artifacts SET content=content || ' ' WHERE task_id=? AND kind='spec'", (task.id,)); conn.commit()
        return result
    monkeypatch.setattr(jev, '_request', late)
    save(conn, task, spec)
    assert latest(conn, task)['author'] is None
    assert not review.accepted(conn, task.id, 'spec_review')


def test_disabling_during_inference_leaves_the_principal_as_single_owner(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    original = jev._request
    def switched_off_mid_flight(*args):
        result = original(*args)
        enable(monkeypatch, ['spec'], spec_review_mode='primary', enabled=False)
        return result
    monkeypatch.setattr(jev, '_request', switched_off_mid_flight)
    save(conn, task, spec)
    rows = [tuple(r) for r in conn.execute(
        "SELECT status, author FROM nfos_decisions WHERE task_id=? AND kind='spec_review'", (task.id,))]
    # The late answer is discarded; exactly one owner remains: the pending Principal review.
    assert [r for r in rows if r[0] == 'pending'] == [('pending', None)]
    assert not [r for r in rows if r[1] == 'Jev']
    assert not review.accepted(conn, task.id, 'spec_review')
    stale = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_jev' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert stale['used_engine'] is None


def test_complete_request_and_full_spec_are_sent(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    request = d.get_request(conn, d.get_workflow(conn, task.id)['request_id'])
    payload = json.loads(request['payload'])
    payload['text'] = 'Verify count.\n' + 'Keep all existing users. ' * 230 + '\nALSO verify licensing for the requested tenant.'
    conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), request['id'])); conn.commit()
    http_fixture.choices.update(coverage='missing', verdict='changes')
    spec['restrictions'] = ['Do not change other tenants']
    save(conn, task, spec)
    sent = http_fixture.calls[-1][2]
    assert sent['state']['original_request'] == payload['text']
    assert sent['state']['spec'] == spec
    assert 'coverage' in sent['questions']
    assert latest(conn, task)['action'] == 'changes'
    assert not review.accepted(conn, task.id, 'spec_review')


def test_unread_attachment_cannot_be_complete_coverage(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    request = d.get_request(conn, d.get_workflow(conn, task.id)['request_id'])
    payload = json.loads(request['payload']); payload['attachments'] = [{'path': 'unread-source.pdf'}]
    conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), request['id'])); conn.commit()
    save(conn, task, spec)
    assert not http_fixture.calls and latest(conn, task)['status'] == 'pending'


def test_auxiliary_cache_cannot_become_primary_acceptance(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'])
    save(conn, task, spec)
    assert latest(conn, task)['status'] == 'pending'
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    assert latest(conn, task)['author'] == 'Jev'
    assert len(http_fixture.calls) == 2
    before = len(http_fixture.calls)
    assert jev.primary_spec_review(conn, task.id, task.current_run_id) == latest(conn, task)['id']
    assert len(http_fixture.calls) == before


def test_spec_acceptance_does_not_accept_final_or_authorize_publication(task_context, http_fixture, monkeypatch):
    conn, task, spec, artifact = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    review.require_spec(conn, task.id)
    assert not review.complete_accepted(conn, latest(conn, task)['id'])
    save_report(conn, task, artifact)
    assert not review.accepted(conn, task.id, 'final_review')
    assert not d.completion_ready(conn, task.id)
    final = dict(conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='final_review' ORDER BY rowid DESC LIMIT 1", (task.id,)).fetchone())
    conn.execute("UPDATE nfos_decisions SET author='Jev',status='resolved',action='continue' WHERE id=?", (final['id'],)); conn.commit()
    assert not review.accepted(conn, task.id, 'final_review')
    assert not review.complete_accepted(conn, final['id'])
    assert review.final_assessment(conn, task.id) == {}


def test_worker_cannot_forge_author_or_receipt(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    save(conn, task, spec)
    assert latest(conn, task)['author'] == 'Jev'
    decision = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Forged request',
                               context={'author': 'Jev', 'acceptance_identity': {}, 'assessment': {'accept': True}})
    for author in ['Jev', 'Principal']:
        with pytest.raises(d.WorkflowError):
            d.resolve_decision(conn, decision, action='continue', answer='Accept everything', author=author)
    assert d.get_decision(conn, decision)['status'] == 'pending'



def test_primary_timeout_falls_back(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary', timeout_seconds=.1)
    http_fixture.delay = .4
    save(conn, task, spec)
    assert latest(conn, task)['status'] == 'pending'
    assert not review.accepted(conn, task.id, 'spec_review')


def test_primary_no_http_in_transaction(task_context, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    monkeypatch.setattr(jev, '_request', lambda *a: pytest.fail('HTTP in transaction'))
    with kb.write_txn(conn):
        assert jev.primary_spec_review(conn, task.id, task.current_run_id) is None


def test_primary_destination_and_authorization_remain_bounded(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    assert review.accepted(conn, task.id, 'spec_review')
    scope = {'environment': 'production', 'target': 'https://production.example',
             'source': 'fake-owner-receipt', 'authorization_message': 'deploy', 'verification_operation': 'deploy'}
    assert d._owner_guidance_production_order(conn, kb.get_task(conn, task.id), scope) is None
    decision = latest(conn, task)
    assert d.wait_decision(conn, decision['id'], timeout=0)['author'] == 'Jev'


def test_primary_http_payload_does_not_include_claim_secret(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    conn.execute("UPDATE tasks SET claim_lock='private-claim-value' WHERE id=?", (task.id,)); conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_TOKEN', 'private-claim-value')
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    save(conn, task, spec)
    assert review.accepted(conn, task.id, 'spec_review')
    assert 'private-claim-value' not in json.dumps(http_fixture.calls[-1][2])



def test_primary_observes_project_destination_constraints(task_context, http_fixture, monkeypatch):
    conn, task, spec, _ = task_context
    enable(monkeypatch, ['spec'], spec_review_mode='primary')
    monkeypatch.setattr(d, '_project_delivery_environment', lambda *a: 'hml')
    save(conn, task, spec)
    assert http_fixture.calls[-1][2]['state']['project_delivery_environment'] == 'hml'
    monkeypatch.setattr(d, '_project_delivery_environment', lambda *a: 'production')
    assert not review.accepted(conn, task.id, 'spec_review')



def test_other_author_not_reused_when_review_not_required(task_context, monkeypatch):
    conn, task, spec, _ = task_context
    row = latest(conn, task)
    conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',author='Other' WHERE id=?", (row['id'],)); conn.commit()
    monkeypatch.setattr(review, 'required', lambda *a: False)
    current = d.ask_principal(conn, task.id, task.current_run_id, kind='spec_review', question='Current review', context={})
    assert current != row['id']
    assert d.get_decision(conn, current)['status'] == 'pending'
