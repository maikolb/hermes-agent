"""A Principal repair pause must release a worker without asking itself again."""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_runtime as runtime
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_principal_acceptance import assessment, task_context
from tests.hermes_cli.test_nfos_workspace_repair import git


@pytest.fixture
def running(task_context, monkeypatch):
    conn, task, _, artifact = task_context
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(180)'], **options)
    kb._set_worker_pid(conn, task.id, process.pid)
    current = kb.get_task(conn, task.id)
    args = dict(expected_run_id=current.current_run_id, expected_claim=current.claim_lock,
                expected_pid=current.worker_pid, expected_started_at=current.worker_started_at,
                actor='Principal', reason='Prepare canonical code workspace for the authorized implementation')
    try:
        yield conn, current, artifact, process, args
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def test_pending_decision_pause_exit_repair_and_resume_same_card(running, monkeypatch):
    conn, task, artifact, process, args = running
    decisions = [tuple(r) for r in conn.execute('SELECT * FROM nfos_decisions')]
    original_spec = dict(d.get_spec(conn, task.id))
    original_artifact = artifact.read_bytes()
    # Reproduce the operator's misleading successful block: it only repeats the question.
    assert kb.block_task(conn, task.id, kind='awaiting_principal', reason=args['reason'], expected_run_id=task.current_run_id)
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    preview = repair.pause_for_repair(conn, task.id, **args)
    assert not preview['apply'] and kb.get_task(conn, task.id).status == 'running'
    result = repair.pause_for_repair(conn, task.id, **args, apply=True)
    assert result['paused'] and result['run_id'] == task.current_run_id
    assert repair.pause_for_repair(conn, task.id, **args, apply=True)['already_paused']
    paused = kb.get_task(conn, task.id)
    assert paused.status == 'blocked' and paused.current_run_id is None
    meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])
    assert meta['nfos_cleanup']['worker_pid'] == process.pid
    assert meta['nfos_cleanup']['worker_started_at'] == args['expected_started_at']
    assert [tuple(r) for r in conn.execute('SELECT * FROM nfos_decisions')] == decisions
    # A competing requeue cannot claim or repair the workspace while the old PID lives.
    assert kb.unblock_task(conn, task.id)
    with kb.connect_closing() as other:
        assert kb.claim_task(other, task.id) is None
        with pytest.raises(d.WorkflowError, match='termination'):
            repair._idle(other, task.id)
    assert process.poll() is None
    real_now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(runtime.time, 'time', lambda: real_now + 30)
        assert task.current_run_id in runtime.reconcile_terminal_workers(conn)
    process.wait(timeout=5)
    assert not runtime.previous_runs_termination_pending(conn, task.id)
    with kb.connect_closing() as other:
        assert kb.claim_task(other, task.id) is None, 'Maintenance must hold until the binding is repaired'
    repo = artifact.parent.parent / 'canonical'; repo.mkdir()
    git(repo, 'init', '-b', 'main'); (repo/'code.txt').write_text('fixture\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-m', 'fixture')
    config_path = artifact.parent.parent/'home/config.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['kanban']['delivery']['projects'] = {'fixture': {'enabled': True, 'profile': 'default', 'repo_path': str(repo)}}
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    wf = d.get_workflow(conn, task.id)
    repair_args = dict(board='fixture', delivery_type='code', expected_delivery_type=task.delivery_type,
                       expected_spec_revision=wf['spec_revision'], expected_instruction_revision=task.instruction_revision,
                       reason=args['reason'], actor='Principal', use_canonical_repo=True)
    repair.repair_card(conn, task.id, **repair_args)
    assert repair.maintenance_pause_pending(conn, task.id)
    with pytest.raises(d.WorkflowError):
        repair.repair_card(conn, task.id, **dict(repair_args, expected_spec_revision=999), apply=True)
    assert repair.maintenance_pause_pending(conn, task.id)
    repair.repair_card(conn, task.id, **repair_args, apply=True)
    resumed = kb.claim_task(conn, task.id)
    assert resumed.id == task.id and resumed.current_run_id != task.current_run_id
    assert resumed.delivery_type == 'code' and resumed.workspace_path == str(repo)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert dict(d.get_spec(conn, task.id)) == original_spec
    assert artifact.read_bytes() == original_artifact
    # A completed pause cannot accidentally release a later maintenance pause.
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    second = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(180)'], **options)
    try:
        kb._set_worker_pid(conn, task.id, second.pid)
        current = kb.get_task(conn, task.id)
        second_args = dict(args, expected_run_id=current.current_run_id, expected_claim=current.claim_lock,
                           expected_pid=current.worker_pid, expected_started_at=current.worker_started_at)
        repair.pause_for_repair(conn, task.id, **second_args, apply=True)
        assert repair.maintenance_pause_pending(conn, task.id)
        assert kb.unblock_task(conn, task.id)
        assert kb.claim_task(conn, task.id) is None
    finally:
        second.terminate(); second.wait(timeout=5)


