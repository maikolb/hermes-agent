"""Closed-run budget recovery uses the real claim hold and native resume selection."""
import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_principal_review as review
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_principal_acceptance import task_context  # noqa: F401
from tests.hermes_cli.test_nfos_escalation_budget import resumed, exhausted_idle  # noqa: F401


def test_escalated_timeout_stays_held_even_after_unblock(resumed, monkeypatch):
    conn, task, _ = resumed
    monkeypatch.setattr(kb.time, 'time', lambda: 1041)
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: None) == [task.id]
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert repair.maintenance_pause_pending(conn, task.id)
    assert kb.unblock_task(conn, task.id)
    assert kb.claim_task(conn, task.id) is None


@pytest.fixture
def paused(exhausted_idle):
    conn, task, grant = exhausted_idle
    cfg_path = Path(os.environ['HERMES_HOME']) / 'config.yaml'
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg['agent'] = {'max_turns': 400}
    cfg_path.write_text(yaml.safe_dump(cfg))
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        metadata = json.loads(row['metadata'] or '{}')
        metadata['maintenance_pause'] = {'kind': 'runtime_budget_exhausted', 'actor': 'runtime'}
        conn.execute("UPDATE task_runs SET metadata=?,outcome='reclaimed' WHERE id=?", (json.dumps(metadata), task.current_run_id))
    args = dict(board=None, expected_run_id=task.current_run_id,
                expected_instruction_revision=task.instruction_revision,
                expected_spec_revision=review.d.get_workflow(conn, task.id)['spec_revision'],
                expected_resume_session=None, actor='Principal', reason='Authorized bounded execution recovery')
    return conn, task, grant, args


def test_repair_requires_budget_and_releases_only_after_verified_apply(paused):
    conn, task, grant, args = paused
    with pytest.raises(review.d.WorkflowError, match='exhausted'):
        repair.repair_execution(conn, task.id, **args)
    review.grant_iteration_budget(conn, task.id, **grant)
    assert repair.maintenance_pause_pending(conn, task.id)
    preview = repair.repair_execution(conn, task.id, **args)
    assert preview['remaining_iterations'] == 200
    assert preview['remaining_runtime_seconds'] == 3600
    assert preview['resume_session'] is None and not preview['apply']
    assert repair.maintenance_pause_pending(conn, task.id)
    actual = repair.repair_execution(conn, task.id, **args, apply=True)
    assert actual['status'] == 'ready'
    assert not repair.maintenance_pause_pending(conn, task.id)
    assert kb.claim_task(conn, task.id) is not None


def test_unblock_cannot_spawn_exhausted_lineage_without_a_pause(paused):
    conn, task, grant, _ = paused
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (task.current_run_id,))
        conn.execute('UPDATE tasks SET max_runtime_seconds=30000 WHERE id=?', (task.id,))
    assert kb.unblock_task(conn, task.id)
    count = conn.execute('SELECT count(*) FROM task_runs').fetchone()[0]
    assert kb.claim_task(conn, task.id) is None
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == count
    assert kb.get_task(conn, task.id).worker_pid is None
    review.grant_iteration_budget(conn, task.id, **dict(grant, runtime_seconds=0))
    assert kb.unblock_task(conn, task.id)
    assert kb.claim_task(conn, task.id) is not None


