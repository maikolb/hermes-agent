"""Principal acceptance within the existing NFOS decisions and artifacts."""
from __future__ import annotations

import hashlib
import json
import os

from hermes_cli import nfos_delivery as d


def settings():
    from hermes_cli.config import load_config
    return (load_config().get('kanban') or {}).get('delivery') or {}


def required(conn, task_id):
    workflow = d.get_workflow(conn, task_id)
    if not workflow:
        return False
    if settings().get('principal_validation') is False:
        # BLOCK_LESS_20260910 (premissa do owner, 10/09): desligado no perfil vale para toda spec,
        # inclusive as que gravam delivery_destination.
        return False
    spec = d.get_spec(conn, task_id)
    if spec and json.loads(spec['content']).get('delivery_destination'):
        return True
    request = d.get_request(conn, workflow['request_id'])
    project = json.loads(request['payload']).get('project', {}) if request else {}
    # The profile switch applies to retained cards too. A request cannot opt
    # out of the profile policy with a false value in its saved payload.
    return settings().get('principal_validation') is True or project.get('principal_validation') is True


def worker_model_args(task):
    policy = settings()
    model = policy.get('worker_model') or task.model_override
    provider = policy.get('worker_provider') or task.provider_override
    effort = policy.get('worker_reasoning_effort') or task.reasoning_effort
    args = ['-m', model] if model else []
    if model and provider:
        args += ['--provider', provider]
    if effort:
        args += ['--reasoning', effort]
    return args


def identity(conn, task_id, kind):
    spec = d.get_spec(conn, task_id)
    if not spec:
        raise d.WorkflowError('Principal acceptance requires the persisted spec')
    task = d._kb().get_task(conn, task_id)
    result = d._legacy_spec_identity(task, spec)
    result['spec_revision'] = spec['revision']
    if kind == 'final_review':
        report = d._artifact(conn, task_id, 'report')
        if not report:
            raise d.WorkflowError('Final review requires the saved report')
        result.update(report_id=report['id'], report_revision=report['revision'],
                      report_sha256=hashlib.sha256(report['content'].encode()).hexdigest(),
                      evidence_sha256=hashlib.sha256(report['evidence'].encode()).hexdigest())
        state = json.loads(d.get_workflow(conn, task_id)['state_json'])
        result['candidate'] = {k: state.get(k) for k in (
            'candidate_sha', 'candidate_tree', 'homolog_sha', 'homolog_evidence',
            'integrated_sha', 'artifact', 'production_readback', 'homologation_decision')}
        from hermes_cli.nfos_destination import destination
        scope = destination(conn, task_id)
        if scope:
            result['delivery_destination'] = scope
            result['delivery_readback'] = state.get('delivery_readback')
            result['destination_effects'] = [dict(row) for row in conn.execute(
                "SELECT id,status,evidence FROM nfos_effects WHERE task_id=? AND operation=? AND target=? ORDER BY id",
                (task_id, scope['verification_operation'], scope['target']))]
    return result


def accepted(conn, task_id, kind):
    if not required(conn, task_id):
        return True
    try:
        current = identity(conn, task_id, kind)
    except d.WorkflowError:
        return False
    # A newer rejection/pending review revokes the former acceptance.
    row = conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? AND kind=? ORDER BY rowid DESC LIMIT 1',
                       (task_id, kind)).fetchone()
    return bool(row and row['status'] == 'resolved' and row['action'] == 'continue'
                and row['author'] == 'Principal'
                and json.loads(row['context']).get('acceptance_identity') == current
                and json.loads(row['context']).get('assessment'))


def require_spec(conn, task_id):
    if not accepted(conn, task_id, 'spec_review'):
        raise d.WorkflowError('Principal spec acceptance is pending; ask kind=spec_review and wait before implementation')


def assess(conn, decision, assessment):
    """Validate coverage and current bytes, never infer semantic truth from prose."""
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise d.WorkflowError('Only the Principal coordinator can accept a worker delivery')
    kind = decision['kind']
    task_id = decision['task_id']
    context = json.loads(decision['context'])
    if context.get('acceptance_identity') != identity(conn, task_id, kind):
        raise d.WorkflowError('Spec, instruction, candidate or report changed; request a current review')
    d._require_current_instruction_spec(conn, task_id)
    if not isinstance(assessment, dict) or not all(
            isinstance(assessment.get(k), str) and assessment[k].strip()
            for k in ('request_alignment', 'scope_assessment')):
        raise d.WorkflowError('Principal assessment needs request_alignment and scope_assessment')
    spec = d.get_spec(conn, task_id)
    expected = {c['id'] for c in json.loads(spec['content'])['criteria']}
    rows = assessment.get('criteria')
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(expected) or {r.get('id') for r in rows} != expected):
        raise d.WorkflowError('Principal must assess every spec criterion exactly once')
    if any(r.get('verdict') != 'accept' or not isinstance(r.get('observation'), str)
           or not r['observation'].strip() for r in rows):
        raise d.WorkflowError('Each accepted criterion needs an explicit verdict and observation; otherwise request changes')
    if kind == 'final_review':
        require_spec(conn, task_id)
        report = d._artifact(conn, task_id, 'report')
        results = d._report_results(spec, json.loads(report['content']))
        if any(r['status'] != 'PASS' for r in results.values()):
            raise d.WorkflowError('Unproven criteria cannot receive final acceptance')
        checks = json.loads(report['evidence']).get('artifact_checks', [])
        for row in rows:
            refs = row.get('evidence')
            linked = [c for c in checks if row['id'] in c['criteria'] and c.get('status') == 'verified_local']
            # Principal names the precise inspected artifact, not just "looks good".
            if not isinstance(refs, list) or not refs or any(
                    not any(ref in (c['ref'], c.get('path')) for c in linked) for ref in refs):
                raise d.WorkflowError('Each final criterion needs inspected local report evidence or a saved external readback')
        for check in checks:
            if check.get('status') == 'verified_local':
                actual = d._inspect_local_evidence(check['path'])
                if any(actual[k] != check[k] for k in ('sha256', 'size_bytes')):
                    raise d.WorkflowError('Report evidence changed; save and review the current evidence')
    return json.loads(d._json(assessment))
