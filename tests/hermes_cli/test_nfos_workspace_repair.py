"""Real Git/SQLite reproduction of retained cards bound to the wrong repository."""
import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli.nfos_runtime import adopt_existing_tasks
from hermes_cli.nfos_workspace_repair import repair_workspace, repair_card


def git(repo, *args):
    result = kb._cleanup_git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                             '-c', 'commit.gpgsign=false', *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def broken(tmp_path, monkeypatch):
    home = tmp_path / 'home';home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    envelope = tmp_path / 'envelope';repo = tmp_path / 'product'
    for root in [envelope, repo]:
        root.mkdir();git(root, 'init', '-b', 'main')
        (root/'source.txt').write_text(root.name+'\n', encoding='utf8')
        git(root, 'add', '.');git(root, 'commit', '-m', root.name)
    project = dict(enabled=True, profile='default', repo_path=str(repo), delivery_type='code')
    (home/'config.yaml').write_text(yaml.safe_dump({'kanban':{'delivery':{'projects':{'pilot':project}}}}), encoding='utf8')
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='Preserve the delivered fix', assignee='default',
                             workspace_kind='worktree', workspace_path=str(envelope),
                             delivery_type='code', initial_status='blocked')
        old, _ = kb._materialize_task_owned_worktree(conn, kb.get_task(conn, tid), repo_root=envelope,
                                                     target=envelope/'.worktrees'/tid, branch=f'wt/{tid}')
        (old/'evidence.json').write_text('{"saved":true}', encoding='utf8')
        adopt_existing_tasks(conn, board='pilot', project=project)
        args = dict(board='pilot', repo_path=str(repo), base_sha=git(repo,'rev-parse','HEAD'),
                    expected_workspace=str(old), expected_source_sha=git(old,'rev-parse','HEAD'),
                    reason='Original card points at the envelope instead of the product', actor='Owner maintenance')
        yield conn, tid, old, repo, args


def test_reproduces_setter_identity_failure_then_repairs_supported_binding(broken):
    conn, tid, old, repo, args = broken
    kb.set_workspace_path(conn, tid, str(repo))
    assert kb._validate_worktree_ownership(conn, tid, require_checkout=True)[0] is False
    kb.set_workspace_path(conn, tid, str(old))
    before = {name:[tuple(x) for x in conn.execute('SELECT * FROM '+name)]
              for name in ['nfos_artifacts','nfos_decisions','nfos_effects','task_comments','task_runs']}
    preview = repair_workspace(conn, tid, **args)
    assert preview['apply'] is False and kb.get_task(conn,tid).workspace_path == str(old)
    result = repair_workspace(conn, tid, **args, apply=True)
    new = Path(result['canonical_worktree']);task = kb.get_task(conn,tid)
    assert new != old and kb._git_common_dir(new) == kb._git_common_dir(repo)
    assert (new/'source.txt').read_text() == 'product\n'
    assert (old/'source.txt').read_text() == 'envelope\n'
    assert (old/'evidence.json').read_text() == '{"saved":true}'
    assert task.status == 'blocked' and task.delivery_type == 'code'
    assert kb._validate_worktree_ownership(conn,tid,require_checkout=True)[0]
    assert conn.execute('SELECT required FROM task_git_delivery WHERE task_id=?',(tid,)).fetchone()[0] == 0
    for name, rows in before.items():assert [tuple(x) for x in conn.execute('SELECT * FROM '+name)] == rows
    assert repair_workspace(conn,tid,**args,apply=True)['already_applied'] is True
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='nfos_workspace_repaired'").fetchone()[0] == 1


def test_repair_preserves_same_repository_index_and_unfinished_files(broken):
    conn, tid, old, repo, args = broken
    # The wrong layout can also be a shared directory within the right repo.
    kb.set_workspace_path(conn,tid,str(repo))
    conn.execute("UPDATE tasks SET workspace_kind='dir',branch_name=NULL WHERE id=?",(tid,));conn.commit()
    (repo/'source.txt').write_text('staged\n');git(repo,'add','source.txt')
    (repo/'source.txt').write_text('unfinished\n');(repo/'report.json').write_text('preserved')
    args.update(expected_workspace=str(repo),expected_source_sha=args['base_sha'])
    result=repair_workspace(conn,tid,**args,apply=True);target=Path(result['canonical_worktree'])
    assert (target/'source.txt').read_text() == 'unfinished\n'
    assert git(target,'show',':source.txt') == 'staged'
    assert (repo/'source.txt').read_text() == 'unfinished\n' and (repo/'report.json').read_text() == 'preserved'


