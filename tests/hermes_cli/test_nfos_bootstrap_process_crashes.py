"""S05 contract with real process death, concurrent SQLite writers and restarts.

No model or Telegram API is called: the production intake/bootstrap functions
run in isolated child processes, including the exact outer commit boundary.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def _trace_stage(stage):
    if __name__ != '__main__' or len(sys.argv)<3 or sys.argv[1]!='--child':
        return
    data=json.loads(sys.argv[2])
    path=Path(data['marker']).with_suffix('.stages.jsonl')
    with path.open('a') as stream:
        stream.write(json.dumps({'stage':stage,'monotonic':time.monotonic(),'pid':os.getpid()})+'\n')


_trace_stage('python_entered')
if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--child':
    import faulthandler
    faulthandler.dump_traceback_later(8,repeat=True)
import pytest
_trace_stage('pytest_imported')
import psutil
_trace_stage('psutil_imported')

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from hermes_cli import kanban_db as kb
_trace_stage('kanban_imported')
from hermes_cli import nfos_delivery as d
_trace_stage('delivery_imported')
from hermes_cli import nfos_runtime as runtime
_trace_stage('runtime_imported')


SOURCE = {'platform': 'telegram', 'chat_id': '-100501', 'thread_id': '7', 'message_id': '42'}
PROJECT = {'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}
TEXT = 'Auditar a contagem sem modificar código.'
ATTACHMENTS = [{'original': '/preserved/original.ogg', 'mime_type': 'audio/ogg', 'file_id': 'original-42'}]
TABLES = ('tasks', 'task_runs', 'nfos_workflows', 'task_events', 'kanban_notify_subs')


def receive(conn):
    return d.receive_request(conn, source=SOURCE, text=TEXT, project=PROJECT, attachments=ATTACHMENTS)


def wait_path(path, proc, timeout=25):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            if getattr(proc,'nfos_stderr_path',None):
                stderr=proc.nfos_stderr_path.read_text(errors='replace')
            raise AssertionError(f'Child exited {proc.returncode}: {stdout}\n{stderr}')
        if time.monotonic() > deadline:
            stages=getattr(proc,'nfos_stages_path',path.with_suffix('.stages.jsonl'))
            diagnostic=stages.read_text(errors='replace') if stages.exists() else 'No child stage recorded'
            if getattr(proc,'nfos_stderr_path',None):
                diagnostic+='\n'+proc.nfos_stderr_path.read_text(errors='replace')[-9000:]
            raise AssertionError(f'Child did not reach boundary in {timeout}s: {path.name}\n{diagnostic}')
        time.sleep(.02)


def assert_child_identity(proc, pid):
    # A Windows venv executable may be a launcher with a Python child.
    identities = {proc.pid, *(p.pid for p in psutil.Process(proc.pid).children(recursive=True))}
    assert pid in identities
    return psutil.Process(pid)


def terminate_at_boundary(proc, marker):
    worker = assert_child_identity(proc, json.loads(marker.read_text())['pid'])
    started_at = worker.create_time()
    if worker.pid==proc.pid:
        # On POSIX psutil.wait() would reap Popen's child and discard the
        # SIGKILL status before Popen can collect it (then it reports rc=0).
        proc.kill(); proc.wait(timeout=10)
    else:
        worker.kill(); worker.wait(timeout=10)
    assert not kb._process_identity_matches(worker.pid, started_at)
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    with kb.connect_closing() as conn:
        d.init_schema(conn)
    return tmp_path/'kanban.db'


@pytest.fixture
def children(board):
    running = []
    def launch(mode, *, suffix='one', **kwargs):
        marker = board.parent/(mode+'-'+suffix+'.json')
        go = board.parent/(mode+'-go')
        data = dict(mode=mode, db=str(board), marker=str(marker), go=str(go), **kwargs)
        stderr_path=marker.with_suffix('.stderr.log')
        with stderr_path.open('w') as stderr_log:
            proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--child', json.dumps(data)],
                cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr_log, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        proc.nfos_stderr_path=stderr_path
        proc.nfos_stages_path=marker.with_suffix('.stages.jsonl')
        running.append(proc)
        # Initialize each independent interpreter/connection before the common
        # mutation barrier. Linux cold imports plus serialized schema checks
        # were consuming the race assertion's 25s before it could even begin.
        wait_path(marker.with_suffix('.ready.json'),proc)
        return proc, marker, go
    yield launch
    for proc in running:
        if proc.poll() is None:
            # Terminate the actual Python before closing stdin, which would
            # otherwise release the controlled commit boundary in a survivor.
            descendants=psutil.Process(proc.pid).children(recursive=True)
            for child in reversed(descendants):
                try: child.kill()
                except psutil.NoSuchProcess: pass
            psutil.wait_procs(descendants,timeout=10)
            proc.kill()
        proc.communicate(timeout=10)


def assert_attached_once(conn, rid):
    request = d.get_request(conn, rid)
    assert request['status'] == 'attached'
    assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == 1
    for table in ('tasks', 'task_runs', 'nfos_workflows', 'kanban_notify_subs'):
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1, table
    task = kb.get_task(conn, request['task_id'])
    assert task.created_by == 'worker:default'
    assert task.status == 'running'
    assert task.claim_lock == request['claim_token']
    assert task.worker_pid == request['worker_pid']
    assert d.get_workflow(conn, task.id)['request_id'] == rid
    run = conn.execute('SELECT * FROM task_runs').fetchone()
    assert run['id'] == task.current_run_id and run['task_id'] == task.id
    assert run['worker_pid'] == task.worker_pid
    events = conn.execute("SELECT task_id,run_id,payload FROM task_events WHERE kind='nfos_worker_created_card'").fetchall()
    assert len(events) == 1
    assert (events[0]['task_id'], events[0]['run_id']) == (task.id, task.current_run_id)
    assert json.loads(events[0]['payload'])['request_id'] == rid
    sub = conn.execute('SELECT * FROM kanban_notify_subs').fetchone()
    assert sub['task_id'] == task.id and sub['chat_id'] == SOURCE['chat_id'] and sub['thread_id'] == SOURCE['thread_id']
    payload = json.loads(request['payload'])
    assert payload['source'] == SOURCE and payload['text'] == TEXT and payload['attachments'] == ATTACHMENTS
    return task


@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_bootstrap_process_death_at_commit_preserves_one_request_and_atomic_card(board, children, phase):
    with kb.connect_closing(board) as conn:
        rid = receive(conn)
        reservation = d.reserve_request(conn, capacity=2)
    proc, marker, go = children('crash_bootstrap', phase=phase, rid=rid, token=reservation['claim_token'])
    go.touch()
    wait_path(marker, proc)
    assert json.loads(marker.read_text())['phase'] == phase
    # Independent reader while the worker is frozen at the commit boundary.
    with kb.connect_closing(board) as conn:
        if phase == 'before_commit':
            assert all(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0 for table in TABLES)
            assert d.get_request(conn, rid)['status'] == 'starting'
        else:
            committed = assert_attached_once(conn, rid)
    terminate_at_boundary(proc,marker)
    assert proc.returncode != 0
    # Reopen after an actual process kill, then retransmit the original intake.
    with kb.connect_closing(board) as conn:
        assert receive(conn) == rid
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == 1
        if phase == 'before_commit':
            assert all(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0 for table in TABLES)
            assert runtime.reconcile_starting(conn, now=reservation['claimed_at']+31) == [rid]
            next_reservation = d.reserve_request(conn, capacity=2)
            assert next_reservation['claim_token'] != reservation['claim_token']
            token = next_reservation['claim_token']
        else:
            survived = assert_attached_once(conn, rid)
            assert (survived.id, survived.current_run_id) == (committed.id, committed.current_run_id)
            assert runtime.reconcile_starting(conn, now=reservation['claimed_at']+31) == []
            assert d.reserve_request(conn, capacity=2) is None
            # A new process cannot silently steal a committed creation receipt.
            with pytest.raises(d.OwnershipConflict, match='different execution'):
                d.bootstrap_card(conn, rid, reservation['claim_token'], pid=os.getpid())
            return
    replacement, ready, go = children('bootstrap', suffix='replacement', rid=rid, token=token)
    wait_path(ready, replacement); go.touch()
    result = ready.with_suffix('.result.json'); wait_path(result, replacement)
    assert json.loads(result.read_text())['outcome'] == 'attached'
    with kb.connect_closing(board) as conn:
        restored = assert_attached_once(conn, rid)
        actual = json.loads(result.read_text())['pid']
        assert_child_identity(replacement, actual)
        assert restored.worker_pid == actual


@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_receive_process_death_then_retransmission_has_one_durable_identity(board, children, phase):
    proc, marker, go = children('crash_receive', phase=phase)
    go.touch()
    wait_path(marker, proc)
    terminate_at_boundary(proc,marker)
    with kb.connect_closing(board) as conn:
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == int(phase == 'after_commit')
        rid = receive(conn)
        assert receive(conn) == rid
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == 1
        assert all(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0 for table in TABLES)
        assert json.loads(d.get_request(conn, rid)['payload'])['attachments'] == ATTACHMENTS


@pytest.mark.parametrize('mode', ['receive', 'reserve_bootstrap', 'bootstrap'])
def test_two_processes_do_not_duplicate_intake_card_or_execution_owner(board, children, mode):
    kwargs = {}
    with kb.connect_closing(board) as conn:
        if mode != 'receive':
            rid = receive(conn)
            kwargs['rid'] = rid
        if mode == 'bootstrap':
            kwargs['token'] = d.reserve_request(conn, capacity=2)['claim_token']
    left, lm, go = children(mode, suffix='left', **kwargs)
    right, rm, _ = children(mode, suffix='right', **kwargs)
    wait_path(lm, left); wait_path(rm, right); go.touch()
    lr=lm.with_suffix('.result.json'); rr=rm.with_suffix('.result.json')
    wait_path(lr, left); wait_path(rr, right)
    results = [json.loads(path.read_text()) for path in (lr,rr)]
    with kb.connect_closing(board) as conn:
        if mode == 'receive':
            assert results[0]['rid'] == results[1]['rid'] == receive(conn)
            assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == 1
            assert all(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0 for table in TABLES)
        else:
            assert sorted(row['outcome'] for row in results) == ['attached', 'conflict' if mode == 'bootstrap' else 'no_reservation']
            task = assert_attached_once(conn, rid)
            winner = next(row for row in results if row['outcome'] == 'attached')
            for proc, payload in zip((left,right),results):
                assert_child_identity(proc,payload['pid'])
            assert task.worker_pid == winner['pid']
            assert kb._process_start_time(task.worker_pid) == task.worker_started_at


def _child(data):
    marker=Path(data['marker'])
    def mark(path, payload):
        temporary=path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(payload, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
    mode=data['mode']
    _trace_stage('before_connect')
    with kb.connect_closing(Path(data['db'])) as conn:
        _trace_stage('connected')
        mark(marker.with_suffix('.ready.json'),{'ready':True,'pid':os.getpid()})
        if not mode.startswith('crash_'):
            mark(marker, {'ready':True,'pid':os.getpid()})
        _trace_stage('waiting_for_start')
        faulthandler.cancel_dump_traceback_later()
        # Two children each have a separate 25s preparation budget. Waiting
        # for their shared start is fixture coordination, not an NFOS timeout.
        deadline=time.monotonic()+2*25+5
        while not Path(data['go']).exists():
            if time.monotonic()>deadline:raise RuntimeError('Parent barrier not released after both preparation budgets')
            time.sleep(.02)
        _trace_stage('operation_started')
        faulthandler.dump_traceback_later(8,repeat=True)
        if mode.startswith('crash_'):
            original=kb._execute_boundary_with_retry
            def boundary(target, sql):
                if sql != 'COMMIT':
                    return original(target, sql)
                if data['phase'] == 'before_commit':
                    _trace_stage('before_commit')
                    mark(marker, {'phase':'before_commit', 'pid':os.getpid()})
                    faulthandler.cancel_dump_traceback_later()
                    sys.stdin.read(1)
                result=original(target,sql)
                if data['phase'] == 'after_commit':
                    _trace_stage('after_commit')
                    mark(marker, {'phase':'after_commit', 'pid':os.getpid()})
                    faulthandler.cancel_dump_traceback_later()
                    sys.stdin.read(1)
                return result
            kb._execute_boundary_with_retry=boundary
            if mode == 'crash_receive':
                receive(conn)
            else:
                d.bootstrap_card(conn,data['rid'],data['token'],pid=os.getpid())
            raise AssertionError('Parent must terminate the child before a response is returned')
        if mode == 'receive':
            result={'rid':receive(conn),'pid':os.getpid()}
        else:
            reservation=d.reserve_request(conn,capacity=2) if mode=='reserve_bootstrap' else data
            if not reservation:
                result={'outcome':'no_reservation','pid':os.getpid()}
            else:
                try:
                    task=d.bootstrap_card(conn,data['rid'],reservation.get('claim_token') or reservation['token'],pid=os.getpid())
                    result={'outcome':'attached','task':task.id,'run':task.current_run_id,'pid':os.getpid()}
                except d.OwnershipConflict:
                    result={'outcome':'conflict','pid':os.getpid()}
        mark(marker.with_suffix('.result.json'),result)
        _trace_stage('result_persisted')
        faulthandler.cancel_dump_traceback_later()
        sys.stdin.read(1)


if __name__ == '__main__' and len(sys.argv)>1 and sys.argv[1]=='--child':
    _child(json.loads(sys.argv[2]))