@pytest.mark.parametrize('mode', ['unselected', 'bound_session', 'multiple', 'valid'])
def test_generic_pause_requires_selected_unbound_reclaimed_attempt(paused, mode):
    conn, task, grant, args = paused
    review.grant_iteration_budget(conn, task.id, **grant)
    with kb.write_txn(conn):
        meta = {'maintenance_pause': {'actor': 'Principal', 'reason': 'Execution investigation'}}
        if mode == 'bound_session': meta['worker_session_id'] = 'do-not-delete-this-session'
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(meta), task.current_run_id))
        if mode == 'multiple':
            first = review.worker_escalation(conn, task.id)['first_run_id']
            row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (first,)).fetchone()
            prior = json.loads(row['metadata']); prior['maintenance_pause'] = {'actor': 'Principal'}
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(prior), first))
    if mode != 'unselected': args['pause_run_id'] = task.current_run_id
    before = [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE id<?', (task.current_run_id,))]
    if mode == 'valid':
        result = repair.repair_execution(conn, task.id, **args, apply=True)
        assert result['resume_kind'] == 'native fresh context after unbound reclaimed attempt'
        assert before == [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE id<?', (task.current_run_id,))]
    else:
        with pytest.raises(review.d.WorkflowError): repair.repair_execution(conn, task.id, **args, apply=True)
        assert repair.maintenance_pause_pending(conn, task.id)


@pytest.mark.parametrize('mode', ['worker', 'child', 'stale_run', 'stale_spec', 'stale_instruction', 'cleanup', 'resume'])
def test_execution_repair_rejects_unsafe_context(paused, monkeypatch, mode):
    conn, task, grant, args = paused
    review.grant_iteration_budget(conn, task.id, **grant)
    if mode == 'worker': monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    if mode == 'child': monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1')
    if mode == 'stale_run': args['expected_run_id'] += 1
    if mode == 'stale_spec': args['expected_spec_revision'] += 1
    if mode == 'stale_instruction': args['expected_instruction_revision'] += 1
    if mode == 'cleanup': monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *a: True)
    if mode == 'resume': args['expected_resume_session'] = 'unexpected-session'
    with pytest.raises(review.d.WorkflowError): repair.repair_execution(conn, task.id, **args, apply=True)
    assert repair.maintenance_pause_pending(conn, task.id)
    assert kb.get_task(conn, task.id).status == 'blocked'


@pytest.mark.parametrize('nfos', [False, True])
def test_per_attempt_timeout_without_escalation_retains_native_retry(task_context, monkeypatch, nfos):
    conn, task, _, _ = task_context
    with kb.write_txn(conn):
        if not nfos: conn.execute('DELETE FROM nfos_workflows WHERE task_id=?', (task.id,))
        conn.execute('UPDATE tasks SET max_runtime_seconds=100 WHERE id=?', (task.id,))
        conn.execute('UPDATE task_runs SET started_at=1000 WHERE id=?', (task.current_run_id,))
    monkeypatch.setattr(kb.time, 'time', lambda: 1101)
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *a: None) == [task.id]
    assert kb.get_task(conn, task.id).status == 'ready'
    assert not repair.maintenance_pause_pending(conn, task.id)


def test_repair_rechecks_config_inside_transaction(paused, monkeypatch):
    from contextlib import contextmanager
    conn, task, grant, args = paused
    review.grant_iteration_budget(conn, task.id, **grant)
    original = kb.write_txn
    @contextmanager
    def changed(conn, **kwargs):
        p = Path(os.environ['HERMES_HOME']) / 'config.yaml'
        cfg = yaml.safe_load(p.read_text()); cfg['agent']['max_turns'] = 100
        p.write_text(yaml.safe_dump(cfg))
        with original(conn, **kwargs): yield
    monkeypatch.setattr(kb, 'write_txn', changed)
    with pytest.raises(review.d.WorkflowError): repair.repair_execution(conn, task.id, **args, apply=True)
    assert repair.maintenance_pause_pending(conn, task.id)


def test_cli_repair_execution_exposes_dry_run_and_preserves_bound_session(paused, monkeypatch, tmp_path, capsys):
    import sqlite3
    import sys
    conn, task, grant, args = paused
    review.grant_iteration_budget(conn, task.id, **grant)
    sid = 'existing-worker-session'
    with sqlite3.connect(Path(os.environ['HERMES_HOME']) / 'state.db') as state:
        state.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
        state.execute('INSERT INTO sessions VALUES (?)', (sid,))
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        meta = json.loads(row['metadata']); meta['worker_session_id'] = sid
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(meta), task.current_run_id))
    args.pop('board'); args['expected_resume_session'] = sid
    path = tmp_path / 'repair.json'; path.write_text(json.dumps(args))
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'repair-execution', '--task', task.id, '--input', str(path)])
    review.d.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['resume_session'] == sid and receipt['apply'] is False
    assert repair.maintenance_pause_pending(conn, task.id)
    repair.repair_execution(conn, task.id, board=None, **args, apply=True)
    meta = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])
    assert meta['worker_session_id'] == sid
