"""NFOS_WORKTREE_CONFIGURED_REPO_20261016: worktrees are sealed against the configured project repository.

Layout of the fixture, as in the concursa-ai board: ``P`` is an "envelope" repository with one commit and the
configured repository ``P/source`` is a separate repository nested inside it.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli.nfos_runtime import adopt_existing_tasks

DRIFT_REASON = 'Sealed worktree diverged from its receipt'


def git(repo, *args, check=True):
    result = kb._cleanup_git(Path(repo), *args)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def init_repo(path, name, branch='trunk'):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-b', branch)
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    git(path, 'config', 'user.name', 'Fixture')
    (path / 'file.txt').write_text(f'{name}\n', encoding='utf-8')
    git(path, 'add', 'file.txt')
    git(path, 'commit', '-m', name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    monkeypatch.setattr(kb, '_memory_pressure_level', lambda: 'normal')
    envelope = tmp_path / 'Concursa_ai'
    init_repo(envelope, 'Initialize project')
    source = envelope / 'source'
    init_repo(source, 'real repository')
    project = {'enabled': True, 'profile': 'default', 'workers': 2, 'repo_path': str(source), 'delivery_type': 'code'}
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': {'projects': {'pilot': project}}}}), encoding='utf-8')
    repairs = []
    from hermes_cli import nfos_workspace_repair as repair_module
    original = repair_module.repair_workspace

    def spy(*args, **kwargs):
        repairs.append(kwargs.get('reason', ''))
        return original(*args, **kwargs)

    monkeypatch.setattr(repair_module, 'repair_workspace', spy)
    with kb.connect_closing() as conn:
        yield SimpleNamespace(conn=conn, envelope=envelope, source=source, project=project, tmp=tmp_path, repairs=repairs)


def new_card(env, workspace_path=None):
    tid = kb.create_task(env.conn, title='Legacy card', assignee='default', workspace_kind='worktree',
                         workspace_path=workspace_path, delivery_type='code')
    adopt_existing_tasks(env.conn, board='pilot', project=env.project)
    return tid


def failures(env):
    return [dict(r)['payload'] for r in env.conn.execute("SELECT payload FROM task_events WHERE kind IN ('spawn_failed','gave_up')")]


def dispatch(env):
    result = kb.dispatch_once(env.conn, board='pilot', max_spawn=2, reconcile_orphans=False,
                              spawn_fn=lambda task, workspace: os.getpid())
    if not result.spawned:
        print([dict(r) for r in env.conn.execute("SELECT kind,payload FROM task_events WHERE kind IN ('spawn_failed','gave_up')")])
    return result


def events(env, tid, kind):
    return [json.loads(r['payload'] or '{}') for r in
            env.conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


def seal_envelope_receipt(env, tid, workspace, branch, tamper=False):
    """Store the receipt the runtime wrote against the envelope, as measured on the concursa-ai board."""
    payload = {
        'schema_version': 1, 'task_id': tid, 'repo_root': str(env.envelope.resolve()),
        'git_common_dir': str(env.envelope.resolve() / '.git'),
        'git_dir': str(env.envelope.resolve() / '.git' / 'worktrees' / tid),
        'canonical_worktree': str(Path(workspace).resolve()), 'branch': branch,
        'creation_nonce': '1' * 32, 'created_at': 1, 'initial_sha': git(env.envelope, 'rev-parse', 'HEAD'),
    }
    encoded, fingerprint = kb._canonical_delivery_document(payload)
    if tamper:
        fingerprint = hashlib.sha256(b'tampered').hexdigest()
    with kb.write_txn(env.conn):
        if env.conn.execute('SELECT 1 FROM task_git_delivery WHERE task_id=?', (tid,)).fetchone() is None:
            kb._insert_git_delivery_obligation(env.conn, tid, 1)
        env.conn.execute("UPDATE tasks SET workspace_kind='worktree',workspace_path=?,branch_name=? WHERE id=?",
                         (str(Path(workspace).resolve()), branch, tid))
        env.conn.execute('UPDATE task_git_delivery SET ownership_json=?,ownership_fingerprint=? WHERE task_id=?',
                         (encoded, fingerprint, tid))


def drifted_card(env, *, repo=None, folder=None, live_branch=None, detach=False, tamper=False):
    """A card whose live worktree is its own in the configured repo, but whose receipt names the envelope."""
    repo = repo or env.source
    tid = new_card(env)
    wt = repo / '.worktrees' / (folder or tid)
    live_branch = live_branch or f'nfos/{tid}-focal'
    git(repo, 'worktree', 'add', '-b', live_branch, str(wt))
    (wt / 'file.txt').write_text('committed in the worktree\n', encoding='utf-8')
    git(wt, 'commit', '-am', 'local commit')
    (wt / 'file.txt').write_text('uncommitted tracked edit\n', encoding='utf-8')
    (wt / 'untracked.txt').write_text('stays behind\n', encoding='utf-8')
    if detach:
        git(wt, 'stash')
        git(wt, 'checkout', '--detach')
    seal_envelope_receipt(env, tid, wt, f'concursa-ai/{tid}-card', tamper=tamper)
    return tid, wt


def assert_not_reconciled(env, tid):
    assert [r for r in env.repairs if r.startswith(DRIFT_REASON)] == []
    assert events(env, tid, 'nfos_workspace_repaired') == []
    assert events(env, tid, 'spawn_failed'), 'the card must keep failing as before, nothing hides it'


# --- the two tests that fail without the fix ------------------------------------------------------------------

def test_unmaterialized_legacy_card_anchors_in_configured_repo(env):
    tid = new_card(env)
    declared = env.envelope / '.worktrees' / tid
    env.conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(declared), tid))
    env.conn.commit()
    result = dispatch(env)
    assert [row[0] for row in result.spawned] == [tid], failures(env)
    task = kb.get_task(env.conn, tid)
    workspace = Path(task.workspace_path)
    configured_common = kb._git_common_dir(env.source)
    assert kb._git_common_dir(workspace) == configured_common
    assert kb._path_identity(workspace.parent.parent) == kb._path_identity(env.source)
    receipt = json.loads(env.conn.execute('SELECT ownership_json FROM task_git_delivery WHERE task_id=?', (tid,)).fetchone()[0])
    assert kb._loose_path_identity(receipt['git_common_dir']) == kb._loose_path_identity(configured_common)
    assert kb._validate_worktree_ownership(env.conn, tid, require_checkout=True)[0]
    moved = events(env, tid, 'nfos_workspace_retargeted')
    assert len(moved) == 1 and moved[0]['from'] == str(declared) and moved[0]['to'] == str(env.source / '.worktrees' / tid)
    assert not declared.exists()


def test_drifted_receipt_of_own_worktree_is_reconciled_by_runtime(env):
    tid, wt = drifted_card(env)
    old_head = git(wt, 'rev-parse', 'HEAD')
    ok, _, reason = kb._validate_worktree_ownership(env.conn, tid, require_checkout=True)
    assert not ok and 'no longer matches creation receipt' in reason
    result = dispatch(env)
    assert [row[0] for row in result.spawned] == [tid], failures(env)
    repaired = events(env, tid, 'nfos_workspace_repaired')
    assert len(repaired) == 1 and repaired[0]['actor'] == 'Runtime'
    task = kb.get_task(env.conn, tid)
    new = Path(task.workspace_path)
    assert new != wt.resolve() and git(new, 'rev-parse', 'HEAD') == old_head
    assert (new / 'file.txt').read_text(encoding='utf-8') == 'uncommitted tracked edit\n'
    assert kb._git_common_dir(new) == kb._git_common_dir(env.source)
    assert kb._validate_worktree_ownership(env.conn, tid, require_checkout=True)[0]
    # The untracked file stays in the old folder, and so does the original branch.
    assert (wt / 'untracked.txt').exists() and not (new / 'untracked.txt').exists()
    assert git(wt, 'branch', '--show-current') == f'nfos/{tid}-focal'


# --- guards: green before and after, they prove the reconciliation has no gap --------------------------------

def test_live_branch_main_is_not_reconciled(env):
    git(env.source, 'branch', '-m', 'trunk', 'elsewhere')
    tid, wt = drifted_card(env, live_branch='main')
    assert not dispatch(env).spawned
    assert_not_reconciled(env, tid)


def test_detached_head_is_not_reconciled(env):
    tid, wt = drifted_card(env, detach=True)
    assert not dispatch(env).spawned
    assert_not_reconciled(env, tid)


def test_folder_of_another_card_is_not_reconciled(env):
    tid, wt = drifted_card(env, folder='t_outro')
    assert not dispatch(env).spawned
    assert_not_reconciled(env, tid)


def test_worktree_of_a_third_repository_is_not_reconciled_by_the_receipt_branch(env):
    third = env.tmp / 'third'
    init_repo(third, 'third repository')
    tid, wt = drifted_card(env, repo=third)
    from hermes_cli import nfos_workspaces
    state = json.loads(delivery.get_workflow(env.conn, tid)['state_json'])
    # Absent before the fix: the guard test stays green on both sides of it.
    reconcile = getattr(nfos_workspaces, '_reconcile_drifted_receipt', None)
    if reconcile is not None:
        assert reconcile(env.conn, kb.get_task(env.conn, tid), state, env.source.resolve(), 'pilot') is None
    assert env.repairs == []
    dispatch(env)
    # Only the pre-existing foreign-repository repair may act; the receipt reconciliation must not.
    assert [r for r in env.repairs if r.startswith(DRIFT_REASON)] == []


def test_tampered_receipt_fingerprint_is_not_reconciled(env):
    tid, wt = drifted_card(env, tamper=True)
    assert not dispatch(env).spawned
    assert_not_reconciled(env, tid)


def test_card_with_a_live_worker_is_not_reconciled(env):
    tid, wt = drifted_card(env)
    env.conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (os.getpid(), tid))
    env.conn.commit()
    dispatch(env)
    assert [r for r in env.repairs if r.startswith(DRIFT_REASON)] == []
    assert events(env, tid, 'nfos_workspace_repaired') == []


def test_forged_git_file_outside_the_worktree_list_is_not_reconciled(env):
    tid = new_card(env)
    other = env.source / '.worktrees' / 't_other'
    git(env.source, 'worktree', 'add', '-b', f'nfos/{tid}-focal', str(other))
    forged = env.source / '.worktrees' / tid
    forged.mkdir()
    admin = env.source / '.git' / 'worktrees' / 't_other'
    (forged / '.git').write_text(f'gitdir: {admin.as_posix()}\n', encoding='utf-8')
    seal_envelope_receipt(env, tid, forged, f'concursa-ai/{tid}-card')
    assert kb._git_current_branch(forged) == f'nfos/{tid}-focal'
    assert kb._git_common_dir(forged) == kb._git_common_dir(env.source)
    assert not dispatch(env).spawned
    assert_not_reconciled(env, tid)


def test_pending_cleanup_is_not_reconciled(env):
    tid, wt = drifted_card(env)
    env.conn.execute("UPDATE task_git_delivery SET cleanup_state='pending' WHERE task_id=?", (tid,))
    env.conn.commit()
    dispatch(env)
    assert [r for r in env.repairs if r.startswith(DRIFT_REASON)] == []
    assert events(env, tid, 'nfos_workspace_repaired') == []


def test_git_work_does_not_invalidate_receipt(env):
    """Documents the refuted hypothesis: commit, cherry-pick and reset never touch the three compared fields."""
    tid = new_card(env, workspace_path=str(env.source))
    assert [row[0] for row in dispatch(env).spawned] == [tid]
    wt = Path(kb.get_task(env.conn, tid).workspace_path)
    check = lambda: kb._validate_worktree_ownership(env.conn, tid, require_checkout=True)[0]  # noqa: E731
    assert check()
    git(env.source, 'branch', 'side')
    git(env.source, 'checkout', 'side')
    (env.source / 'file.txt').write_text('side change\n', encoding='utf-8')
    git(env.source, 'commit', '-am', 'side change')
    side = git(env.source, 'rev-parse', 'HEAD')
    git(env.source, 'checkout', 'trunk')
    (wt / 'file.txt').write_text('worker change\n', encoding='utf-8')
    git(wt, 'commit', '-am', 'worker commit')
    assert check()
    assert kb._cleanup_git(wt, 'cherry-pick', side).returncode != 0
    assert (wt / '.git').exists() and check()
    git(wt, 'cherry-pick', '--abort')
    assert check()
    git(wt, 'reset', '--hard', 'HEAD~1')
    assert check()
