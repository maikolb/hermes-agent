"""Retained cards gain independent checkouts without erasing earlier work."""
import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli.nfos_runtime import adopt_existing_tasks


def git(repo, *args):
    result = kb._cleanup_git(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def retained(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    monkeypatch.setattr(kb, '_memory_pressure_level', lambda: 'normal')
    repo = tmp_path / 'project'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'source.txt').write_text('base\n', encoding='utf-8')
    git(repo, 'add', 'source.txt')
    git(repo, 'commit', '-m', 'base')
    project = {'enabled': True, 'profile': 'default', 'workers': 2, 'repo_path': str(repo), 'delivery_type': 'report'}
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': {'projects': {'pilot': project}}}}), encoding='utf-8')
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f'Retained {n}', assignee='default', workspace_kind='dir',
                 workspace_path=str(repo), delivery_type='report', requires_repo=False) for n in range(2)]
        adopt_existing_tasks(conn, board='pilot', project=project)
        yield conn, repo, ids


def dispatch(conn):
    result = kb.dispatch_once(conn, board='pilot', max_spawn=2, reconcile_orphans=False,
                             spawn_fn=lambda task, workspace: os.getpid())
    if not result.spawned:
        print([dict(r) for r in conn.execute("SELECT kind,payload FROM task_events WHERE kind IN ('spawn_failed','gave_up','nfos_workspace_restore_failed')")])
    return result


def test_two_retained_cards_dispatch_into_distinct_checkouts(retained):
    conn, repo, ids = retained
    original = git(repo, 'rev-parse', 'HEAD')
    result = dispatch(conn)
    assert len(result.spawned) == 2, result.skipped_workspace_leased
    paths = [Path(kb.get_task(conn, tid).workspace_path) for tid in ids]
    assert len(set(paths)) == 2 and repo not in paths
    for tid, path in zip(ids, paths):
        assert kb.get_task(conn, tid).workspace_kind == 'worktree'
        assert git(path, 'rev-parse', 'HEAD') == original
        assert delivery.get_spec(conn, tid) is None
    (paths[0] / 'source.txt').write_text('worker one\n', encoding='utf-8')
    assert (paths[1] / 'source.txt').read_text() == 'base\n'
    assert (repo / 'source.txt').read_text() == 'base\n'


def test_retained_dirty_files_and_index_survive_isolation(retained):
    conn, repo, ids = retained
    (repo / 'source.txt').write_text('staged\n', encoding='utf-8')
    git(repo, 'add', 'source.txt')
    (repo / 'source.txt').write_text('unfinished\n', encoding='utf-8')
    (repo / 'saved-report.md').write_text('earlier evidence', encoding='utf-8')
    result = dispatch(conn)
    assert len(result.spawned) == 2
    for tid in ids:
        path = Path(kb.get_task(conn, tid).workspace_path)
        assert (path / 'source.txt').read_text() == 'unfinished\n'
        assert git(path, 'show', ':source.txt') == 'staged'
        state = json.loads(delivery.get_workflow(conn, tid)['state_json'])
        assert state['retained_workspace']['source'] == str(repo.resolve())
    assert (repo / 'saved-report.md').read_text() == 'earlier evidence'
    assert (repo / 'source.txt').read_text() == 'unfinished\n'
    assert git(repo, 'show', ':source.txt') == 'staged'


def test_live_source_writer_is_not_interrupted_or_copied(retained):
    conn, repo, ids = retained
    lease, _ = kb._try_acquire_workspace_lease(repo, task_id='existing-writer')
    assert lease
    try:
        result = dispatch(conn)
        assert result.spawned == []
        assert {row[0] for row in result.skipped_workspace_leased} == set(ids)
        assert all(kb.get_task(conn, tid).workspace_path == str(repo) for tid in ids)
    finally:
        kb._release_workspace_lease(lease)
    assert len(dispatch(conn).spawned) == 2


def test_power_loss_before_workspace_handoff_replays_saved_snapshot(retained):
    conn, repo, ids = retained
    (repo / 'source.txt').write_text('saved before crash\n', encoding='utf-8')
    conn.execute("CREATE TRIGGER fail_handoff BEFORE INSERT ON task_events WHEN NEW.kind='nfos_workspace_isolated' BEGIN SELECT RAISE(ABORT,'handoff power loss'); END")
    conn.commit()
    dispatch(conn)
    assert all(kb.claim_task(conn, tid) is None for tid in ids)
    assert all(not json.loads(delivery.get_workflow(conn, tid)['state_json'])['retained_workspace'].get('restored_at') for tid in ids)
    assert len(delivery.pending_decisions(conn)) == 2
    dispatch(conn)
    assert len(delivery.pending_decisions(conn)) == 2
    assert conn.execute('SELECT count(*) FROM tasks WHERE status=\'running\'').fetchone()[0] == 0
    conn.execute('DROP TRIGGER fail_handoff')
    conn.commit()
    (repo / 'source.txt').write_text('later unrelated edit\n', encoding='utf-8')
    result = dispatch(conn)
    assert len(result.spawned) == 2
    assert delivery.pending_decisions(conn) == []
    for tid in ids:
        path = Path(kb.get_task(conn, tid).workspace_path)
        assert (path / 'source.txt').read_text() == 'saved before crash\n'
    assert (repo / 'source.txt').read_text() == 'later unrelated edit\n'


