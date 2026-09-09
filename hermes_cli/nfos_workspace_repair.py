"""Maintainer recovery of an idle card's repository binding, with provenance."""
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli.nfos_workspaces import _git


def repair_card(conn, task_id, *, board, delivery_type, expected_delivery_type,
                expected_spec_revision, expected_instruction_revision,
                reason, actor, use_canonical_repo=False, apply=False):
    """Correct routing/classification without replacing historical specifications."""
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise delivery.WorkflowError('Card repair belongs to the maintainer, outside a worker')
    if delivery_type not in {'code', 'report', 'operation'} or not reason or not actor:
        raise delivery.WorkflowError('Specify delivery type, reason and operator')
    from hermes_cli.nfos_runtime import project_config
    from hermes_cli.profiles import profile_exists
    project = project_config(board)
    profile = (project or {}).get('profile')
    if not profile or not profile_exists(profile):
        raise delivery.WorkflowError('The project executor must already exist')
    task = _idle(conn, task_id)
    wf = delivery.get_workflow(conn, task_id)
    if not wf or (task.delivery_type, wf['spec_revision'], task.instruction_revision) != (
            expected_delivery_type, expected_spec_revision, expected_instruction_revision):
        raise delivery.WorkflowError('Card classification or instruction changed since readback')
    path = task.workspace_path
    kind = task.workspace_kind
    if use_canonical_repo:
        repo = Path(project.get('repo_path') or '').expanduser().resolve()
        if delivery_type != 'code' or not project.get('repo_path') or kb._git_toplevel(repo) != repo:
            raise delivery.WorkflowError('Canonical execution requires the configured code repository')
        path, kind = str(repo), 'dir'
    elif delivery_type != 'code':
        if not path or not Path(path).expanduser().exists():
            path = None
        kind = 'dir' if path else 'scratch'
    proposed = dict(delivery_type=delivery_type, requires_repo=delivery_type=='code',
                    assignee=profile, workspace_kind=kind, workspace_path=path,
                    new_spec_required=delivery_type != task.delivery_type,
                    status=task.status, actor=actor, reason=reason)
    if not apply:
        return dict(proposed, apply=False)
    with kb.write_txn(conn):
        current = _idle(conn, task_id)
        current_wf = delivery.get_workflow(conn, task_id)
        if (current.delivery_type, current_wf['spec_revision'], current.instruction_revision,
                current.assignee, current.workspace_path) != (
                expected_delivery_type, expected_spec_revision, expected_instruction_revision,
                task.assignee, task.workspace_path):
            raise delivery.WorkflowError('Card changed during routing repair')
        if not path and kind == 'scratch':
            # Create only the native managed report directory, never a fake repo.
            path = str(kb.resolve_workspace(replace(task,workspace_kind='scratch',workspace_path=None), board=board, conn=conn))
            proposed['workspace_path'] = path
        state = json.loads(current_wf['state_json'])
        state.setdefault('card_repairs', []).append(dict(previous=dict(
            delivery_type=task.delivery_type, requires_repo=task.requires_repo,
            assignee=task.assignee, workspace_kind=task.workspace_kind,
            workspace_path=task.workspace_path), proposed=proposed, at=int(time.time())))
        if proposed['new_spec_required']:
            state['classification_revision_floor'] = current_wf['spec_revision'] + 1
            conn.execute("UPDATE nfos_workflows SET stage='analysis',next_action=? WHERE task_id=?",
                         ('Reuse the preserved TL proposal and save the matching specification revision',task_id))
        if use_canonical_repo:
            state['workspace_policy'] = 'canonical'
        conn.execute('UPDATE tasks SET delivery_type=?,requires_repo=?,assignee=?,workspace_kind=?,workspace_path=? WHERE id=?',
                     (delivery_type,int(delivery_type=='code'),profile,kind,path,task_id))
        conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',(delivery._json(state),task_id))
        kb._append_event(conn,task_id,'nfos_card_repaired',proposed)
    return dict(proposed, apply=True)


