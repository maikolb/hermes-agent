"""Closed NFOS runs release their real process trees without touching new work."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_runtime as runtime
from tests.hermes_cli.test_nfos_tool import hidden_kwargs, cleanup_verified_test_processes, eventually


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        yield conn, tmp_path


@pytest.fixture
def worker(board):
    _, directory = board
    receipt = directory / 'test-worker-child.json'
    code = """import subprocess,sys,psutil,json,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])
Path(%r).write_text(json.dumps({'pid':p.pid,'started_at':psutil.Process(p.pid).create_time()}))
time.sleep(90)
""" % str(receipt)
    proc = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_kwargs())
    identities = [{'pid':proc.pid,'started_at':psutil.Process(proc.pid).create_time()}]
    try:
        eventually(receipt.exists)
        identities.append(json.loads(receipt.read_text()))
        yield proc, identities
    finally:
        cleanup_verified_test_processes(identities)
        proc.wait(timeout=10)


def enrolled(conn, pid):
    rid = delivery.receive_request(conn,
        source={'platform':'test','chat_id':'1','thread_id':'2','message_id':'shutdown'},
        text='Audit and preserve the report',
        project={'board':'pilot','profile':'default','delivery_type':'report'})
    claim = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, claim['claim_token'], pid=pid)


def close_done(conn, task):
    # The subject is cleanup after the canonical terminal state, not delivery
    # approval. Populate that state directly without pretending a report passed.
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET status='done',outcome='completed',ended_at=? WHERE id=?",
                     (int(time.time()), task.current_run_id))
        conn.execute("UPDATE tasks SET status='done',current_run_id=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?",
                     (task.id,))


def human_block(conn, task):
    decision = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Choose a business rule', context={'checkpoint':'saved'})
    delivery.resolve_decision(conn, decision, action='human', answer='Choose A or B', author='Principal')
    kb.block_task(conn, task.id, kind='needs_input', reason='Choose A or B')
    return decision


def assert_exited(identities):
    for identity in identities:
        if not psutil.pid_exists(identity['pid']):
            continue
        process = psutil.Process(identity['pid'])
        assert process.status() == psutil.STATUS_ZOMBIE or abs(process.create_time()-identity['started_at']) >= .01


def test_done_run_stops_its_worker_and_children_preserving_files(board, worker):
    conn, directory = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    saved = directory/'saved-work.txt'
    saved.write_text('Uncommitted work and completed report are preserved')
    close_done(conn, task)
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert_exited(identities)
    assert saved.read_text() == 'Uncommitted work and completed report are preserved'
    assert kb.get_task(conn, task.id).status == 'done'
    row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
    receipt = json.loads(row['metadata'])['nfos_cleanup']
    assert receipt['status'] == 'confirmed'
    assert receipt['worker_pid'] == proc.pid and receipt['worker_started_at'] == identities[0]['started_at']
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_worker_exit_confirmed'").fetchone()[0] == 1


def test_human_block_cleans_old_worker_then_applies_the_saved_answer(board, worker):
    conn, _ = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    decision = human_block(conn, task)
    delivery.resume_after_answer(conn, task.id, answer='Use A', source={'platform':'telegram','message_id':'answer'})
    assert kb.get_task(conn, task.id).status == 'blocked'
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert_exited(identities)
    assert kb.get_task(conn, task.id).status == 'ready'
    assert delivery.get_decision(conn, decision)['status'] == 'resolved'


def test_worker_awaiting_principal_and_resolved_continue_remain_alive(board, worker):
    conn, _ = board
    proc, _ = worker
    task = enrolled(conn, proc.pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Resolve locally', context={'checkpoint':'saved'})
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert proc.poll() is None and kb.get_task(conn, task.id).status == 'running'
    delivery.resolve_decision(conn, decision, action='continue', answer='Continue the same task', author='Principal')
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert proc.poll() is None
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_worker_exit_requested'").fetchone()[0] == 0


def test_explicit_grace_is_persisted_and_allows_final_state_flush(board, worker):
    conn, _ = board
    proc, _ = worker
    task = enrolled(conn, proc.pid)
    close_done(conn, task)
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=15)
    assert proc.poll() is None
    receipt = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',
                                     (task.current_run_id,)).fetchone()['metadata'])['nfos_cleanup']
    assert receipt['status'] == 'waiting' and receipt['grace_seconds'] == 15
    assert receipt['not_before'] > time.time()
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert proc.poll() is None, 'A later configuration cannot silently remove the recorded grace'


def test_existing_native_command_can_flush_within_the_recorded_exit_grace(board, worker):
    from hermes_cli import nfos_tool as tool
    conn,directory=board
    proc,_=worker
    task=enrolled(conn,proc.pid)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future=pool.submit(tool.run_command,directory/'kanban.db',task_id=task.id,run_id=task.current_run_id,
            argv=[sys.executable,'-u','-c',"import time;print('saving final evidence',flush=True);time.sleep(2);print('saved',flush=True)"],
            cwd=directory,timeout_seconds=15,call_id='final-flush')
        def started():
            try:
                return conn.execute("SELECT count(*) FROM nfos_tool_chunks WHERE call_id='final-flush'").fetchone()[0]>0
            except Exception:
                return False
        eventually(started)
        close_done(conn,task)
        runtime.reconcile_runtime(conn,worker_exit_grace_seconds=15)
        assert not future.done(), 'A closed run still receives its short final-flush grace'
        assert future.result(timeout=10)['status']=='succeeded'
    assert proc.poll() is None


def test_cleanup_does_not_kill_a_current_run_using_the_same_process(board, worker):
    conn, _ = board
    proc, _ = worker
    task = enrolled(conn, proc.pid)
    close_done(conn, task)
    with kb.write_txn(conn):
        new_run = conn.execute("INSERT INTO task_runs(task_id,status,started_at,worker_pid) VALUES(?,'running',?,?)",
                               (task.id, int(time.time()), proc.pid)).lastrowid
        conn.execute("UPDATE tasks SET status='running',current_run_id=?,worker_pid=?,worker_started_at=? WHERE id=?",
                     (new_run, proc.pid, psutil.Process(proc.pid).create_time(), task.id))
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert proc.poll() is None and kb.get_task(conn, task.id).current_run_id == new_run


def test_cleanup_without_a_start_identity_never_signals_a_live_pid(board, worker):
    conn, _ = board
    proc, _ = worker
    task = enrolled(conn, proc.pid)
    close_done(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='nfos_worker_created_card'",
                     (json.dumps({'pid':proc.pid}), task.id))
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert proc.poll() is None
    receipt = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',
                                     (task.current_run_id,)).fetchone()['metadata'])['nfos_cleanup']
    assert receipt['status'] == 'identity_unavailable'


@pytest.mark.live_system_guard_bypass  # creates and then cleans its own deliberately orphaned call tree
def test_saved_human_answer_waits_for_orphaned_native_adapter_and_command(board):
    from hermes_cli import nfos_tool as tool
    conn, directory = board
    instruction=directory/'launch-native-test-call.json'
    adapter_receipt=directory/'created-native-test-adapter.json'
    launch="""import subprocess,sys,json,time,psutil
