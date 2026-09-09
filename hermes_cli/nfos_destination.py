"""Bind delivery verification to the destination in the approved specification."""
import json


def validate(scope):
    from hermes_cli.nfos_delivery import WorkflowError
    if not isinstance(scope, dict) or any(
            not isinstance(scope.get(key), str) or not scope[key].strip()
            for key in ('environment', 'target', 'source', 'authorization_message')):
        raise WorkflowError('delivery_destination needs environment, exact target, source and authorization_message')
    if scope.get('verification_operation') not in {'homolog', 'deploy'}:
        raise WorkflowError('delivery_destination verification_operation must be homolog or deploy')
    return scope


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
    candidate = state.get('homolog_sha') if scope['verification_operation'] == 'homolog' else state.get('integrated_sha')
    if not candidate or not state.get('candidate_tree'):
        return False
    effect = conn.execute("SELECT * FROM nfos_effects WHERE task_id=? AND operation=? AND target=? AND candidate=? ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                          (task_id, scope['verification_operation'], scope['target'], candidate)).fetchone()
    if not effect or effect['status'] != 'confirmed':
        return False
    evidence = json.loads(effect['evidence'])
    return bool(evidence.get('readback') and evidence.get('behavior_evidence')
                and evidence.get('artifact') and evidence.get('candidate') == candidate
                and evidence.get('tree') == state['candidate_tree'])
