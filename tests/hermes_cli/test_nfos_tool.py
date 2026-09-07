"""Real processes and SQLite prove live output, ownership and crash recovery."""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest


@pytest.fixture
def adapter():
    return importlib.import_module('hermes_cli.nfos_tool')


@pytest.fixture
def board(tmp_path):
    path = tmp_path / 'kanban.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
        CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,current_run_id INTEGER);
        CREATE TABLE task_runs(id INTEGER PRIMARY KEY,task_id TEXT,status TEXT);
        CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
            run_id INTEGER,kind TEXT,payload TEXT,created_at INTEGER);
        INSERT INTO tasks VALUES('t_one','running',7);
        INSERT INTO task_runs VALUES(7,'t_one','running');
        ''')
    return path


def read(board, sql, args=()):
    with sqlite3.connect(board) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args)]


def eventually(probe, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = probe()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('Expected real process evidence did not arrive')


def invoke(adapter, board, code, **kwargs):
    return adapter.run_command(board, task_id='t_one', run_id=7,
        argv=[sys.executable, '-u', '-c', code], cwd=board.parent,
        timeout_seconds=kwargs.pop('timeout_seconds', 10), **kwargs)


def test_output_is_committed_while_command_is_still_running(adapter, board):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(invoke, adapter, board,
            "import time,sys;print('visible before exit',flush=True);"
            "sys.stderr.write('actual error stream\\n');sys.stderr.flush();time.sleep(1.2);sys.exit(5)",
            call_id='streaming')
        def streamed():
            try:
                rows = read(board, 'SELECT * FROM nfos_tool_chunks WHERE call_id=?', ('streaming',))
                return rows if b'visible before exit' in b''.join(r['content'] for r in rows) else []
            except sqlite3.OperationalError:
                return []
        rows = eventually(streamed)
        assert not future.done()
        assert read(board, 'SELECT status FROM nfos_tool_calls')[0]['status'] == 'running'
        assert b'visible before exit' in b''.join(r['content'] for r in rows)
        result = future.result(timeout=15)
    assert result['status'] == 'failed' and result['returncode'] == 5
    assert result['timed_out'] is False
    chunks = read(board, 'SELECT * FROM nfos_tool_chunks ORDER BY seq')
    assert b'actual error stream' in b''.join(r['content'] for r in chunks if r['stream'] == 'stderr')
    events = read(board, 'SELECT * FROM task_events ORDER BY id')
    assert {e['run_id'] for e in events} == {7}
    assert all(json.loads(e['payload'])['call_id'] == 'streaming' for e in events)


def test_child_observes_committed_intent_and_identity_before_any_effect(adapter, board):
    code = """import sqlite3,os
c=sqlite3.connect(%r)
r=c.execute("SELECT status,worker_pid,worker_started_at FROM nfos_tool_calls WHERE id='intent'").fetchone()
assert r[0]=='running' and r[1] and r[2],r
print('intent verified')
""" % str(board)
    result = invoke(adapter, board, code, call_id='intent')
    assert result['returncode'] == 0
    assert result['status'] == 'succeeded'


def test_same_call_identity_never_repeats_a_completed_command(adapter, board):
    marker = board.parent / 'effects.txt'
    code = "from pathlib import Path;p=Path(%r);p.write_text(p.read_text()+'x' if p.exists() else 'x')" % str(marker)
    first = invoke(adapter, board, code, call_id='external-effect')
    second = invoke(adapter, board, code, call_id='external-effect')
    assert first['id'] == second['id']
    assert marker.read_text() == 'x'
    with pytest.raises(adapter.ToolExecutionError, match='different'):
        invoke(adapter, board, "print('different operation')", call_id='external-effect')


def test_wrong_task_run_cannot_spawn_a_process(adapter, board):
    marker = board.parent / 'wrong-owner.txt'
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET current_run_id=8")
    with pytest.raises(adapter.ToolExecutionError, match='execution'):
        invoke(adapter, board, "from pathlib import Path;Path(%r).touch()" % str(marker))
    assert not marker.exists()


def test_deadline_terminates_command_and_its_real_descendant(adapter, board, monkeypatch):
    import threading
    from types import SimpleNamespace

    # CI can spend more than one second starting Python. Prove that the actual
    # command AND its actual child are alive before exercising the deadline.
    # Only this module's clock reference moves; process creation, SQLite, output,
    # signals, termination waits and every other module retain real time.
    expired = threading.Event()
    monkeypatch.setattr(adapter, 'time', SimpleNamespace(
        time=time.time, sleep=time.sleep,
        monotonic=lambda: time.monotonic() + (180 if expired.is_set() else 0)))
    child = ("import json,os,psutil,time;"
             "print(json.dumps({'role':'child','pid':os.getpid(),"
             "'started_at':psutil.Process().create_time()}),flush=True);time.sleep(90)")
    code = ("import json,subprocess,sys,time,os,psutil\n"
            "print(json.dumps({'role':'command','pid':os.getpid(),"
            "'started_at':psutil.Process().create_time()}),flush=True)\n"
            f"p=subprocess.Popen([sys.executable,'-u','-c',{child!r}])\n"
            "time.sleep(90)\n")
    owned = []
    ready = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke, adapter, board, code, timeout_seconds=90, call_id='deadline')
        try:
            readiness_deadline = time.monotonic() + 60
            output = b''
            while time.monotonic() < readiness_deadline:
                try:
                    chunks = read(board, "SELECT * FROM nfos_tool_chunks WHERE call_id='deadline' ORDER BY seq")
                    output = b''.join(r['content'] for r in chunks if r['stream'] == 'stdout')
                    for line in output.splitlines():
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # a persisted chunk may end halfway through a line
                        ready[row['role']] = row
                except sqlite3.OperationalError:
                    pass
                if set(ready) == {'command', 'child'}:
                    break
                if future.done():
                    pytest.fail(f'Fixture process ended before both readiness receipts: {future.result()}; stdout={output!r}')
                time.sleep(.025)
            else:
                pytest.fail(f'Fixture startup exceeded 60s before both readiness receipts; stdout={output!r}')
            call = read(board, "SELECT * FROM nfos_tool_calls WHERE id='deadline'")[0]
            root = psutil.Process(call['worker_pid'])
            assert abs(root.create_time() - call['worker_started_at']) < .01
            owned = [{'pid': p.pid, 'started_at': p.create_time()}
                     for p in [root, *root.children(recursive=True)]]
            for receipt in ready.values():
                assert any(p['pid'] == receipt['pid'] and
                           abs(p['started_at'] - receipt['started_at']) < .01 for p in owned)
                assert psutil.Process(receipt['pid']).status() != psutil.STATUS_ZOMBIE
            assert call['status'] == 'running' and not future.done()
            assert call['deadline_at'] - call['started_at'] == pytest.approx(90)
            expired.set()
            result = future.result(timeout=20)
            assert result['status'] == 'timed_out' and result['timed_out'] is True
            assert result['error'] == 'Tool deadline exceeded'
            assert len(ready) == 2
            # Assert before fixture cleanup: cleanup must never hide a failure
            # of the adapter to terminate one of the two real processes.
            for receipt in ready.values():
                try:
                    process = psutil.Process(receipt['pid'])
                    assert (abs(process.create_time() - receipt['started_at']) >= .01 or
                            process.status() == psutil.STATUS_ZOMBIE)
                except psutil.NoSuchProcess:
                    pass
        finally:
            # On a failed readiness/assertion the adapter still receives its
            # deadline, then only recorded identities from this test are cleaned.
            expired.set()
            try:
                future.result(timeout=20)
            finally:
                cleanup_verified_test_processes(owned)


def test_utf8_and_large_output_are_preserved_without_truncation(adapter, board):
    expected = ('ação 🧪\n' * 3000).encode()
    result = invoke(adapter, board, "import sys;sys.stdout.buffer.write(('a\\u00e7\\u00e3o \\U0001f9ea\\n'*3000).encode());sys.stdout.flush()",
        call_id='bytes')
    assert result['status'] == 'succeeded'
    with adapter.connect(board) as conn:
        first = adapter.read_chunks(conn, 'bytes', after_seq=0, limit=1)
        rest = adapter.read_chunks(conn, 'bytes', after_seq=first[0]['seq'], limit=1000)
    chunks = first + rest
    assert b''.join(r['content'] for r in chunks) == expected
    assert len({r['seq'] for r in chunks}) == len(chunks)


def test_live_streaming_does_not_scan_all_host_process_groups_each_tick(adapter, board, monkeypatch):
    group_scans=[]
    original=adapter._group_children
    def counted(pid, started_at):
        group_scans.append(pid)
        return original(pid,started_at)
    monkeypatch.setattr(adapter,'_group_children',counted)
    result=invoke(adapter,board,"import time;print('stream remains live',flush=True);time.sleep(1.1)")
    assert result['status']=='succeeded'
    assert not group_scans, 'Normal streaming must not enumerate all host process groups'


def hidden_kwargs():
    if os.name != 'nt':
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {'creationflags': subprocess.CREATE_NO_WINDOW, 'startupinfo': si}


def cleanup_verified_test_processes(identities):
    """Only receipts captured from processes this test created authorize cleanup."""
    for identity in reversed(identities):
        try:
            proc = psutil.Process(identity['pid'])
            if abs(proc.create_time() - identity['started_at']) < .01 and proc.status() != psutil.STATUS_ZOMBIE:
                proc.kill()
        except psutil.NoSuchProcess:
            pass


@pytest.mark.live_system_guard_bypass  # real test children become orphans after deliberate adapter death
def test_reconcile_after_actual_adapter_death_preserves_output_and_stops_child(adapter, board):
    command = [sys.executable, str(Path(adapter.__file__)), '--db', str(board),
        '--task', 't_one', '--run', '7', '--cwd', str(board.parent), '--timeout', '90',
        '--call-id', 'crash', '--', sys.executable, '-u', '-c',
        "import time;print('persisted before adapter crash',flush=True);time.sleep(90)"]
    runner = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_kwargs())
    owned = [{'pid': runner.pid, 'started_at': psutil.Process(runner.pid).create_time()}]
    try:
        def streamed():
            try:
                return read(board, 'SELECT * FROM nfos_tool_chunks WHERE call_id=?', ('crash',))
            except sqlite3.OperationalError:
                return []
        eventually(streamed)
        row = read(board, 'SELECT * FROM nfos_tool_calls WHERE id=?', ('crash',))[0]
        # Capture provenance while the complete tree is still our own subtree.
        owned.extend({'pid': p.pid, 'started_at': p.create_time()}
                     for p in psutil.Process(runner.pid).children(recursive=True))
        # A hidden Windows launcher may wrap the actual Python adapter. Its
        # recorded PID must still be a creation-time-verified member of our tree.
        actual_runner=next((p for p in owned if p['pid']==row['runner_pid'] and
                            abs(p['started_at']-row['runner_started_at'])<.01),None)
        assert actual_runner is not None
        assert any(p['pid'] == row['worker_pid'] and
                   abs(p['started_at'] - row['worker_started_at']) < .01 for p in owned)
        psutil.Process(actual_runner['pid']).kill()
        runner.wait(timeout=10)
        with adapter.connect(board) as conn:
            reconciled = adapter.reconcile_calls(conn)
        assert reconciled[0]['status'] == 'interrupted'
        assert read(board, 'SELECT * FROM nfos_tool_chunks')[0]['content']
        assert not psutil.pid_exists(row['worker_pid']) or psutil.Process(row['worker_pid']).status() == psutil.STATUS_ZOMBIE
    finally:
        cleanup_verified_test_processes(owned)
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=10)


def test_reconciliation_does_not_signal_reused_pid_identity(adapter, board):
    with adapter.connect(board) as conn:
        adapter.init_schema(conn)
        conn.execute('''INSERT INTO nfos_tool_calls
            (id,task_id,run_id,argv_json,cwd,status,created_at,timeout_seconds,
             runner_pid,runner_started_at,worker_pid,worker_started_at)
            VALUES('foreign','t_one',7,'[]',?,'running',?,10,0,0,?,?)''',
            (str(board.parent), time.time(), os.getpid(), psutil.Process().create_time() - 100))
        conn.commit()
        result = adapter.reconcile_calls(conn)
    assert result[0]['status'] == 'interrupted'
    assert psutil.Process(os.getpid()).is_running()
    assert 'identity' in result[0]['error'].lower()


def test_prompt_file_is_actual_stdin_and_not_command_line(adapter, board):
    prompt = board.parent / 'prompt.txt'
    prompt.write_text('A spec completa: ação e conferência.', encoding='utf-8')
    result = invoke(adapter, board, "import sys;print(sys.stdin.read())", stdin_path=prompt, call_id='prompt')
    assert result['returncode'] == 0
    chunks = read(board, 'SELECT * FROM nfos_tool_chunks ORDER BY seq')
    assert 'A spec completa: ação e conferência.' in b''.join(r['content'] for r in chunks).decode()
    assert 'A spec completa' not in result['argv_json']
    assert result['stdin_sha256']


def test_live_ownership_loss_stops_the_old_call(adapter, board):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(invoke, adapter, board, "import time;print('started',flush=True);time.sleep(90)",
            call_id='lost-owner', timeout_seconds=90)
        def streamed():
            try:
                return read(board, "SELECT * FROM nfos_tool_chunks WHERE call_id='lost-owner'")
            except sqlite3.OperationalError:
                return []
        eventually(streamed)
        with sqlite3.connect(board) as conn:
            conn.execute("UPDATE tasks SET current_run_id=8")
        result = future.result(timeout=10)
    assert result['status'] == 'interrupted'
    assert 'ownership' in result['error']


def test_runtime_reconciliation_reports_a_transition_only_once(adapter, board):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(invoke, adapter, board,
            "import time;print('active',flush=True);time.sleep(90)",
            call_id='runtime-expiry', timeout_seconds=90)
        def streamed():
            try:
                return read(board, "SELECT * FROM nfos_tool_chunks WHERE call_id='runtime-expiry'")
            except sqlite3.OperationalError:
                return []
        eventually(streamed)
        with adapter.connect(board) as conn:
            conn.execute("UPDATE nfos_tool_calls SET deadline_at=? WHERE id='runtime-expiry'", (time.time()-1,))
            conn.commit()
            first = adapter.reconcile_calls(conn)
            second = adapter.reconcile_calls(conn)
        result = future.result(timeout=10)
    assert len(first) == 1 and first[0]['status'] == 'timed_out'
    assert second == []
    assert result['status'] == 'timed_out'
    events = read(board, "SELECT * FROM task_events WHERE kind='nfos_tool_finished'")
    assert len(events) == 1


def test_runtime_can_stop_only_the_selected_cards_calls(adapter, board):
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO tasks VALUES('t_other','running',8)")
        conn.execute("INSERT INTO task_runs VALUES(8,'t_other','running')")
    def code(release):
        return "from pathlib import Path;import time;print('active',flush=True)\nwhile not Path(%r).exists():time.sleep(.05)" % str(release)
    selected_release=board.parent/'selected-release'
    unrelated_release=board.parent/'unrelated-release'
    with concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(invoke, adapter, board, code(selected_release), call_id='selected',timeout_seconds=90)
        other = pool.submit(adapter.run_command, board, task_id='t_other', run_id=8,
            argv=[sys.executable,'-u','-c',code(unrelated_release)], cwd=board.parent,
            timeout_seconds=90, call_id='unrelated')
        def both_streamed():
            try:
                return len(read(board, 'SELECT DISTINCT call_id FROM nfos_tool_chunks')) == 2
            except sqlite3.OperationalError:
                return False
        try:
            eventually(both_streamed,timeout=45)
            with adapter.connect(board) as conn:
                adapter.terminate_calls(conn, 't_one', 7, reason='Human answer required')
            assert not other.done(), 'The unrelated command is still waiting for its explicit release'
            assert first.result(timeout=10)['status'] == 'interrupted'
        finally:
            selected_release.touch()
            unrelated_release.touch()
        assert other.result(timeout=10)['status'] == 'succeeded'


@pytest.mark.live_system_guard_bypass  # the deliberately reparented child requires a real signal
@pytest.mark.skipif(os.name == 'nt', reason='POSIX reparented process-group semantics')
def test_fast_command_exit_cannot_leave_an_unbounded_descendant(adapter, board):
    receipt = board.parent / 'orphan-child-created-by-this-test.json'
    code = """import subprocess,sys,psutil,json,os
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])
Path(%r).write_text(json.dumps({'pid':p.pid,'started_at':psutil.Process(p.pid).create_time(),'group_id':os.getpgid(0)}))
print(p.pid,flush=True)
""" % str(receipt)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future=pool.submit(invoke,adapter,board,code,call_id='orphan',timeout_seconds=90)
        try:
            eventually(receipt.exists,timeout=45)
            def stdout():
                chunks=read(board,"SELECT * FROM nfos_tool_chunks WHERE stream='stdout' ORDER BY seq")
                return b''.join(r['content'] for r in chunks).strip()
            pid=int(eventually(stdout,timeout=45))
            identity = json.loads(receipt.read_text())
            row=read(board,"SELECT * FROM nfos_tool_calls WHERE id='orphan'")[0]
            assert identity['pid'] == pid and identity['group_id'] == row['worker_pid']
            assert started - .01 <= identity['started_at'] <= time.time()
            assert adapter._matches(pid,identity['started_at']) and not future.done()
            with adapter.connect(board) as conn:
                conn.execute("UPDATE nfos_tool_calls SET deadline_at=? WHERE id='orphan'",(time.time()-1,))
                conn.commit()
                adapter.reconcile_calls(conn)
            result=future.result(timeout=15)
            assert result['status']=='timed_out' and result['timed_out'] is True
            assert not adapter._matches(pid,identity['started_at'])
        finally:
            # Even an assertion failure must not leave this test's orphan running.
            with adapter.connect(board) as conn:
                adapter.terminate_calls(conn,'t_one',7,reason='Fixture cleanup')
            if receipt.exists():
                identity = json.loads(receipt.read_text())
                assert started - .01 <= identity['started_at'] <= time.time()
                rows = read(board, "SELECT * FROM nfos_tool_calls WHERE id='orphan'")
                assert rows and rows[0]['cwd'] == str(board.parent)
                assert rows[0]['worker_pid'] == identity['group_id']
                cleanup_verified_test_processes([
                    {'pid':rows[0]['worker_pid'],'started_at':rows[0]['worker_started_at']}, identity])
