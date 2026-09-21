"""Deterministic retrospective export/replay. No inference, training or production writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'nfos-system-one-learning-v1'
INPUT_KEYS = {'state', 'objective', 'stage', 'budget_remaining', 'recent_result', 'options',
              'constraints', 'evidence_refs'}
FORBIDDEN_INPUT_KEYS = {'outcome', 'verified_outcome', 'reviewer_label', 'preferred_option',
                        'labels', 'verification', 'future_result', 'executed_action'}
SECRET_KEYS = re.compile(r'(?i)(password|senha|secret|api[_-]?key|(?:^|[_-])token(?:$|[_-])|authorization|credential|cookie)')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact(value):
    """Keep meaningful context; redact credentials/PII, never truncate input."""
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET_KEYS.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----',
                       '[PRIVATE KEY REDACTED]', value, flags=re.S)
        value = re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', value)
        value = re.sub(r'(?i)\b(?:(?:Bearer|Basic)\s+|sk-)[A-Za-z0-9_./=+~-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s&,;]+', r'\1[REDACTED]', value)
        value = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', value)
        value = re.sub(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b', '[CPF]', value)
    return value


def payload(record):
    value = record['data'].get('payload', {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def has_future_field(value):
    if isinstance(value, dict):
        return bool(FORBIDDEN_INPUT_KEYS.intersection(value)) or any(has_future_field(v) for v in value.values())
    return isinstance(value, list) and any(has_future_field(v) for v in value)


def option_ids(options):
    if isinstance(options, dict):
        return {str(key) for question in options.values() if isinstance(question, dict)
                for key in question.get('criteria', {})}
    if not isinstance(options, list):
        return set()
    return {str(x.get('id', x.get('value', x.get('key', '')))) if isinstance(x, dict) else str(x)
            for x in options} - {''}


def chosen_option(value):
    decision = value.get('decision', value.get('selected_option', value.get('action')))
    if isinstance(decision, dict):
        direct = decision.get('option', decision.get('selected_option', decision.get('value')))
        choices = [a.get('choice') for a in decision.values() if isinstance(a, dict) and 'choice' in a]
        decision = direct if direct is not None else choices[0] if len(choices) == 1 else None
    return str(decision) if isinstance(decision, (str, int)) else None


def typed_validity(outcome, options):
    response = outcome.get('decision')
    if isinstance(options, dict) and isinstance(response, dict):
        questions = {k: q for k, q in options.items() if isinstance(q, dict) and q.get('type') == 'choice'}
        if not questions:
            return 'NOT_PROVEN'
        return 'PASS' if all(isinstance(response.get(k), dict) and
            response[k].get('choice') in q.get('criteria', {}) for k, q in questions.items()) else 'FAIL'
    choice, allowed = chosen_option(outcome), option_ids(options)
    return ('PASS' if choice in allowed else 'FAIL') if choice is not None and allowed else 'NOT_PROVEN'


def native_probe_outcome(outcome, records, inputs, run_id, at):
    evidence = outcome.get('outcome', {})
    if not isinstance(evidence, dict) or evidence.get('status') not in {'PASS', 'FAIL'}:
        return 'NOT_PROVEN'
    refs = evidence.get('evidence_refs') or []
    if not refs or float(evidence.get('verified_at') or 0) < at:
        return 'NOT_PROVEN'
    spec_refs = [str(ref)[5:] for ref in inputs.get('evidence_refs', []) if str(ref).startswith('spec:')]
    if len(spec_refs) != 1:
        return 'NOT_PROVEN'
    state = inputs.get('state', {})
    artifacts = [r['data'] for r in records.values() if '/nfos_artifacts/' in r['ref']]
    specs = [a for a in artifacts if a.get('kind') == 'spec' and str(a.get('revision')) == spec_refs[0]]
    if len(specs) != 1:
        return 'NOT_PROVEN'
    spec = json.loads(specs[0]['content'])
    states = []
    for ref in refs:
        if not str(ref).startswith('artifact:probe:'):
            return 'NOT_PROVEN'
        kind, revision = str(ref)[9:].rsplit(':', 1)
        matches = [a for a in artifacts if a.get('kind') == kind and str(a.get('revision')) == revision]
        if len(matches) != 1 or matches[0].get('author') != 'runtime' or matches[0].get('run_id') != run_id:
            return 'NOT_PROVEN'
        measured = json.loads(matches[0]['content'])
        criterion = next((c for c in spec.get('criteria', []) if c.get('id') == kind[6:]), {})
        expected_probe = {k: v for k, v in criterion.get('probe', {}).items() if k != 'headers'}
        if (not expected_probe or measured.get('probe') != expected_probe or measured.get('run_id') != run_id
                or str(measured.get('spec_revision')) != spec_refs[0] or float(measured.get('ran_at', 0)) < int(at)
                or ('mutation' in state and measured.get('mutation') != state['mutation'])):
            return 'NOT_PROVEN'
        states.append(measured.get('state'))
    actual = 'PASS' if all(s == 'PASS' for s in states) else 'FAIL' if 'FAIL' in states else 'NOT_PROVEN'
    return actual if actual == evidence['status'] else 'NOT_PROVEN'


def verified_outcome(outcome, source_records, opportunity_id, run_id, at, target):
    """Only a later, source-bound native verifier receipt can prove a target result."""
    verification = outcome.get('verification', {})
    if not isinstance(verification, dict):
        return 'NOT_PROVEN'
    refs = verification.get('source_refs', [])
    status = verification.get('status')
    if status not in {'PASS', 'FAIL'} or not refs or not target or verification.get('target') != target:
        return 'NOT_PROVEN'
    for ref in refs:
        record = source_records.get(ref)
        if not record or record['data'].get('kind') != 'nfos_verification':
            return 'NOT_PROVEN'
        evidence = payload(record)
        if (evidence.get('opportunity_id') != opportunity_id or evidence.get('run_id') != run_id
                or evidence.get('status') != status or evidence.get('authority') != 'native_verifier'
                or evidence.get('target') != target or float(record['data'].get('created_at', 0)) <= at):
            return 'NOT_PROVEN'
    return status


def build_dataset(cases, reviews=None, *, train_before=None, test_after=None):
    """Source snapshots become inputs; subsequent decisions/reviews stay separate labels."""
    reviews = reviews or {}
    examples = {}
    conflicts = set()
    merged, conflicting_events = {}, set()
    for case in sorted(cases, key=lambda c: (c['project'], c['task_id'], fingerprint(c))):
        key = (case['project'], case['task_id'])
        target = merged.setdefault(key, {'project': case['project'], 'task_id': case['task_id'], 'records': {}})
        for record in case['records']:
            previous = target['records'].get(record['ref'])
            if previous and previous != record and any(t in record['ref'] for t in ('/task_events/', '/nfos_artifacts/')):
                conflicting_events.add(key)
            target['records'][record['ref']] = record
    for key, merged_case in sorted(merged.items()):
        case = {**merged_case, 'records': list(merged_case['records'].values())}
        records = {r['ref']: r for r in case['records']}
        review = reviews.get(f'{case["project"]}/{case["task_id"]}', {})
        inspected = set(review.get('inspected_sources', []))
        events = [r for r in records.values() if '/task_events/' in r['ref']]
        opportunities = [r for r in events if r['data'].get('kind') == 'nfos_system_one_opportunity']
        known_opportunities = {payload(r).get('opportunity_id') for r in opportunities}
        # Legacy receipts remain candidates with explicitly unknown pre-decision context.
        opportunities += [r for r in events if r['data'].get('kind') in {'nfos_jev', 'nfos_jev_action'}
                          and not payload(r).get('opportunity_id') and payload(r).get('key') not in known_opportunities
                          and payload(r).get('reason') != 'started']
        for source in opportunities:
            data = payload(source)
            legacy = source['data'].get('kind') != 'nfos_system_one_opportunity'
            oid = str(data.get('opportunity_id') or source['ref'])
            project = str(data.get('project_id') or case['project'])
            lineage = str(data.get('lineage_id') or f'{project}/{case["task_id"]}')
            run_id = data.get('run_id', source['data'].get('run_id'))
            at = float(source['data'].get('created_at') or data.get('created_at') or 0)
            inputs = data.get('input', {}) if not legacy else {}
            reasons = []
            if key in conflicting_events:
                reasons.append('conflicting_source_snapshots')
            if (data.get('task_id', case['task_id']) != case['task_id'] or
                    data.get('project_id', case['project']) != case['project'] or
                    source['data'].get('run_id', run_id) != run_id):
                reasons.append('inconsistent_source_identity')
            versions = data.get('versions', {})
            if not isinstance(versions, dict) or not all(versions.get(k) for k in
                    ('policy', 'model', 'tokenizer', 'head', 'schema', 'action_catalog')):
                reasons.append('missing_versions')
            if not isinstance(inputs, dict) or not inputs:
                inputs = {}
                reasons.append('missing_decision_context')
            if has_future_field(inputs):
                reasons.append('future_field_in_input')
                inputs = {}  # never export contaminated training inputs, even quarantined
            inputs = {k: v for k, v in inputs.items() if k in INPUT_KEYS}
            if isinstance(inputs.get('state'), dict) and inputs['state'].get('omitted'):
                reasons.append('missing_decision_context')
            allowed = option_ids(inputs.get('options'))
            related = [r for r in events if r['data'].get('kind') in
                       {'nfos_system_one_outcome', 'nfos_jev', 'nfos_jev_action'}
                       and payload(r).get('opportunity_id') == oid]
            outcome_records = [r for r in related if float(r['data'].get('created_at') or 0) >= at
                               and payload(r).get('run_id', r['data'].get('run_id')) == run_id]
            outcome_records.sort(key=lambda r: (float(r['data'].get('created_at') or 0),
                int(r['data'].get('id') or 0), r['ref']))
            outcome = dict(data) if legacy else {}
            for outcome_record in outcome_records:
                outcome.update(payload(outcome_record))
            choice = chosen_option(outcome)
            policy = typed_validity(outcome, inputs.get('options'))
            preference = None
            for label in review.get('system_one_labels', []):
                if (label.get('source') == source['ref'] and source['ref'] in inspected
                        and str(label.get('preferred_option')) in allowed):
                    preference = {'option': str(label['preferred_option']), 'reason': redact(label.get('reason', '')),
                                  'source_ref_sha256': fingerprint(source['ref'])}
            constraints = inputs.get('constraints', {})
            target = constraints.get('target') if isinstance(constraints, dict) else None
            outcome_label = verified_outcome(outcome, records, oid, run_id, at, target)
            if outcome_label == 'NOT_PROVEN':
                try:
                    outcome_label = native_probe_outcome(outcome, records, inputs, run_id, at)
                except (ValueError, TypeError, AttributeError):
                    reasons.append('malformed_verification_source')
            identity = fingerprint({'project': project, 'task': case['task_id'], 'run': run_id, 'opportunity': oid})
            row = {'id': identity, 'schema': SCHEMA,
                   'project': fingerprint(project), 'task': fingerprint([project, case['task_id']]),
                   'lineage': fingerprint(lineage), 'run': fingerprint([project, case['task_id'], run_id]),
                   'decision_at': at, 'inputs': redact(inputs), 'versions': redact(data.get('versions', {})),
                   'decision': {'choice': redact(choice), 'recorded_response': redact(outcome.get('decision')),
                                'selected_engine': outcome.get('selected_engine', data.get('selected_engine')),
                                'actual_engine': outcome.get('actual_engine', outcome.get('used_engine')),
                                'fallback_reason': redact(outcome.get('fallback_reason', outcome.get('reason')))},
                   'labels': {'reviewer_preference': preference,
                              'policy_validity': {'PASS': 'VALID', 'FAIL': 'INVALID'}.get(policy, 'NOT_PROVEN'),
                              'policy_validity_scope': 'typed_option_membership_only', 'verified_outcome': outcome_label},
                   'provenance': {'opportunity_ref_sha256': fingerprint(source['ref']),
                                  'opportunity_payload_sha256': fingerprint(source['data']),
                                  'outcome_refs_sha256': [fingerprint(r['ref']) for r in outcome_records],
                                  'outcome_payloads_sha256': [fingerprint(r['data']) for r in outcome_records],
                                  'verification_sources': [{'ref_sha256': fingerprint(r['ref']),
                                      'payload_sha256': fingerprint(r['data'])} for r in records.values()
                                      if outcome_label != 'NOT_PROVEN' and ('/nfos_artifacts/' in r['ref'] or
                                          r['data'].get('kind') == 'nfos_verification')],
                                  'review_sha256': fingerprint(review) if preference else None,
                                  'legacy': legacy},
                   'quarantine_reasons': reasons}
            if identity in examples and examples[identity] != row:
                conflicts.add(identity)
            else:
                examples[identity] = row
    for identity in conflicts:
        examples[identity]['quarantine_reasons'].append('conflicting_source_snapshots')
    rows = sorted(examples.values(), key=lambda r: r['id'])
    assign_splits(rows, train_before=train_before, test_after=test_after)
    return {'schema': SCHEMA, 'examples': rows, 'manifest': {'count': len(rows),
            'sha256': fingerprint(rows), 'split_policy': 'project+lineage components with disjoint chronological windows',
            'train_before': train_before, 'test_after': test_after,
            'quarantined': sum(r['split'] == 'quarantine' for r in rows)}}


def assign_splits(rows, *, train_before, test_after):
    """Connected project/lineage groups never cross partitions; crossing time is quarantined."""
    parents = list(range(len(rows)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    seen = {}
    for i, row in enumerate(rows):
        for key in [('project', row['project']), ('lineage', row['lineage']), ('task', row['task'])]:
            if key in seen:
                parents[find(i)] = find(seen[key])
            seen[key] = i
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(find(i), []).append(row)
    for group in groups.values():
        clean = [r for r in group if not r['quarantine_reasons']]
        reasons = set()
        split = 'quarantine'
        if train_before is None or test_after is None or train_before >= test_after:
            reasons.add('split_windows_not_configured')
        elif clean and all(0 < r['decision_at'] <= train_before for r in clean):
            split = 'train'
        elif clean and all(r['decision_at'] >= test_after for r in clean):
            split = 'test'
        elif clean:
            reasons.add('project_or_lineage_crosses_time_windows')
        for row in group:
            row['split'] = 'quarantine' if row['quarantine_reasons'] else split
            row['quarantine_reasons'] = sorted(reasons | set(row['quarantine_reasons']))


def replay(dataset):
    """Replay the recorded typed choices only. This is not an inference/latency benchmark."""
    rows = dataset['examples']
    assert dataset.get('schema') == SCHEMA
    assert dataset['manifest']['sha256'] == fingerprint(rows), 'Dataset hash mismatch'
    outcomes = []
    for row in rows:
        assert not has_future_field(row['inputs']), 'Outcome leaked into decision inputs'
        choice = row['decision']['choice']
        supported = typed_validity({'decision': row['decision'].get('recorded_response', choice)},
                                   row['inputs'].get('options')) == 'PASS'
        preference = row['labels']['reviewer_preference']
        outcomes.append({'id': row['id'], 'route': 'recorded_choice' if supported else 'fallback',
                         'reviewer_agreement': choice == preference['option'] if preference else None,
                         'verified_outcome': row['labels']['verified_outcome'], 'split': row['split']})
    return {'schema': SCHEMA, 'offline': True, 'inference_calls': 0, 'training_calls': 0,
            'examples': outcomes, 'count': len(outcomes), 'dataset_sha256': fingerprint(rows)}


def load_cases(path):
    path = Path(path)
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8'))
        return data['cases'], data.get('reviews', {})
    files = sorted(path.glob('cases/*.json')) + sorted(path.glob('*/cases/*.json'))
    cases, reviews = [], {}
    for file in files:
        case = json.loads(file.read_text(encoding='utf-8'))
        cases.append(case)
        review = file.parent.parent / 'reviews' / file.name
        if review.exists():
            reviews[f'{case["project"]}/{case["task_id"]}'] = json.loads(review.read_text(encoding='utf-8'))
    return cases, reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export', 'replay'])
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--train-before', type=float)
    parser.add_argument('--test-after', type=float)
    args = parser.parse_args()
    if args.action == 'export':
        cases, reviews = load_cases(args.input)
        result = build_dataset(cases, reviews, train_before=args.train_before, test_after=args.test_after)
    else:
        result = replay(json.loads(Path(args.input).read_text(encoding='utf-8')))
    Path(args.output).write_text(canonical(result) + '\n', encoding='utf-8')
    print(canonical({'action': args.action, 'output': args.output, 'sha256': fingerprint(result),
                     'offline': True, 'inference_calls': 0, 'training_calls': 0}))


if __name__ == '__main__':
    main()
