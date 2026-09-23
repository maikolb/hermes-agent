import copy
import json

from hermes_cli import nfos_system_one_dataset as dataset
from tests.hermes_cli.test_nfos_jev import http_fixture  # noqa: F401 -- pytest fixture
from tests.hermes_cli.test_nfos_principal_acceptance import task_context  # noqa: F401 -- pytest fixture


def case(project='one', at=10, lineage=None, task='t1'):
    prefix = f'{project}/{task}/task_events/'
    opportunity = {'opportunity_id': 'o1', 'project_id': project, 'task_id': task, 'run_id': 1,
                   'lineage_id': lineage or f'{project}/{task}',
                   'input': {'objective': 'Leia somente. Não apague. contato owner@example.com token=private123',
                             'stage': 'impediment', 'budget_remaining': 8,
                             'recent_result': 'read failed', 'options': [{'id': 'read'}, {'id': 'fallback'}],
                             'constraints': {'target': 'synthetic-target', 'access': 'read-only'},
                             'evidence_refs': ['prior-read']},
                   'versions': {'policy': 'p1', 'model': 'multilingual', 'tokenizer': 'r1',
                                'head': 'h1', 'schema': '1', 'action_catalog': 'native-probes-v1'}}
    outcome = {'opportunity_id': 'o1', 'run_id': 1, 'decision': 'read', 'selected_engine': 'laya',
               'actual_engine': 'laya', 'verified_outcome': 'PASS'}
    def event(i, kind, body, timestamp):
        return {'ref': prefix + str(i), 'data': {'id': i, 'task_id': task, 'run_id': 1,
                'kind': kind, 'created_at': timestamp, 'payload': json.dumps(body)}}
    return {'project': project, 'task_id': task, 'title': 'Synthetic only', 'records': [
        event(1, 'nfos_system_one_opportunity', opportunity, at),
        event(2, 'nfos_system_one_outcome', outcome, at + 1)]}


def update_payload(c, index, **values):
    record = c['records'][index]['data']
    record['payload'] = json.dumps({**json.loads(record['payload']), **values})


def build(cases, reviews=None):
    return dataset.build_dataset(cases, reviews, train_before=20, test_after=30)


def test_snapshot_redaction_no_outcome_leak_and_no_reviewer_success():
    c = case()
    ref = c['records'][0]['ref']
    review = {'inspected_sources': [ref], 'summary': 'Approved', 'system_one_labels': [
        {'source': ref, 'preferred_option': 'read', 'reason': 'Preserves read-only intent'}]}
    result = build([c], {'one/t1': review})
    row = result['examples'][0]
    assert 'Não apague' in row['inputs']['objective']
    assert row['inputs']['constraints']['access'] == 'read-only'
    assert 'private123' not in dataset.canonical(result) and 'owner@example.com' not in dataset.canonical(result)
    assert row['labels']['reviewer_preference']['option'] == 'read'
    assert row['labels']['policy_validity'] == 'VALID'
    assert row['labels']['verified_outcome'] == 'NOT_PROVEN'
    assert not dataset.has_future_field(row['inputs'])
    assert row['provenance']['opportunity_payload_sha256'] == dataset.fingerprint(c['records'][0]['data'])


def test_invalid_typed_choice_and_unread_reviewer_label():
    c = case()
    update_payload(c, 1, decision='delete')
    reviews = {'one/t1': {'inspected_sources': [], 'system_one_labels': [
        {'source': c['records'][0]['ref'], 'preferred_option': 'read'}]}}
    result = build([c], reviews)
    row = result['examples'][0]
    assert row['labels']['policy_validity'] == 'INVALID'
    assert row['labels']['reviewer_preference'] is None
    assert dataset.replay(result)['examples'][0]['route'] == 'fallback'


def test_only_matching_later_native_verification_can_label_pass_or_fail():
    for status in ['PASS', 'FAIL']:
        c = case()
        ref = 'one/t1/task_events/3'
        verification = {'opportunity_id': 'o1', 'run_id': 1, 'target': 'synthetic-target',
                        'status': status, 'authority': 'native_verifier'}
        c['records'].append({'ref': ref, 'data': {'kind': 'nfos_verification', 'created_at': 12,
                                               'payload': json.dumps(verification)}})
        update_payload(c, 1, verification={'status': status, 'target': 'synthetic-target', 'source_refs': [ref]})
        assert build([c])['examples'][0]['labels']['verified_outcome'] == status
        bad = copy.deepcopy(c)
        update_payload(bad, 2, target='other-target')
        assert build([bad])['examples'][0]['labels']['verified_outcome'] == 'NOT_PROVEN'
        bad = copy.deepcopy(c)
        bad['records'][2]['data']['created_at'] = 9
        assert build([bad])['examples'][0]['labels']['verified_outcome'] == 'NOT_PROVEN'
        bad = copy.deepcopy(c)
        update_payload(bad, 2, run_id=2)
        assert build([bad])['examples'][0]['labels']['verified_outcome'] == 'NOT_PROVEN'


