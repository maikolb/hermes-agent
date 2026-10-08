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


def test_dispatcher_ticks_keep_a_paused_card_blocked_without_repeating_events(running, monkeypatch, all_assignees_spawnable):
    """05-06/10/2026: the sweep restored the block and recompute_ready promoted the card back in the same tick,
    one pair of events per tick. A paused card now stays blocked across ticks with a single restoration."""
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    assert kb.recompute_ready(conn) == 0 and kb.get_task(conn, task.id).status == 'blocked', 'a pause is not promoted'
    assert kb.unblock_task(conn, task.id), 'the state DV-0008 and DV-0009 were left in: ready, with the pause pending'
    refusal = []
    assert kb.claim_task(conn, task.id, refusal=refusal) is None and refusal == ['maintenance_pause']
    for _ in range(3):
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: pytest.fail('a held card must not spawn'))
        assert kb.get_task(conn, task.id).status == 'blocked'
    kinds = [k for (k,) in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND kind IN ('nfos_maintenance_hold_restored','promoted') "
        "AND id > (SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='unblocked') ORDER BY id", (task.id, task.id))]
    assert kinds == ['nfos_maintenance_hold_restored'], 'one restoration, no promotion back, nothing per tick'



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


def test_pause_exposes_its_reason_in_native_card_presentation(running):
    conn, task, _, _, args = running
    repair.pause_for_repair(conn, task.id, **args, apply=True)
    assert kb.task_presentation(conn, kb.get_task(conn, task.id))['block_reason'] == args['reason']


def test_principal_cannot_end_with_explanation_before_actual_repair(running, monkeypatch):
    from agent.kanban_stop import build_kanban_stop_nudge
    from gateway.wake import current_notify_receipt
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    d.sweep_awaiting_principal(conn)
    row = conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchone()
    d.resolve_decision(conn, row['id'], action='changes', author='Principal',
                       answer='The broker returned 75 busy. Inspect its tools after the lock is released.')
    token = current_notify_receipt.set({'db_path': conn.execute('PRAGMA database_list').fetchone()[2],
        'principal_task_id': task.id, 'delivery_id': 'internal-notifier-fixture'})
    try:
        # Even after two narrated stops, stay in this same bounded agent turn.
        assert build_kanban_stop_nudge(messages=[], attempts=3) is not None
        assert d.get_decision(conn, row['id'])['status'] == 'pending'
        request = dict(pause_run_id=task.current_run_id, actor='Principal', reason='Environment prepared',
                       evidence=['real fixture preparation receipt'])
        preview = repair.resume_after_repair(conn, task.id, **request)
        repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
        d.sweep_awaiting_principal(conn)
        assert build_kanban_stop_nudge(messages=[], attempts=4) is None
        assert kb.get_task(conn, task.id).status == 'ready'
    finally:
        current_notify_receipt.reset(token)


def test_principal_waits_for_real_human_dependency_but_handles_new_owner_guidance(running,monkeypatch):
    from agent.kanban_stop import build_kanban_stop_nudge
    from gateway.wake import current_notify_receipt
    conn,task,artifact,process,args=running
    _paused_and_exited(conn,task,artifact,process,args,monkeypatch)
    d.sweep_awaiting_principal(conn)
    did=conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending'",(task.id,)).fetchone()[0]
    d.resolve_decision(conn,did,action='human',answer='Which access is authorized for this isolated fixture?',author='Principal')
    token=current_notify_receipt.set({'db_path':conn.execute('PRAGMA database_list').fetchone()[2],
        'principal_task_id':task.id,'delivery_id':'fixture'})
    try:
        assert build_kanban_stop_nudge(messages=[]) is None
        d.receive_owner_guidance(conn,task.id,text='Use the existing authorized fixture route.',
            source={'platform':'vigilia','actor':'Maikol','message_id':'fixture-access'})
        assert build_kanban_stop_nudge(messages=[]) is not None
    finally:
        current_notify_receipt.reset(token)


