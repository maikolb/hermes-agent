"""Give retained shared checkouts the same isolation as new NFOS work."""
import json
import hashlib
import subprocess
import time
from pathlib import Path

from hermes_cli import kanban_db as kb, nfos_delivery as delivery


def _git(path, *args, patch=None):
    if patch is None:
        result = kb._cleanup_git(path, *args, timeout=60)
    else:
        result = subprocess.run(
            ['git', '-C', str(path), *args], input=patch.encode('utf-8'), capture_output=True,
            timeout=60, env=kb.noninteractive_git_env(),
            **kb.windows_hidden_popen_kwargs())
        result.stdout = result.stdout.decode('utf-8', errors='replace')
        result.stderr = result.stderr.decode('utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(f'Retained workspace Git operation failed: {result.stderr.strip()}')
    return result.stdout


def _configured_common_dir(configured):
    """Common Git dir of the configured repository, only when it is a normal repository root."""
    top = kb._git_toplevel(configured) if configured is not None else None
    if top is None or kb._path_identity(top) != kb._path_identity(configured):
        return None
    common = kb._git_common_dir(configured)
    if common is None or common.name != '.git' or kb._path_identity(common.parent) != kb._path_identity(configured):
        return None
    return common


def _retarget_unmaterialized_worktree(conn, task, configured):
    """NFOS_WORKTREE_CONFIGURED_REPO_20261016: anchor a worktree that does not exist yet in the configured repository.

    A legacy card can still declare ``<envelope>/.worktrees/<id>`` before any checkout exists. The nearest
    existing Git ancestor of that path is the envelope, so materialization would seal a receipt against the
    wrong repository. Only an unsealed card, with no creation intent and no checkout, is moved; the branch is
    kept and the normal materialization seals the receipt in the configured repository.
    """
    if task.workspace_kind != 'worktree' or not task.workspace_path or configured is None:
        return None
    declared = Path(task.workspace_path).expanduser()
    target = configured / '.worktrees' / task.id
    if not declared.is_absolute() or declared.exists() or declared.is_symlink() or target.exists() or target.is_symlink():
        return None
    configured_common = _configured_common_dir(configured)
    anchor = kb._repo_root_for_worktree_target(declared.parent)
    if configured_common is None or anchor is None or kb._git_common_dir(anchor) == configured_common:
        return None
    with kb.write_txn(conn):
        receipt = conn.execute('SELECT ownership_json,ownership_fingerprint,creation_intent_json '
                               'FROM task_git_delivery WHERE task_id=?', (task.id,)).fetchone()
        if receipt is not None and any(value is not None for value in tuple(receipt)):
            return None
        updated = conn.execute("UPDATE tasks SET workspace_path=? WHERE id=? AND status='ready' "
                               "AND current_run_id IS NULL AND worker_pid IS NULL AND workspace_path=?",
                               (str(target), task.id, task.workspace_path))
        if updated.rowcount != 1:
            return None
        kb._append_event(conn, task.id, 'nfos_workspace_retargeted', {
            'from': task.workspace_path, 'to': str(target), 'configured_repo': str(configured),
            'declared_anchor': str(anchor)})
    return kb.get_task(conn, task.id)


def _registered_in_configured_repo(configured, workspace, branch, git_dir):
    """The configured repository lists this exact folder on this branch, and the admin dir points back at it."""
    try:
        listing = _git(configured, 'worktree', 'list', '--porcelain')
    except RuntimeError:
        return False
    want = kb._loose_path_identity(workspace)
    entries = []
    for line in listing.splitlines():
        if line.startswith('worktree '):
            entries.append({'path': line[len('worktree '):]})
        elif line.startswith('branch ') and entries:
            entries[-1]['branch'] = line[len('branch '):]
    if not any(kb._loose_path_identity(e['path']) == want and e.get('branch') == f'refs/heads/{branch}' for e in entries):
        return False
    try:
        back = Path(git_dir, 'gitdir').read_text(encoding='utf-8').strip()
    except OSError:
        return False
    # Git writes an absolute path; a relative one (worktree.useRelativePaths) is relative to the admin dir.
    return bool(back) and kb._loose_path_identity(Path(git_dir) / back) == kb._loose_path_identity(Path(workspace) / '.git')


def _git_operation_in_progress(workspace, git_dir):
    """Unmerged paths or a cherry-pick, merge, revert, rebase or sequencer state in this checkout."""
    if any((Path(git_dir) / name).exists() for name in (
            'CHERRY_PICK_HEAD', 'MERGE_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')):
        return True
    try:
        return bool(_git(workspace, 'ls-files', '-u').strip())
    except RuntimeError:
        return True


def _reconcile_drifted_receipt(conn, task, state, configured, board):
    """NFOS_WORKTREE_CONFIGURED_REPO_20261016: reseal, through repair_workspace, a card's own live checkout.

    The sealed-receipt validator stays strict. This runs only when every guard shows the checkout is this
    card's own idle worktree, registered in the configured repository; any false guard leaves the existing
    behavior (spawn failure, breaker, Principal). Returns None when it does not apply, else (task, lease owner).
    A card whose workspace was already repaired once is left to the Principal: repair_workspace keeps one plan.
    """
    from hermes_cli.nfos_runtime import previous_runs_termination_pending
    from hermes_cli.nfos_workspace_repair import repair_workspace
    if task.workspace_kind != 'worktree' or not task.workspace_path or configured is None or state.get('workspace_repair'):
        return None
    # G1: idle card (ready, no run or live pid is checked by the caller) with no cleanup in flight.
    row = conn.execute('SELECT ownership_json,cleanup_state FROM task_git_delivery WHERE task_id=?', (task.id,)).fetchone()
    if row is None or not row['ownership_json'] or row['cleanup_state'] != 'not_requested':
        return None
    if previous_runs_termination_pending(conn, task.id):
        return None
    # G2: an intact receipt that belongs to this card, this folder and this branch name; only the live checkout diverges.
    if not kb._validate_worktree_ownership(conn, task.id, require_checkout=False)[0]:
        return None
    if kb._validate_worktree_ownership(conn, task.id, require_checkout=True)[0]:
        return None
    workspace = Path(task.workspace_path).expanduser()
    if not workspace.is_dir() or not kb._is_linked_worktree_checkout(workspace):
        return None
    # G3: the configured project repository is the authority, never the receipt.
    configured_common = _configured_common_dir(configured)
    live_common = kb._git_common_dir(workspace)
    if configured_common is None or live_common is None or live_common != configured_common:
        return None
    # G4: strict card identity of the folder and of the live branch (not the validator's substring match).
    live_branch = kb._git_current_branch(workspace)
    if not live_branch or not kb._task_owns_worktree_identity(task.id, workspace, live_branch):
        return None
    # G5: registered by the configured repository, with the admin dir pointing back at this folder.
    live_git_dir = kb._git_dir(workspace)
    if live_git_dir is None or not _registered_in_configured_repo(configured, workspace, live_branch, live_git_dir):
        return None
    # G7: no Git operation in progress. A conflicted cherry-pick, merge, revert or rebase makes
    # `git diff HEAD` unusable as a patch, and repair_workspace would persist that broken plan and reuse it
    # forever, so the checkout must be settled by its owner first.
    if _git_operation_in_progress(workspace, live_git_dir):
        return None
    # G6: no live writer in the checkout.
    lease, owner = kb._try_acquire_workspace_lease(workspace, task_id=task.id)
    if lease is None:
        return task, owner
    kb._release_workspace_lease(lease)
    live_head = _git(workspace, 'rev-parse', 'HEAD').strip()
    repair_workspace(conn, task.id, board=board, repo_path=str(configured), base_sha=live_head,
                     expected_workspace=task.workspace_path, expected_source_sha=live_head, apply=True,
                     actor='Runtime',
                     reason="Sealed worktree diverged from its receipt; the live checkout is the card's own, "
                            'idle and registered in the configured project repository')
    return kb.get_task(conn, task.id), None


def isolate_retained_workspace(conn, task, *, board=None):
    """Return a refreshed task and any live source owner that deferred migration.

    Source files are never reset, removed or moved. A durable snapshot of tracked
    work is restored only in the new task-owned checkout, before its first claim.
    Untracked/ignored artifacts remain accessible at the retained source path.
    The existing source lease prevents copying another live writer mid-edit.
    """
    wf = delivery.get_workflow(conn, task.id)
    if not wf:
        return task, None
    state = json.loads(wf['state_json'])
    if state.get('workspace_policy') == 'canonical':
        # Explicit maintainer reconciliation; the dispatcher's normal workspace
        # lease still serializes writers on this checkout.
        return task, None
    if not state.get('legacy_adoption'):
        return task, None
    if task.status != 'ready' or task.current_run_id or task.worker_pid:
        return task, None
    # The inherited directory is evidence, not the project's repository
    # authority. Otherwise recovery can manufacture a fresh, valid ownership
    # receipt for the wrong Git repository on every subsequent adoption.
    from hermes_cli.nfos_runtime import project_config
    from hermes_cli.nfos_workspace_repair import repair_workspace
    project = project_config(board) or {}
    source = Path(task.workspace_path).expanduser().resolve() if task.workspace_path else None
    configured = Path(project['repo_path']).expanduser().resolve() if project.get('repo_path') else None
    repair = state.get('workspace_repair')
    pending_repair = repair and not repair.get('completed_at')
    mismatch = (source and configured and kb._git_toplevel(source) is not None
                and kb._git_common_dir(source) != kb._git_common_dir(configured))
    if pending_repair or mismatch:
        lease, owner = kb._try_acquire_workspace_lease(source, task_id=task.id)
        if lease is None:
            return task, owner
        kb._release_workspace_lease(lease)
        identity = repair['identity'] if pending_repair else dict(
            board=board, repo_path=str(configured), base_sha=_git(configured, 'rev-parse', 'HEAD').strip(),
            expected_workspace=task.workspace_path, expected_source_sha=_git(source, 'rev-parse', 'HEAD').strip())
        repair_workspace(conn, task.id, **identity, apply=True, actor='Runtime',
                         reason='Retained workspace differs from the configured project repository; preserve its artifacts and repair before dispatch')
        return kb.get_task(conn, task.id), None
    retargeted = _retarget_unmaterialized_worktree(conn, task, configured)
    if retargeted is not None:
        return retargeted, None
    reconciled = _reconcile_drifted_receipt(conn, task, state, configured, board)
    if reconciled is not None:
        return reconciled
    snapshot = state.get('retained_workspace')
    if snapshot and snapshot.get('restored_at'):
        return task, None
    if not snapshot and task.workspace_kind != 'dir':
        return task, None
    source = Path(snapshot['source'] if snapshot else task.workspace_path).expanduser().resolve()
    repo = kb._git_toplevel(source)
    if repo is None or repo != source:
        # A directory of reports or a subdirectory is not a Git anchor. Keep
        # its existing semantics rather than guessing a repository or scope.
        return task, None
    lease, owner = kb._try_acquire_workspace_lease(source, task_id=task.id)
    if lease is None:
        return task, owner
    try:
        if not snapshot:
            snapshot = {
                'source': str(source), 'base_sha': _git(source, 'rev-parse', 'HEAD').strip(),
                'working_patch': _git(source, 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'),
                'index_patch': _git(source, 'diff', '--cached', '--binary', '--no-ext-diff', 'HEAD', '--'),
                'source_branch': _git(source, 'branch', '--show-current').strip(),
                'artifacts_location': str(source), 'created_at': int(time.time()),
            }
            with kb.write_txn(conn):
                state = json.loads(delivery.get_workflow(conn, task.id)['state_json'])
                state['retained_workspace'] = snapshot
                conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',
                             (delivery._json(state), task.id))
                kb._insert_git_delivery_obligation(conn, task.id, int(time.time()))
                conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?', (task.id,))
                # claim_task refuses this pending snapshot until restoration
                # and its completion event commit together below.
                conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (task.id,))
                kb._append_event(conn, task.id, 'nfos_workspace_snapshot', {
                    'source': str(source), 'base_sha': snapshot['base_sha'],
                    'tracked_changes': bool(snapshot['working_patch'])})
        common = kb._git_common_dir(source)
        if common is None or common.name != '.git':
            raise RuntimeError('Retained checkout has no identifiable main repository')
        target, branch = kb._materialize_task_owned_worktree(
            conn, task, repo_root=common.parent, target=common.parent / '.nfos' / '.worktrees' / task.id,
            branch=f'nfos/{task.id}', initial_sha=snapshot['base_sha'])
        # This checkout is still unpublished to the task. Replaying its exact
        # snapshot after a crash is safe only while no executor can claim it.
        current = kb.get_task(conn, task.id)
        if current.status != 'ready' or current.current_run_id:
            raise RuntimeError('Retained task changed before workspace handoff')
        valid, _, reason = kb._validate_worktree_ownership(conn, task.id, require_checkout=True)
        if not valid:
            raise RuntimeError(f'Retained target ownership changed: {reason}')
        _git(target, 'reset', '--hard', snapshot['base_sha'])
        if snapshot['working_patch']:
            _git(target, 'apply', '--binary', '--whitespace=nowarn', '-', patch=snapshot['working_patch'])
        if snapshot['index_patch']:
            _git(target, 'apply', '--cached', '--binary', '--whitespace=nowarn', '-', patch=snapshot['index_patch'])
        with kb.write_txn(conn):
            state = json.loads(delivery.get_workflow(conn, task.id)['state_json'])
            state['retained_workspace']['restored_at'] = int(time.time())
            conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (delivery._json(state), task.id))
            updated = conn.execute("UPDATE tasks SET workspace_kind='worktree',workspace_path=?,branch_name=? "
                                   "WHERE id=? AND status='ready' AND current_run_id IS NULL",
                                   (str(target), branch, task.id))
            if updated.rowcount != 1:
                raise RuntimeError('Retained task changed during workspace handoff')
            kb._append_event(conn, task.id, 'nfos_workspace_isolated', {
                'source': str(source), 'workspace': str(target), 'base_sha': snapshot['base_sha'],
                'artifacts_location': snapshot['artifacts_location']})
            conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',author='Runtime',"
                         "answer='Workspace restored from the saved snapshot; no delivery was repeated',resolved_at=? "
                         "WHERE task_id=? AND status='pending' AND json_extract(context,'$.runtime_operation')='workspace_restore'",
                         (int(time.time()), task.id))
        return kb.get_task(conn, task.id), None
    except Exception as exc:
        with kb.write_txn(conn):
            # Infrastructure preparation belongs to the same Principal queue,
            # even though no worker has been started. Do not invent a run or
            # repeatedly notify the same unresolved fault on every tick.
            pending = conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending' "
                                   "AND json_extract(context,'$.runtime_operation')='workspace_restore'", (task.id,)).fetchone()
            if pending is None:
                wf = delivery.get_workflow(conn, task.id)
                state = json.loads(wf['state_json'])
                previous = conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (task.id,)).fetchone()[0]
                sequence = conn.execute('SELECT COUNT(*) FROM nfos_decisions WHERE task_id=?', (task.id,)).fetchone()[0]
                did = 'dec_' + hashlib.sha256(f'{task.id}:workspace_restore:{sequence}'.encode()).hexdigest()[:24]
                question = 'Preparação da pasta de trabalho interrompida: ' + str(exc)
                context = {'runtime_operation': 'workspace_restore', 'legacy_adoption': state['legacy_adoption'],
                           'source': str(source), 'error': str(exc)}
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                             (did, task.id, previous or 0, 'impediment', question, delivery._json(context), wf['spec_revision'], int(time.time())))
                kb._append_event(conn, task.id, 'nfos_workspace_restore_failed', {'error': str(exc)})
                kb._append_event(conn, task.id, 'nfos_principal_requested', {'decision_id': did, 'kind': 'impediment', 'question': question}, run_id=previous)
        raise
    finally:
        kb._release_workspace_lease(lease)