def test_final_transaction_failure_preserves_old_binding_and_reuses_target(broken):
    conn,tid,old,repo,args=broken
    conn.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON task_events WHEN NEW.kind='nfos_workspace_repaired' BEGIN SELECT RAISE(ABORT,'power loss'); END");conn.commit()
    with pytest.raises(sqlite3.DatabaseError,match='power loss'):repair_workspace(conn,tid,**args,apply=True)
    assert kb.get_task(conn,tid).workspace_path == str(old)
    assert kb._validate_worktree_ownership(conn,tid,require_checkout=True)[0]
    plan=json.loads(delivery.get_workflow(conn,tid)['state_json'])['workspace_repair']
    assert 'completed_at' not in plan and Path(plan['canonical_worktree']).is_dir()
    conn.execute('DROP TRIGGER fail_repair');conn.commit()
    result=repair_workspace(conn,tid,**args,apply=True)
    assert result['canonical_worktree']==plan['canonical_worktree']


@pytest.mark.parametrize('fault',['source','commit','destination','live','worker'])
def test_repair_rejects_stale_or_unauthorized_identity(broken,monkeypatch,fault):
    conn,tid,old,repo,args=broken
    if fault=='source':args['expected_workspace']=str(repo)
    if fault=='commit':args['expected_source_sha']='0'*40
    if fault=='destination':args['repo_path']=str(old)
    if fault=='live':
        conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?',(os.getpid(),tid));conn.commit()
    if fault=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    with pytest.raises(delivery.WorkflowError):repair_workspace(conn,tid,**args,apply=True)
    assert kb.get_task(conn,tid).workspace_path==str(old)
    assert not (repo/'.nfos').exists()


def test_an_unrelated_target_cannot_be_adopted_after_interrupted_creation(broken,monkeypatch):
    conn,tid,old,repo,args=broken
    from hermes_cli import nfos_workspace_repair as module
    original=module._git
    def fail(root,*argv,**kwargs):
        if argv[:2]==('worktree','add'):raise RuntimeError('creation interrupted')
        return original(root,*argv,**kwargs)
    monkeypatch.setattr(module,'_git',fail)
    with pytest.raises(RuntimeError,match='creation interrupted'):repair_workspace(conn,tid,**args,apply=True)
    monkeypatch.setattr(module,'_git',original)
    plan=json.loads(delivery.get_workflow(conn,tid)['state_json'])['workspace_repair'];target=Path(plan['canonical_worktree'])
    target.mkdir();(target/'other.txt').write_text('foreign')
    with pytest.raises(delivery.WorkflowError,match='durable creation intent'):repair_workspace(conn,tid,**args,apply=True)
    assert (target/'other.txt').read_text()=='foreign' and kb.get_task(conn,tid).workspace_path==str(old)


def card_args(conn, tid, **extra):
    task=kb.get_task(conn,tid);wf=delivery.get_workflow(conn,tid)
    return dict(board='pilot',expected_delivery_type=task.delivery_type,
                expected_spec_revision=wf['spec_revision'],expected_instruction_revision=task.instruction_revision,
                actor='Owner maintenance',reason='Correct the retained classification using the existing request',**extra)