@pytest.mark.parametrize('field', ['expected_run_id', 'expected_claim', 'expected_pid', 'expected_started_at'])
def test_stale_identity_never_pauses_another_owner(running, field):
    conn, task, _, _, args = running
    args[field] = 'wrong' if field == 'expected_claim' else args[field]+1
    with pytest.raises(d.WorkflowError, match='changed'):
        repair.pause_for_repair(conn, task.id, **args, apply=True)
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


def test_identity_is_rechecked_inside_pause_transaction(running, monkeypatch):
    conn, task, _, _, args = running
    original = kb.write_txn
    @contextmanager
    def change_owner(connection, *a, **kw):
        with original(connection, *a, **kw):
            connection.execute('UPDATE tasks SET worker_started_at=1 WHERE id=?', (task.id,))
            yield
    monkeypatch.setattr(kb, 'write_txn', change_owner)
    with pytest.raises(d.WorkflowError, match='changed'):
        repair.pause_for_repair(conn, task.id, **args, apply=True)
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


@pytest.mark.parametrize('context', ['worker', 'child', 'cron'])
def test_only_principal_maintenance_can_pause(running, monkeypatch, context):
    from agent.delegation_context import non_dispatcher_owned_context
    conn, task, _, _, args = running
    def pause():
        with pytest.raises(d.WorkflowError, match='Principal'):
            repair.pause_for_repair(conn, task.id, **args, apply=True)
    if context == 'worker':
        monkeypatch.setenv('HERMES_KANBAN_TASK', task.id); pause()
    elif context == 'child':
        monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1'); pause()
    else:
        with non_dispatcher_owned_context(): pause()


def test_native_cli_previews_exact_run_without_mutating_it(running, monkeypatch, capsys):
    conn, task, artifact, _, args = running
    request = artifact.parent/'pause.json'; request.write_text(json.dumps(args))
    monkeypatch.setattr(sys, 'argv', ['nfos', 'pause-for-repair', '--task', task.id, '--input', str(request)])
    d.main()
    result = json.loads(capsys.readouterr().out)
    assert result['identity']['run_id'] == task.current_run_id and not result['apply']
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


@pytest.mark.skipif(os.name == 'nt', reason='POSIX orphan-process exit ordering')
@pytest.mark.live_system_guard_bypass  # This test signals only its own recorded orphan after its parent exits.
def test_orphan_child_is_preserved_in_receipt_before_parent_exits(running, monkeypatch):
    from hermes_cli import nfos_tool as tool
    conn, task, artifact, first, args = running
    first.terminate(); first.wait(timeout=5)
    marker = artifact.parent/'child-pid.txt'
    parent = subprocess.Popen([sys.executable, '-c', '''
import subprocess,sys,time,pathlib
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(180)'],start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(180)
''', str(marker)], start_new_session=True)
    child = None
    try:
        deadline = time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
        child = tool._identity(int(marker.read_text()))
        kb._set_worker_pid(conn, task.id, parent.pid)
        current = kb.get_task(conn, task.id)
        args.update(expected_pid=parent.pid, expected_started_at=current.worker_started_at)
        repair.pause_for_repair(conn, task.id, **args, apply=True)
        meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',(task.current_run_id,)).fetchone()[0])
        assert child['pid'] in [p['pid'] for p in json.loads(meta['nfos_cleanup']['descendants_json'])]
        parent.terminate(); parent.wait(timeout=5)
        assert tool._matches(child['pid'], child['started_at'])
        assert kb.unblock_task(conn, task.id)
        with kb.connect_closing() as other:
            assert kb.claim_task(other, task.id) is None
            with pytest.raises(d.WorkflowError, match='termination'): repair._idle(other, task.id)
        now = time.time()
        with monkeypatch.context() as clock:
            clock.setattr(runtime.time, 'time', lambda: now+30)
            assert task.current_run_id in runtime.reconcile_terminal_workers(conn)
        assert not tool._matches(child['pid'], child['started_at'])
    finally:
        if parent.poll() is None: parent.terminate()
        parent.wait(timeout=5)
        if child: tool._signal_identity(child, kill=True)


