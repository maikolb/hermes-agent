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