def test_sweep_owns_recovery_until_native_repair_then_resumes(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    before_runs = [tuple(r) for r in conn.execute('SELECT * FROM task_runs')]
    original_spec = dict(d.get_spec(conn, task.id))
    assert d.sweep_awaiting_principal(conn) == []
    row = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND "
                       "json_extract(context,'$.maintenance_recovery.pause_run_id')=?",
                       (task.id, task.current_run_id)).fetchone()
    assert row is not None, 'A retained pause needs an executable Principal recovery obligation'
    did = row['id']
    assert row['status'] == 'pending'
    context = json.loads(row['context'])['maintenance_recovery']
    assert context['reason'] == args['reason']
    assert context['resume_route'] == 'resume-after-repair'
    assert context['budget_mode'] == 'per_run'
    assert args['reason'] in d.get_workflow(conn, task.id)['next_action']
    # Explaining a pause does not execute its repair or discharge the obligation.
    d.resolve_decision(conn, did, action='changes', answer='The environment needs preparation; no receipt yet', author='Principal')
    assert d.get_decision(conn, did)['status'] == 'pending'
    last_event = conn.execute('SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (task.id,)).fetchone()[0]
    assert last_event == 'nfos_maintenance_recovery_deferred'
    event_count = conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
    d.sweep_awaiting_principal(conn)
    d.nudge_open_decisions(conn, task.id)
    assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0] == event_count
    assert [tuple(r) for r in conn.execute('SELECT * FROM task_runs')] == before_runs
    assert dict(d.get_spec(conn, task.id)) == original_spec
    # A blocked card is absent from dispatch's ready/review loops: the runtime tick must own reminders.
    now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(d.time, 'time', lambda: now + d.DECISION_REMINDER_GAP + 1)
        runtime.reconcile_runtime(conn)
        runtime.reconcile_runtime(conn)
    wakes = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'",
                         (task.id,)).fetchall()
    reminders = [json.loads(r[0]) for r in wakes if json.loads(r[0]).get('decision_id') == did
                 and json.loads(r[0]).get('reminder')]
    assert len(reminders) == 1, 'One due wake across repeated runtime ticks, not an orphan or a busy loop'
    request = dict(pause_run_id=task.current_run_id, actor='Principal', reason='Environment prepared',
                   evidence=['verified preparation receipt'])
    preview = repair.resume_after_repair(conn, task.id, **request)
    repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
    assert d.sweep_awaiting_principal(conn) == [task.id]
    assert d.get_decision(conn, did)['status'] == 'resolved'
    assert not repair.maintenance_pause_pending(conn, task.id)
    assert kb.claim_task(conn, task.id).id == task.id


def test_legacy_pause_recovers_reason_without_releasing_the_pause(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    # The deployed pause path recorded only its private event, invisible to consumers.
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='blocked'", (task.id,))
    d.sweep_awaiting_principal(conn)
    assert kb.task_presentation(conn, kb.get_task(conn, task.id))['block_reason'] == args['reason']
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert repair.maintenance_pause_pending(conn, task.id)


def test_sweep_preserves_a_genuine_human_input_hold(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (task.id,))
        conn.execute("UPDATE nfos_workflows SET next_action='Waiting for the named destination operator' WHERE task_id=?", (task.id,))
    decisions = [tuple(r) for r in conn.execute('SELECT * FROM nfos_decisions')]
    assert d.sweep_awaiting_principal(conn) == []
    assert [tuple(r) for r in conn.execute('SELECT * FROM nfos_decisions')] == decisions
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert d.get_workflow(conn, task.id)['next_action'] == 'Waiting for the named destination operator'


def test_reconsidered_recovery_closes_the_effective_obligation(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    d.sweep_awaiting_principal(conn)
    original = conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND "
                           "json_extract(context,'$.maintenance_recovery.pause_run_id')=?",
                           (task.id, task.current_run_id)).fetchone()[0]
    d.resolve_decision(conn, original, action='human', answer='PERGUNTA para Operador: qual o endereço do destino?', author='Principal')
    replacement = d.reconsider_decision(conn, original, action='continue', reason='Existing destination access was restored',
                                      answer='The prerequisite is available; execute the remaining maintenance')
    d.sweep_awaiting_principal(conn)
    assert d.get_decision(conn, original)['status'] == 'superseded'
    assert d.get_decision(conn, replacement)['status'] == 'pending'
    request = dict(pause_run_id=task.current_run_id, actor='Principal', reason='Cause repaired', evidence=['verified repair receipt'])
    preview = repair.resume_after_repair(conn, task.id, **request)
    repair.resume_after_repair(conn, task.id, **request, expected_pause_sha256=preview['pause_sha256'], apply=True)
    assert d.sweep_awaiting_principal(conn) == [task.id]
    assert d.get_decision(conn, original)['status'] == 'superseded'
    assert d.get_decision(conn, replacement)['status'] == 'resolved'
    assert kb.claim_task(conn, task.id).id == task.id
