"""Typed staging preparation. These receipts are never publication authority.

No database/schema migration: use the existing decision/effect journal, with
separate kinds so a staging merge cannot satisfy production's merge gate.
"""
import json
import re

from hermes_cli import nfos_delivery as d

OPERATIONS = {'staging_pr', 'staging_merge'}
FIELDS = ('candidate_sha', 'candidate_tree', 'repository', 'base_ref', 'target')


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None


def identity(conn, task_id, preparation):
    """Bind the requested staging destination to the current task and spec."""
    d._require_current_instruction_spec(conn, task_id)
    spec = d.get_spec(conn, task_id)
    task = d._kb().get_task(conn, task_id)
    wf = d.get_workflow(conn, task_id)
    state = json.loads(wf['state_json'])
    if task.delivery_type != 'code' or not isinstance(preparation, dict):
        raise d.WorkflowError('Preparation needs a code task and typed staging destination')
    if set(preparation) != set(FIELDS):
        raise d.WorkflowError('Preparation needs exactly candidate SHA/tree, repository, base_ref and target')
    if any(not _sha(preparation.get(k)) or preparation[k] != state.get(k)
           for k in ('candidate_sha', 'candidate_tree')):
        raise d.WorkflowError('Preparation must identify the current candidate SHA and tree')
    repo = preparation.get('repository')
    if (not isinstance(repo, str)
            or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', repo)
            or repo.endswith('/.') or repo.endswith('/..') or repo.endswith('.git')
            or preparation.get('base_ref') != 'staging'
            or preparation.get('target') != repo + '/tree/staging'):
        raise d.WorkflowError('Preparation permits only the exact GitHub repository staging destination')
    return dict(preparation, task_id=task_id, spec_revision=spec['revision'],
                instruction_revision=task.instruction_revision)


def check_current(conn, row):
    saved = json.loads(row['context']).get('preparation_identity') or {}
    current = identity(conn, row['task_id'], {k: saved.get(k) for k in FIELDS})
    if saved != current or row['spec_revision'] != current['spec_revision']:
        raise d.WorkflowError('Preparation candidate, spec or instructions changed; request a new review')
    return current


def authorized(conn, task_id, target, candidate):
    row = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='preparation' "
                       "ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
    if (not row or row['status'] != 'resolved' or row['action'] != 'continue'
            or row['author'] != 'Principal'):
        raise d.WorkflowError('Principal staging preparation review is pending')
    prepared = check_current(conn, dict(row))
    if candidate != prepared['candidate_sha'] or target != prepared['target']:
        raise d.WorkflowError('Use only the exact prepared candidate and staging destination')
    return {'preparation_identity': prepared, 'preparation_decision': row['id']}


def confirmed_pr(conn, task_id, candidate, target):
    row = conn.execute("SELECT * FROM nfos_effects WHERE task_id=? AND operation='staging_pr' "
                       "AND candidate=? AND target=? AND status='confirmed' ORDER BY updated_at DESC LIMIT 1",
                       (task_id, candidate, target)).fetchone()
    if not row:
        raise d.WorkflowError('Read back the staging PR before integrating it')
    return json.loads(row['evidence'])


def _recorded_preparation(conn, effect):
    """STAGING_RECORD_MODE_20261017: em modo registro o begin_effect não grava a preparação no efeito (o registro não
    conduz), e o reconcile a exige. A preparação sai da decisão do Principal que já existe para esta tarefa: a mais recente
    resolvida como continue, cuja identidade aponta exatamente a candidata e o destino do efeito. Sem decisão assim,
    nada é derivado e o recibo segue recusado: autoridade nova nunca nasce aqui."""
    rows = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='preparation' AND status='resolved' "
                        "AND action='continue' AND author='Principal' ORDER BY rowid DESC", (effect['task_id'],)).fetchall()
    for row in rows:
        prepared = json.loads(row['context']).get('preparation_identity') or {}
        if (prepared.get('task_id') == effect['task_id'] and prepared.get('candidate_sha') == effect['candidate']
                and prepared.get('target') == effect['target']):
            return {'preparation_identity': prepared, 'preparation_decision': row['id']}
    return {}


def reconcile(conn, effect, found, evidence, state):
    """Read historical intent even after a spec change; never grant new authority."""
    prior = json.loads(effect['evidence'] or '{}')
    if not (prior.get('preparation_identity') and prior.get('preparation_decision')):
        prior = dict(prior, **_recorded_preparation(conn, effect))
    prepared = prior.get('preparation_identity') or {}
    decision = d.get_decision(conn, prior.get('preparation_decision'))
    if (not decision or decision['task_id'] != effect['task_id'] or decision['kind'] != 'preparation'
            or decision['status'] != 'resolved' or decision['action'] != 'continue'
            or decision['author'] != 'Principal'
            or prepared != json.loads(decision['context']).get('preparation_identity')
            or prepared.get('task_id') != effect['task_id']
            or prepared.get('candidate_sha') != effect['candidate']
            or prepared.get('target') != effect['target']):
        raise d.WorkflowError('Staging receipt lacks its original preparation identity')
    for key in ('candidate', 'repository', 'base_ref', 'target'):
        expected = prepared['candidate_sha'] if key == 'candidate' else prepared[key]
        if evidence.get(key) != expected:
            raise d.WorkflowError('Read back the exact prepared staging destination and candidate')
    if found:
        if (evidence.get('tree') != prepared['candidate_tree']
                or not isinstance(evidence.get('url'), str)
                or not re.fullmatch(re.escape(prepared['repository']) + r'/pull/[1-9][0-9]*', evidence['url'])):
            raise d.WorkflowError('Read back the exact staging PR URL and candidate tree')
        if effect['operation'] == 'staging_merge':
            pr = confirmed_pr(conn, effect['task_id'], effect['candidate'], effect['target'])
            if not _sha(evidence.get('integrated_sha')) or evidence['url'] != pr['url']:
                raise d.WorkflowError('Read back the exact integrated staging SHA and confirmed PR')
    result = dict(evidence, preparation_identity=prepared, preparation_decision=decision['id'])
    if effect['status'] == 'confirmed' and result != prior:
        raise d.WorkflowError('Confirmed staging receipt is immutable; reconcile the exact prior result')
    if found and effect['operation'] == 'staging_merge':
        if state.get('candidate_sha') == effect['candidate'] and state.get('candidate_tree') == prepared['candidate_tree']:
            state.update(staging_sha=evidence['integrated_sha'], staging_readback=result)
    return result