def test_old_foreign_worktree_is_preserved_while_retained_card_gets_its_own(retained):
    conn, repo, ids = retained
    foreign = repo / '.worktrees' / ids[0]
    git(repo, 'worktree', 'add', '-b', f'wt/{ids[0]}', str(foreign))
    (foreign / 'source.txt').write_text('foreign unfinished work\n', encoding='utf-8')
    assert len(dispatch(conn).spawned) == 2
    assert Path(kb.get_task(conn, ids[0]).workspace_path) != foreign
    assert (foreign / 'source.txt').read_text() == 'foreign unfinished work\n'


def test_binary_and_deleted_tracked_files_are_restored(retained):
    conn, repo, ids = retained
    (repo / 'binary.dat').write_bytes(b'\x00before\xff')
    git(repo, 'add', 'binary.dat')
    git(repo, 'commit', '-m', 'binary baseline')
    (repo / 'binary.dat').write_bytes(b'\x00unfinished\xfe')
    (repo / 'source.txt').unlink()
    assert len(dispatch(conn).spawned) == 2
    for tid in ids:
        path = Path(kb.get_task(conn, tid).workspace_path)
        assert (path / 'binary.dat').read_bytes() == b'\x00unfinished\xfe'
        assert not (path / 'source.txt').exists()
    assert (repo / 'binary.dat').read_bytes() == b'\x00unfinished\xfe'


@pytest.mark.parametrize('interrupted',[False,True])
@pytest.mark.parametrize('previously_isolated',[False,True])
def test_dispatch_repairs_foreign_repository_before_claim_without_losing_history(retained,interrupted,previously_isolated):
    conn,repo,ids=retained
    foreign=repo.parent/'old-envelope';foreign.mkdir()
    git(foreign,'init');git(foreign,'config','user.email','fixture@example.invalid')
    git(foreign,'config','user.name','Fixture')
    (foreign/'old.txt').write_text('old repository\n');git(foreign,'add','old.txt');git(foreign,'commit','-m','old')
    (foreign/'old.txt').write_text('unfinished unrelated edit\n')
    (foreign/'proof.json').write_text('{"preserved":true}')
    conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?',(str(foreign),ids[0]));conn.commit()
    if previously_isolated:
        state=json.loads(delivery.get_workflow(conn,ids[0])['state_json'])
        state['retained_workspace']={'restored_at':1,'source':str(foreign)}
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',(json.dumps(state),ids[0]));conn.commit()
    history={table:[tuple(x) for x in conn.execute('SELECT * FROM '+table+' WHERE task_id=?',(ids[0],))] for table in ['task_comments','nfos_artifacts','nfos_decisions']}
    if interrupted:
        conn.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON task_events WHEN NEW.kind='nfos_workspace_repaired' BEGIN SELECT RAISE(ABORT,'repair interrupted'); END");conn.commit()
        dispatch(conn)
        task=kb.get_task(conn,ids[0]);assert task.status=='ready'
        plan=json.loads(delivery.get_workflow(conn,ids[0])['state_json'])['workspace_repair']
        assert not plan.get('completed_at')
        conn.execute('DROP TRIGGER fail_repair');conn.commit()
    dispatch(conn)
    task=kb.get_task(conn,ids[0]);assert task.status=='running'
    assert kb._git_common_dir(task.workspace_path)==kb._git_common_dir(repo)
    assert Path(task.workspace_path,'source.txt').read_text()=='base\n'
    assert not Path(task.workspace_path,'old.txt').exists()
    assert (foreign/'old.txt').read_text()=='unfinished unrelated edit\n'
    assert (foreign/'proof.json').read_text()=='{"preserved":true}'
    saved=json.loads(delivery.get_workflow(conn,ids[0])['state_json'])['workspace_repair']
    assert saved['artifacts_location']==str(foreign.resolve()) and saved['completed_at']
    if interrupted:assert saved['canonical_worktree']==plan['canonical_worktree']
    assert history=={table:[tuple(x) for x in conn.execute('SELECT * FROM '+table+' WHERE task_id=?',(ids[0],))] for table in history}