def _idle(conn, task_id):
    task = kb.get_task(conn, task_id)
    if not task or task.status not in {'blocked', 'ready', 'todo', 'review'} or task.current_run_id:
        raise delivery.WorkflowError('Workspace repair requires an idle existing card')
    if task.worker_pid and kb._pid_alive(task.worker_pid):
        raise delivery.WorkflowError('Workspace repair cannot interrupt a live executor')
    from hermes_cli.nfos_runtime import previous_runs_termination_pending
    if previous_runs_termination_pending(conn, task_id):
        raise delivery.WorkflowError('Previous worker termination is not confirmed')
    return task


def repair_workspace(conn, task_id, *, board, repo_path, base_sha, expected_workspace,
                     expected_source_sha, reason, actor, apply=False):
    """Plan/apply only an explicit correction to the configured project repository.

    The old checkout stays intact. Same-repository tracked edits are restored
    exactly; unrelated repository files remain at their original source. Neither
    existing delivery evidence nor approvals nor card status are rewritten.
    """
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise delivery.WorkflowError('Workspace repair belongs to the maintainer, outside a worker')
    if not reason or not actor:
        raise delivery.WorkflowError('Record the repair reason and operator')
    from hermes_cli.nfos_runtime import project_config
    project = project_config(board)
    repo = Path(repo_path).expanduser().resolve(strict=True)
    if not project or kb._path_identity(project.get('repo_path')) != kb._path_identity(repo):
        raise delivery.WorkflowError('Destination must be the configured project repository')
    if kb._git_toplevel(repo) != repo or not kb._git_common_dir(repo):
        raise delivery.WorkflowError('Destination is not a Git repository root')
    if len(base_sha) not in (40, 64) or any(c not in '0123456789abcdef' for c in base_sha):
        raise delivery.WorkflowError('Pin the exact existing destination commit')
    if _git(repo, 'rev-parse', '--verify', base_sha + '^{commit}').strip() != base_sha:
        raise delivery.WorkflowError('Destination commit could not be verified')
    identity = dict(board=board, repo_path=str(repo), base_sha=base_sha,
                    expected_workspace=kb._loose_path_identity(expected_workspace),
                    expected_source_sha=expected_source_sha)
    wf = delivery.get_workflow(conn, task_id)
    if not wf:
        raise delivery.WorkflowError('Workspace repair requires an enrolled NFOS card')
    prior = json.loads(wf['state_json']).get('workspace_repair')
    if prior and prior['identity'] != identity:
        raise delivery.WorkflowError('Existing workspace repair has a different identity')
    if prior and prior.get('completed_at'):
        valid, _, error = kb._validate_worktree_ownership(conn, task_id, require_checkout=True)
        if not valid:
            raise delivery.WorkflowError('Repaired workspace changed: ' + error)
        kb._release_worktree_creation_lock(Path(prior['canonical_worktree']), prior)
        return dict(prior, already_applied=True)
    task = _idle(conn, task_id)
    if kb._loose_path_identity(task.workspace_path) != identity['expected_workspace']:
        raise delivery.WorkflowError('Source workspace changed since the operator readback')
    source = Path(task.workspace_path).expanduser().resolve(strict=True)
    source_repo = kb._git_toplevel(source)
    source_sha = _git(source, 'rev-parse', 'HEAD').strip() if source_repo else None
    if source_sha != expected_source_sha:
        raise delivery.WorkflowError('Source commit changed since the operator readback')
    same_repo = source_repo is not None and kb._git_common_dir(source) == kb._git_common_dir(repo)
    if same_repo and source_sha != base_sha:
        raise delivery.WorkflowError('Preserve the existing commit when isolating the same repository')
    previous_delivery = conn.execute('SELECT * FROM task_git_delivery WHERE task_id=?', (task_id,)).fetchone()
    if previous_delivery and previous_delivery['cleanup_state'] != 'not_requested':
        raise delivery.WorkflowError('Resolve the previous cleanup obligation before workspace repair')
    if previous_delivery and previous_delivery['ownership_json']:
        old = json.loads(previous_delivery['ownership_json'])
        _, fingerprint = kb._canonical_delivery_document(old)
        if fingerprint != previous_delivery['ownership_fingerprint'] or old.get('task_id') != task_id:
            raise delivery.WorkflowError('Source ownership evidence is corrupt')
    if not apply:
        return dict(identity, task_id=task_id, status=task.status, same_repository=same_repo,
                    source_preserved=True, apply=False)
    lease, owner = kb._try_acquire_workspace_lease(source, task_id=task_id)
    if lease is None:
        raise delivery.WorkflowError('Source workspace has a live writer: ' + str(owner))
    try:
        with kb.write_txn(conn):
            current = _idle(conn, task_id)
            if current.workspace_path != task.workspace_path or current.branch_name != task.branch_name:
                raise delivery.WorkflowError('Card binding changed before repair')
            state = json.loads(delivery.get_workflow(conn, task_id)['state_json'])
            plan = state.get('workspace_repair')
            if not plan:
                nonce = secrets.token_hex(16)
                target = repo / '.nfos' / 'repairs' / nonce / '.worktrees' / task_id
                branch = f'nfos/{task_id}-repair-{nonce[:12]}'
                plan = dict(identity=identity, task_id=task_id, actor=actor, reason=reason,
                            source=str(source), base_sha=base_sha, repo_root=str(repo),
                            git_common_dir=str(kb._git_common_dir(repo)), canonical_worktree=str(target),
                            branch=branch, creation_nonce=nonce, created_at=int(time.time()),
                            previous_binding=dict(workspace_kind=task.workspace_kind,
                                                  workspace_path=task.workspace_path, branch_name=task.branch_name),
                            previous_git_delivery=dict(previous_delivery) if previous_delivery else None,
                            working_patch=_git(source, 'diff', '--binary', '--no-ext-diff', 'HEAD', '--') if same_repo else '',
                            index_patch=_git(source, 'diff', '--cached', '--binary', '--no-ext-diff', 'HEAD', '--') if same_repo else '',
                            artifacts_location=str(source), same_repository=same_repo)
                state['workspace_repair'] = plan
                conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (delivery._json(state), task_id))
                kb._append_event(conn, task_id, 'nfos_workspace_repair_planned',
                                 {k:plan[k] for k in ['identity', 'actor', 'reason', 'canonical_worktree', 'creation_nonce']})
        target = Path(plan['canonical_worktree'])
        if not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, 'worktree', 'add', '--lock', '--reason', 'hermes-nfos-create:' + plan['creation_nonce'],
                 '-b', plan['branch'], str(target), base_sha)
        if (not kb._worktree_creation_lock_matches(target, plan)
                or kb._git_common_dir(target) != kb._git_common_dir(repo)
                or kb._git_current_branch(target) != plan['branch']
                or _git(target, 'rev-parse', 'HEAD').strip() != base_sha):
            raise delivery.WorkflowError('Repair target does not match its durable creation intent')
        # Restore only before publishing this new checkout to the task. A failed
        # final DB commit leaves the same locked target and saved patches retryable.
        _git(target, 'reset', '--hard', base_sha)
        if plan['working_patch']:
            _git(target, 'apply', '--binary', '--whitespace=nowarn', '-', patch=plan['working_patch'])
        if plan['index_patch']:
            _git(target, 'apply', '--cached', '--binary', '--whitespace=nowarn', '-', patch=plan['index_patch'])
        with kb.write_txn(conn):
            current = _idle(conn, task_id)
            if current.workspace_path != task.workspace_path or current.branch_name != task.branch_name:
                raise delivery.WorkflowError('Card binding changed during repair')
            kb._insert_git_delivery_obligation(conn, task_id, int(time.time()))
            conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?', (task_id,))
            conn.execute('UPDATE task_git_delivery SET ownership_json=NULL,ownership_fingerprint=NULL,'
                         'creation_intent_json=NULL,creation_intent_fingerprint=NULL WHERE task_id=?', (task_id,))
            conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (task_id,))
            kb._seal_materialized_worktree_ownership(conn, task_id, repo_root=repo, worktree=target,
                                                    branch=plan['branch'])
            state = json.loads(delivery.get_workflow(conn, task_id)['state_json'])
            state['workspace_repair']['completed_at'] = int(time.time())
            conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (delivery._json(state), task_id))
            kb._append_event(conn, task_id, 'nfos_workspace_repaired',
                             dict(source=str(source), workspace=str(target), base_sha=base_sha,
                                  actor=actor, reason=reason, previous_status=task.status,
                                  previous_ownership_fingerprint=(dict(previous_delivery).get('ownership_fingerprint') if previous_delivery else None)))
        kb._release_worktree_creation_lock(target, plan)
        return dict(state['workspace_repair'], already_applied=False)
    finally:
        kb._release_workspace_lease(lease)
