"""Bind delivery verification to the destination in the approved specification."""
import json
import re

OPERATIONS = {'homolog', 'deploy', 'pr'}
REPOSITORY = re.compile(r'https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+')
PULL_REQUEST = re.compile(r'https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/pull/[0-9]+')


def validate(scope):
    from hermes_cli.nfos_delivery import WorkflowError
    if not isinstance(scope, dict) or any(
            not isinstance(scope.get(key), str) or not scope[key].strip()
            for key in ('environment', 'target', 'source', 'authorization_message')):
        raise WorkflowError('delivery_destination needs environment, exact target, source and authorization_message')
    if scope.get('verification_operation') not in OPERATIONS:
        raise WorkflowError('delivery_destination verification_operation must be homolog, deploy or pr')
    if scope['verification_operation'] == 'pr' and not REPOSITORY.fullmatch(scope['target']):
        raise WorkflowError('A pr delivery_destination targets the exact GitHub repository URL')
    return scope


def review_only(scope):
    """The approved phase ends at the review PR: no homolog, merge or deploy."""
    return bool(scope) and scope['verification_operation'] == 'pr'


def review_pr_problem(evidence):
    """Why a review-PR readback does not verify the destination, or None."""
    if not PULL_REQUEST.fullmatch(str(evidence.get('pull_request') or '')):
        return 'Review PR readback needs pull_request, the exact GitHub pull request URL'
    if evidence.get('ci_status') != 'success':
        return 'Review PR readback needs ci_status success for the exact candidate'
    return None


def destination(conn, task_id):
    from hermes_cli import nfos_delivery as d
    spec = d.get_spec(conn, task_id)
    scope = json.loads(spec['content']).get('delivery_destination') if spec else None
    return validate(scope) if scope is not None else None


def verified(conn, task_id, state):
    from hermes_cli import nfos_delivery as d
    scope = destination(conn, task_id)
    if not scope:
        return False
    operation = scope['verification_operation']
    candidate = {'homolog': state.get('homolog_sha'), 'pr': state.get('candidate_sha')}.get(
        operation, state.get('integrated_sha'))
    if not candidate or not state.get('candidate_tree'):
        return False
    effect = conn.execute("SELECT * FROM nfos_effects WHERE task_id=? AND operation=? AND target=? AND candidate=? ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                          (task_id, operation, scope['target'], candidate)).fetchone()
    if not effect or effect['status'] != 'confirmed':
        return False
    evidence = json.loads(effect['evidence'])
    if not evidence.get('readback') or evidence.get('candidate') != candidate or evidence.get('tree') != state['candidate_tree']:
        return False
    if operation == 'pr':
        return review_pr_problem(evidence) is None
    return bool(evidence.get('behavior_evidence') and evidence.get('artifact'))
