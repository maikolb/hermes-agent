"""A replacement cannot acquire a card while its earlier execution still lives."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_runtime as runtime, nfos_delivery as delivery
from hermes_cli import nfos_tool as tool
from tests.hermes_cli.test_nfos_worker_shutdown import board, worker, enrolled, assert_exited
from tests.hermes_cli.test_nfos_tool import hidden_kwargs, cleanup_verified_test_processes, eventually


def released_for_retry(conn, task):
    # This is the canonical shape after an interrupted run is reclaimed.
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET status='crashed',outcome='crashed',ended_at=? WHERE id=?",
                     (int(time.time()), task.current_run_id))
        conn.execute("UPDATE tasks SET status='ready',current_run_id=NULL,worker_pid=NULL,worker_started_at=NULL,claim_lock=NULL,claim_expires=NULL WHERE id=?",
                     (task.id,))


def test_clean_nfos_and_legacy_cards_remain_claimable(board):
    conn, _ = board
    for name in ('clean-nfos','legacy'):
        tid = kb.create_task(conn,title=name,assignee='default',delivery_type='report',requires_repo=False)
        if name=='clean-nfos':
            request = delivery.receive_request(conn,
                source={'platform':'test','chat_id':'1','thread_id':'2','message_id':'clean'},
                text='Clean NFOS card', project={'board':'pilot','profile':'default','delivery_type':'report'})
            with kb.write_txn(conn):
                conn.execute("INSERT INTO nfos_workflows(task_id,request_id,stage,updated_at) VALUES(?,?,'intake',?)",
                             (tid,request,int(time.time())))
        assert not runtime.previous_runs_termination_pending(conn,tid)
        claimed = kb.claim_task(conn,tid)
        assert claimed and claimed.status=='running'


def test_atomic_claim_waits_for_the_previous_run_even_when_pointer_is_cleared(board, worker):
    conn, _ = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    released_for_retry(conn, task)
    assert kb.claim_task(conn, task.id) is None
    assert kb.get_task(conn, task.id).status == 'ready'
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 1
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert_exited(identities)
    replacement = kb.claim_task(conn, task.id)
    assert replacement and replacement.current_run_id != task.current_run_id


def test_unconfirmed_termination_survives_repeated_ticks_and_only_then_allows_claim(board, worker, monkeypatch):
    conn, _ = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    released_for_retry(conn, task)
    original = tool._stop_tree
    with monkeypatch.context() as partial:
        partial.setattr(tool, '_stop_tree', lambda row, **kw: list(identities))
        for _ in range(2):
            runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
            assert runtime.run_termination_pending(conn, task.id, task.current_run_id)
            assert kb.claim_task(conn, task.id) is None
            assert proc.poll() is None
    assert tool._stop_tree is original
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert_exited(identities)
    assert kb.claim_task(conn, task.id) is not None


@pytest.mark.live_system_guard_bypass  # signal only recorded fixture descendants after deliberately orphaning them
def test_dispatch_after_worker_death_stops_hung_native_call_before_replacement(board, monkeypatch):
    conn, directory = board
    instruction = directory/'native-command.json'
    receipt = directory/'native-runner.json'
    code = """import json,subprocess,sys,time,psutil
from pathlib import Path
p=Path(sys.argv[1])
while not p.exists():time.sleep(.02)
data=json.loads(p.read_text())
child=subprocess.Popen(data['command'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
Path(data['receipt']).write_text(json.dumps({'pid':child.pid,'started_at':psutil.Process(child.pid).create_time()}))
child.wait()
"""
    proc = subprocess.Popen([sys.executable,'-u','-c',code,str(instruction)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_kwargs())
    identities = [{'pid':proc.pid,'started_at':psutil.Process(proc.pid).create_time()}]
    try:
        task = enrolled(conn, proc.pid)
        kb.set_workspace_path(conn, task.id, str(directory/'workspace'))
        command = [sys.executable,str(Path(tool.__file__)),'--db',str(directory/'kanban.db'),'--task',task.id,
                   '--run',str(task.current_run_id),'--cwd',str(directory),'--timeout','90','--call-id','recovery-call',
                   '--',sys.executable,'-u','-c',"import time;print('saved before death',flush=True);time.sleep(90)"]
        instruction.write_text(json.dumps({'command':command,'receipt':str(receipt)}))
        eventually(receipt.exists)
        def output_saved():
            try:
                return conn.execute("SELECT count(*) FROM nfos_tool_chunks WHERE call_id='recovery-call'").fetchone()[0]>0
            except Exception:
                return False
        eventually(output_saved)
        identities.extend({'pid':p.pid,'started_at':p.create_time()}
                          for p in psutil.Process(proc.pid).children(recursive=True))
        assert json.loads(receipt.read_text()) in identities
        call = tool.get_call(conn,'recovery-call')
        # Windows' Python launcher may own the interpreter that records the
        # call. Signal that actual recorded descendant, never guess the PID.
        runner = {'pid':call['runner_pid'],'started_at':call['runner_started_at']}
        assert runner in identities
        psutil.Process(runner['pid']).suspend()
        proc.kill(); proc.wait(timeout=10)
        monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS','0')
        monkeypatch.setattr(kb, '_memory_pressure_level', lambda:'normal')
        monkeypatch.setattr(kb, 'check_respawn_guard', lambda *a, **kw:None)
        monkeypatch.setattr(kb, '_respawn_guard_backoff_remaining', lambda *a, **kw:0)
        monkeypatch.setattr(kb, 'resolve_workspace', lambda *a, **kw:directory)
        from hermes_cli import profiles
        monkeypatch.setattr(profiles,'profile_exists',lambda name:True)
        at_spawn = []
        def spawn(replacement, workspace, **unused):
            old = tool.get_call(conn,'recovery-call')
            assert_exited(identities)
            cleanup = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',
                                 (task.current_run_id,)).fetchone()['metadata'])['nfos_cleanup']
            at_spawn.append({'run':replacement.current_run_id,
                'cleanup_confirmed':cleanup['status']=='confirmed',
                'old_runner_alive':tool.call_runner_alive(old),
                'old_command_alive':tool._matches(old['worker_pid'],old['worker_started_at'])})
            return None
        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2, board='pilot')
        assert result.spawned and len(at_spawn)==1
        assert at_spawn[0]['run'] != task.current_run_id
        assert at_spawn[0]['cleanup_confirmed']
        assert not at_spawn[0]['old_runner_alive'] and not at_spawn[0]['old_command_alive']
        assert_exited(identities)
        assert tool.get_call(conn,'recovery-call')['status']=='interrupted'
        assert b'saved before death' in b''.join(row['content'] for row in tool.read_chunks(conn,'recovery-call'))
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0]==2
    finally:
        cleanup_verified_test_processes(identities)
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=10)