def test_project_lineage_and_time_splits_are_disjoint_or_quarantined():
    cases = [case('train', 10), case('test', 40), case('mixed', 10, task='a'), case('mixed', 40, task='b'),
             case('linked-a', 10, lineage='same-request'), case('linked-b', 40, lineage='same-request')]
    result = build(cases)
    rows = result['examples']
    assert {r['split'] for r in rows} == {'train', 'test', 'quarantine'}
    assert sum(r['split'] == 'quarantine' for r in rows) == 4
    for field in ['project', 'task', 'lineage']:
        assert {r[field] for r in rows if r['split'] == 'train'}.isdisjoint(
            {r[field] for r in rows if r['split'] == 'test'})
    assert max(r['decision_at'] for r in rows if r['split'] == 'train') < min(
        r['decision_at'] for r in rows if r['split'] == 'test')


def test_repeated_snapshots_dedupe_and_conflicting_event_is_quarantined():
    c = case()
    earlier = copy.deepcopy(c)
    earlier['records'].pop()
    assert build([c, c, earlier]) == build([earlier, c]) == build([c])
    contradictory = copy.deepcopy(c)
    update_payload(contradictory, 1, decision='delete')
    result = build([c, contradictory])
    assert len(result['examples']) == 1
    assert result['examples'][0]['split'] == 'quarantine'
    assert 'conflicting_source_snapshots' in result['examples'][0]['quarantine_reasons']


def test_future_input_is_removed_and_legacy_success_is_not_ground_truth():
    contaminated = case()
    body = json.loads(contaminated['records'][0]['data']['payload'])
    body['input']['recent_result'] = {'verified_outcome': 'PASS'}
    update_payload(contaminated, 0, **body)
    row = build([contaminated])['examples'][0]
    assert row['inputs'] == {} and row['split'] == 'quarantine'
    legacy = case()
    legacy['records'] = [{'ref': 'one/t1/task_events/9', 'data': {'kind': 'nfos_jev_action',
                         'created_at': 10, 'payload': json.dumps({'typed_action_executed': True,
                          'principal_calls_saved': 1, 'results': [{'state': 'PASS'}]})}}]
    row = build([legacy])['examples'][0]
    assert row['provenance']['legacy'] is True
    assert row['labels']['verified_outcome'] == 'NOT_PROVEN'
    assert row['inputs'] == {} and row['split'] == 'quarantine'
    # A legacy action with no decision snapshot stays excluded without poisoning
    # the valid, source-bound opportunity from the same card/project.
    mixed = case()
    mixed['records'].extend(legacy['records'])
    rows = build([mixed])['examples']
    assert len(rows) == 2
    assert next(r for r in rows if not r['provenance']['legacy'])['split'] == 'train'
    assert next(r for r in rows if r['provenance']['legacy'])['split'] == 'quarantine'