def test_report_to_code_requires_new_spec_and_keeps_old_spec_immutable(broken):
    conn,tid,old,repo,args=broken
    content=json.dumps(dict(goal='Original request includes implementation',criteria=[dict(id='C1',text='Implement')],steps=['Implement'],delivery_type='report'))
    conn.execute("UPDATE tasks SET delivery_type='report',requires_repo=0 WHERE id=?",(tid,))
    conn.execute("INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,0,'spec',1,?,'Codex','{}',1)",(tid,content))
    conn.execute('UPDATE nfos_workflows SET spec_revision=1 WHERE task_id=?',(tid,));conn.commit()
    before=delivery.get_spec(conn,tid)
    repair_card(conn,tid,**card_args(conn,tid,delivery_type='code',use_canonical_repo=True),apply=True)
    assert delivery.get_spec(conn,tid)==before
    assert kb.get_task(conn,tid).requires_repo and kb.get_task(conn,tid).workspace_path==str(repo)
    assert not delivery._spec_matches_instruction(conn,tid) and not delivery.completion_ready(conn,tid)
    kb.unblock_task(conn,tid);task=kb.claim_task(conn,tid)
    new_spec=json.loads(content);new_spec['delivery_type']='code'
    delivery.save_spec(conn,tid,task.current_run_id,new_spec,author='Codex',evidence={'fallback_reason':'Synthetic unavailable TL in this test'})
    assert delivery.get_spec(conn,tid)['revision']==2 and delivery._spec_matches_instruction(conn,tid)
    assert conn.execute("SELECT content FROM nfos_artifacts WHERE task_id=? AND kind='spec' AND revision=1",(tid,)).fetchone()[0]==content


@pytest.mark.parametrize('missing_path',[False,True])
def test_report_repair_sets_executor_and_managed_directory_without_git(broken,missing_path):
    conn,tid,old,repo,args=broken
    conn.execute("UPDATE tasks SET assignee=NULL,workspace_path=NULL,workspace_kind='scratch' WHERE id=?",(tid,));conn.commit()
    if missing_path:
        kb.set_workspace_path(conn,tid,str(repo/'.worktrees'/'missing'))
    repair_card(conn,tid,**card_args(conn,tid,delivery_type='report'),apply=True)
    task=kb.get_task(conn,tid)
    assert task.assignee=='default' and task.delivery_type=='report' and not task.requires_repo
    assert task.workspace_kind=='scratch' and Path(task.workspace_path).is_dir()
    assert kb._git_toplevel(Path(task.workspace_path)) is None
    assert (old/'evidence.json').exists() and task.status=='blocked'


def test_canonical_repair_retains_dispatch_workspace_lease(broken):
    conn,tid,old,repo,args=broken
    from hermes_cli.nfos_workspaces import isolate_retained_workspace
    repair_card(conn,tid,**card_args(conn,tid,delivery_type='code',use_canonical_repo=True),apply=True)
    kb.unblock_task(conn,tid)
    task,_=isolate_retained_workspace(conn,kb.get_task(conn,tid),board='pilot')
    assert task.workspace_path==str(repo) and task.workspace_kind=='dir'
    lease,_=kb._try_acquire_workspace_lease(repo,task_id='another-writer')
    try:
        result=kb.dispatch_once(conn,board='pilot',max_spawn=1,reconcile_orphans=False,
                               spawn_fn=lambda task,path:pytest.fail('must not start a conflicting writer'))
        assert not result.spawned and result.skipped_workspace_leased
    finally:kb._release_workspace_lease(lease)


def test_reclassification_rejects_stale_spec_or_worker(broken,monkeypatch):
    conn,tid,old,repo,args=broken
    change=card_args(conn,tid,delivery_type='report');change['expected_spec_revision']=99
    with pytest.raises(delivery.WorkflowError,match='changed'):repair_card(conn,tid,**change,apply=True)
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    with pytest.raises(delivery.WorkflowError,match='maintainer'):repair_card(conn,tid,**card_args(conn,tid,delivery_type='report'),apply=True)


def test_adoption_assigns_the_executor_already_saved_in_the_request(broken):
    conn,tid,old,repo,args=broken
    new=kb.create_task(conn,title='Retained unassigned report',assignee=None,workspace_kind='scratch',delivery_type='report',initial_status='blocked')
    conn.execute('UPDATE tasks SET assignee=NULL WHERE id=?',(new,));conn.commit()
    adopt_existing_tasks(conn,board='pilot',project={'profile':'default','repo_path':str(repo)})
    assert kb.get_task(conn,new).assignee=='default' and kb.get_task(conn,new).status=='blocked'