from pathlib import Path
p=Path(sys.argv[1])
while not p.exists():time.sleep(.02)
data=json.loads(p.read_text())
child=subprocess.Popen(data['command'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
Path(data['receipt']).write_text(json.dumps({'pid':child.pid,'started_at':psutil.Process(child.pid).create_time()}))
child.wait()
"""
    proc=subprocess.Popen([sys.executable,'-u','-c',launch,str(instruction)],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**hidden_kwargs())
    identities=[{'pid':proc.pid,'started_at':psutil.Process(proc.pid).create_time()}]
    try:
        task=enrolled(conn,proc.pid)
        command=[sys.executable,str(Path(tool.__file__)), '--db',str(directory/'kanban.db'),
            '--task',task.id,'--run',str(task.current_run_id),'--cwd',str(directory),'--timeout','90',
            '--call-id','orphan-human','--',sys.executable,'-u','-c',
            "import time;print('work saved before interruption',flush=True);time.sleep(90)"]
        instruction.write_text(json.dumps({'command':command,'receipt':str(adapter_receipt)}))
        eventually(adapter_receipt.exists)
        launched=json.loads(adapter_receipt.read_text())
        def output_saved():
            try:
                return conn.execute("SELECT count(*) FROM nfos_tool_chunks WHERE call_id='orphan-human'").fetchone()[0]>0
            except Exception:
                return False
        eventually(output_saved)
        identities.extend({'pid':p.pid,'started_at':p.create_time()}
                          for p in psutil.Process(proc.pid).children(recursive=True))
        assert launched in identities
        call=conn.execute("SELECT runner_pid,runner_started_at FROM nfos_tool_calls WHERE id='orphan-human'").fetchone()
        native={'pid':call['runner_pid'],'started_at':call['runner_started_at']}
        assert native in identities
        psutil.Process(native['pid']).suspend()
        decision=human_block(conn,task)
        proc.kill();proc.wait(timeout=10)
        delivery.resume_after_answer(conn,task.id,answer='Use A',source={'platform':'telegram','message_id':'orphan-answer'})
        assert kb.get_task(conn,task.id).status=='blocked'
        assert runtime.run_termination_pending(conn,task.id,task.current_run_id)
        runtime.reconcile_runtime(conn,worker_exit_grace_seconds=0)
        assert_exited(identities)
        assert kb.get_task(conn,task.id).status=='ready'
        assert delivery.get_decision(conn,decision)['status']=='resolved'
        assert conn.execute("SELECT count(*) FROM nfos_tool_chunks WHERE call_id='orphan-human'").fetchone()[0]>0
    finally:
        cleanup_verified_test_processes(identities)
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=10)