def _paused_and_exited(conn, task, artifact, process, args, monkeypatch):
    """Paused card whose worker exited and with no open decision: only the pause can hold it."""
    repair.pause_for_repair(conn, task.id, **args, apply=True)
    real_now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(runtime.time, 'time', lambda: real_now + 30)
        runtime.reconcile_terminal_workers(conn)
    process.wait(timeout=5)
    assert not runtime.previous_runs_termination_pending(conn, task.id)
    for (decision_id,) in conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')",
                                       (task.id,)).fetchall():
        d.resolve_decision(conn, decision_id, action='continue', answer='Reviewed', author='Principal',
                           assessment=assessment(artifact))
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')",
                            (task.id,)).fetchone()


def test_sweep_keeps_a_paused_card_blocked_until_its_repair(running, monkeypatch):
    """05/10/2026: the decision sweep returned paused cards to ready, the claim refused them on every
    tick and the dispatcher reported a stuck queue with no reason (DV-0008 held for two days)."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    assert d.sweep_awaiting_principal(conn) == [], 'a pause waits for its repair, not for a decision'
    paused = kb.get_task(conn, task.id)
    assert paused.status == 'blocked' and paused.block_kind == 'awaiting_principal'
    repo = artifact.parent.parent / 'canonical'; repo.mkdir()
    git(repo, 'init', '-b', 'main'); (repo/'code.txt').write_text('fixture\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-m', 'fixture')
    config_path = artifact.parent.parent/'home/config.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['kanban']['delivery']['projects'] = {'fixture': {'enabled': True, 'profile': 'default', 'repo_path': str(repo)}}
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    wf = d.get_workflow(conn, task.id)
    repair.repair_card(conn, task.id, board='fixture', delivery_type='code', expected_delivery_type=task.delivery_type,
                       expected_spec_revision=wf['spec_revision'], expected_instruction_revision=task.instruction_revision,
                       reason=args['reason'], actor='Principal', use_canonical_repo=True, apply=True)
    assert not repair.maintenance_pause_pending(conn, task.id)
    assert d.sweep_awaiting_principal(conn) == [task.id], 'once repaired, the same sweep returns the card to the queue'
    assert kb.claim_task(conn, task.id).id == task.id


def test_dispatcher_names_the_hold_of_a_ready_card(running, monkeypatch, all_assignees_spawnable):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    assert kb.unblock_task(conn, task.id), 'a competing requeue can still make it ready'
    refusal = []
    assert kb.claim_task(conn, task.id, refusal=refusal) is None and refusal == ['maintenance_pause']
    result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: pytest.fail('a held card must not spawn'))
    assert (task.id, 'maintenance_pause') in result.claim_held, 'the tick says why the ready card did not start'
    assert kb.get_task(conn, task.id).status == 'ready'


def test_sweep_restores_the_block_of_a_card_already_ready_under_a_pause(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    assert kb.unblock_task(conn, task.id), 'the state DV-0008 and DV-0009 were in: ready, with the pause pending'
    for _ in range(3):
        d.sweep_awaiting_principal(conn)
    restored = kb.get_task(conn, task.id)
    assert restored.status == 'blocked' and restored.block_kind == 'awaiting_principal'
    kinds = [k for (k,) in conn.execute("SELECT kind FROM task_events WHERE task_id=? AND kind='nfos_maintenance_hold_restored'",
                                        (task.id,))]
    assert kinds == ['nfos_maintenance_hold_restored'], 'one event per restoration, not one per tick'


def _second_pending_pause(conn, task, **pause):
    """Another pause of the same card, on an older closed run (as two maintenance passes leave)."""
    now = time.time()
    pause = pause or {'actor': 'Principal', 'reason': 'links to another ticket', 'at': now - 590}
    conn.execute("INSERT INTO task_runs(task_id, status, outcome, started_at, ended_at, metadata) "
                 "VALUES (?, 'reclaimed', 'reclaimed', ?, ?, ?)",
                 (task.id, int(now) - 600, int(now) - 590, json.dumps({'maintenance_pause': pause})))
    conn.commit()
    return conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (task.id,)).fetchone()[0]


def test_resume_after_repair_closes_only_the_selected_pause(running, monkeypatch):
    """DV-0008 (05/10/2026): the pause waited for the support approval of split requests; the cause was repaired
    outside the card, and repair-card/workspace/execution did not apply."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    run_id = task.current_run_id
    request = dict(pause_run_id=run_id, actor='Principal', reason='Split requests approved and done',
                   evidence=['req_9937bb85 done in t_c9f676b5', 'req_c176d83e done in t_ca2605b5'])
    with pytest.raises(d.WorkflowError, match='evidence'):
        repair.resume_after_repair(conn, task.id, **dict(request, evidence=[]))
    preview = repair.resume_after_repair(conn, task.id, **request)
    assert not preview['resumed'] and preview['pause_reason'] == args['reason'] and repair.maintenance_pause_pending(conn, task.id)
    with pytest.raises(d.WorkflowError, match='sha256'):
        repair.resume_after_repair(conn, task.id, **request, apply=True)
    with pytest.raises(d.WorkflowError, match='changed'):
        repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256='0' * 64, apply=True)
    with monkeypatch.context() as worker:
        worker.setenv('HERMES_KANBAN_TASK', task.id)
        with pytest.raises(d.WorkflowError, match='Principal maintainer'):
            repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
    other = _second_pending_pause(conn, task)
    done = repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
    assert done['resumed'] and not done['already_resumed']
    assert repair.maintenance_pause_pending(conn, task.id), 'the other pause still holds the card'
    assert d.sweep_awaiting_principal(conn) == [] and kb.get_task(conn, task.id).status == 'blocked'
    again = repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
    assert again['already_resumed'], 'repeating the same resume is idempotent'
    with pytest.raises(d.WorkflowError, match='another repair'):
        repair.resume_after_repair(conn, task.id, **dict(request, reason='a different story'),
                                   expected_pause_sha256=preview['pause_sha256'], apply=True)
    resumed = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_maintenance_resumed'", (task.id,))]
    assert len(resumed) == 1 and resumed[0]['evidence'] == request['evidence'] and resumed[0]['pause_run_id'] == run_id
    meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()[0])['maintenance_pause']
    assert meta['repair_kind'] == 'external' and meta['repaired_by'] == 'Principal'
    second = repair.resume_after_repair(conn, task.id, pause_run_id=other, actor='Principal', reason='Links repaired',
                                        evidence=['DV-0016 link fixed'])
    repair.resume_after_repair(conn, task.id, pause_run_id=other, actor='Principal', reason='Links repaired',
                               evidence=['DV-0016 link fixed'], expected_pause_sha256=second['pause_sha256'], apply=True)
    assert not repair.maintenance_pause_pending(conn, task.id)
    assert d.sweep_awaiting_principal(conn) == [task.id] and kb.claim_task(conn, task.id).id == task.id