def test_cli_export_and_replay_are_deterministic_offline(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'fixture.json'
    output = tmp_path / 'dataset.json'
    report = tmp_path / 'replay.json'
    source.write_text(json.dumps({'cases': [case()], 'reviews': {}}), encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['dataset', 'export', '--input', str(source), '--output', str(output),
                                  '--train-before', '20', '--test-after', '30'])
    dataset.main()
    first = output.read_bytes()
    dataset.main()
    assert output.read_bytes() == first
    monkeypatch.setattr('sys.argv', ['dataset', 'replay', '--input', str(output), '--output', str(report)])
    dataset.main()
    data = json.loads(report.read_text())
    assert data['offline'] is True and data['inference_calls'] == data['training_calls'] == 0
    assert data['examples'][0]['route'] == 'recorded_choice'
    assert 'inference_calls' in capsys.readouterr().out


def test_actual_native_question_and_probe_artifact_format():
    c = case()
    source = json.loads(c['records'][0]['data']['payload'])
    source['input'] = {'state': {'goal': 'Verify native HTTP', 'mutation': {'revision': 4}},
                       'options': {'next': {'type': 'choice', 'criteria': {'probe_0': 'Read endpoint', 'fallback': 'Abstain'}}},
                       'evidence_refs': ['request:r1', 'spec:2']}
    update_payload(c, 0, **source)
    update_payload(c, 1, decision={'next': {'type': 'choice', 'choice': 'probe_0', 'confidence': .99}},
                   outcome={'status': 'NOT_PROVEN', 'evidence_refs': [], 'verified_at': None})
    probe = {'kind': 'http', 'url': 'https://synthetic.invalid/health', 'expect': {'status': 200}}
    spec = {'criteria': [{'id': 'c1', 'probe': probe}]}
    measured = {'criterion': 'c1', 'probe': probe, 'state': 'PASS', 'spec_revision': 2,
                'run_id': 1, 'mutation': {'revision': 4}, 'ran_at': 12}
    for ident, kind, revision, content in [(3, 'spec', 2, spec), (4, 'probe:c1', 1, measured)]:
        c['records'].append({'ref': f'one/t1/nfos_artifacts/{ident}', 'data': {'id': ident,
            'task_id': 't1', 'run_id': 1, 'kind': kind, 'revision': revision,
            'content': json.dumps(content), 'author': 'runtime', 'created_at': 12}})
    c['records'].append({'ref': 'one/t1/task_events/5', 'data': {'kind': 'nfos_system_one_outcome',
        'run_id': 1, 'created_at': 13, 'payload': json.dumps({'opportunity_id': 'o1', 'policy_validity': 'VALID',
            'actual_engine': 'laya', 'outcome': {'status': 'PASS',
            'evidence_refs': ['artifact:probe:c1:1'], 'verified_at': 13}})}})
    result = build([c])
    row = result['examples'][0]
    assert row['inputs']['state']['goal'] == 'Verify native HTTP'
    assert row['decision']['choice'] == 'probe_0'
    assert row['labels']['policy_validity'] == 'VALID'
    assert row['labels']['verified_outcome'] == 'PASS'
    assert dataset.replay(result)['examples'][0]['route'] == 'recorded_choice'
    # An outcome claim whose actual verifier targets another endpoint is not ground truth.
    wrong = copy.deepcopy(c)
    bad_measurement = {**measured, 'probe': {**probe, 'url': 'https://other.invalid/health'}}
    wrong['records'][3]['data']['content'] = json.dumps(bad_measurement)
    assert build([wrong])['examples'][0]['labels']['verified_outcome'] == 'NOT_PROVEN'


def test_legacy_secret_shapes_redacted_without_erasing_tokenizer_revision():
    value = {'token': 'synthetic-token', 'refresh_token': 'synthetic-refresh',
             'Authorization': 'Basic c3ludGhldGljOm9ubHk=', 'tokenizer': 'revision-052592',
             'state': '-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC-NOT-A-KEY\n-----END OPENSSH PRIVATE KEY-----',
             'header_text': 'Authorization: Basic c3ludGhldGljOm9ubHk='}
    cleaned = dataset.redact(value)
    assert cleaned['tokenizer'] == 'revision-052592'
    text = dataset.canonical(cleaned)
    assert all(secret not in text for secret in ['synthetic-token', 'synthetic-refresh', 'c3ludGhldGljOm9ubHk', 'SYNTHETIC-NOT-A-KEY'])


def test_learning_consumes_actual_native_opportunity(task_context, http_fixture, monkeypatch):
    # Producer: the native report path collecting a registered, still unmeasured probe.
    from hermes_cli import nfos_reviewer, nfos_jev
    from tests.hermes_cli.test_nfos_jev import prepare_probe
    from tests.hermes_cli.test_nfos_laya import select_laya
    from tests.hermes_cli.test_nfos_principal_acceptance import save_report
    conn, task, spec, artifact = task_context
    prepare_probe(conn, task, spec, http_fixture, monkeypatch)
    nfos_jev.configure_system_one({'mode': 'active', 'engine': 'laya', 'classes': ['evidence'],
                                   'timeout_seconds': 1, 'max_decisions_per_run': 4})
    select_laya(conn, task, http_fixture, ['evidence'])
    save_report(conn, task, artifact)
    assert http_fixture.probes == ['/count']
    db = conn.execute('PRAGMA database_list').fetchone()[2]
    opportunity = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_system_one_opportunity'").fetchone()[0])
    cases = nfos_reviewer.collect(db, opportunity['project_id'], 0, 9999999999)
    result = dataset.build_dataset(cases, train_before=9999999999, test_after=10000000000)
    rows = [r for r in result['examples'] if not r['provenance']['legacy']]
    assert rows and rows[0]['inputs']['state']['reason'] == 'Collect missing evidence for the current report'
    assert rows[0]['decision']['actual_engine'] == 'laya'
    assert rows[0]['labels']['policy_validity'] == 'VALID'
    assert rows[0]['labels']['verified_outcome'] == 'PASS'
    assert rows[0]['provenance']['verification_sources']
    assert dataset.replay(result)['inference_calls'] == 0