def test_native_cli_previews_the_resume_without_mutating_it(running, monkeypatch, capsys):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    request = artifact.parent / 'resume.json'
    request.write_text(json.dumps(dict(pause_run_id=task.current_run_id, actor='Principal', reason='Cause repaired',
                                       evidence=['readback of the repaired cause'])))
    monkeypatch.setattr(sys, 'argv', ['nfos', 'resume-after-repair', '--task', task.id, '--input', str(request)])
    d.main()
    result = json.loads(capsys.readouterr().out)
    assert not result['resumed'] and len(result['pause_sha256']) == 64
    assert repair.maintenance_pause_pending(conn, task.id)


def test_resume_after_repair_leaves_budget_pauses_to_repair_execution(running, monkeypatch):
    """Master review of cba2fcf: a cumulative runtime pause must keep its own route, which checks the balance."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    budget = _second_pending_pause(conn, task, kind='runtime_budget_exhausted', actor='runtime', at=time.time(),
                                   reason='Cumulative runtime exhausted; grant-budget then repair-execution before resuming')
    request = dict(pause_run_id=budget, actor='Principal', reason='budget looks fine', evidence=['operator read'])
    for apply in (False, True):
        with pytest.raises(d.WorkflowError, match='repair-execution'):
            repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256='0' * 64, apply=apply)
    meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (budget,)).fetchone()[0])['maintenance_pause']
    assert meta.get('repaired_at') is None and repair.maintenance_pause_pending(conn, task.id)
