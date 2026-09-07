"""Native exit results survive the wrapper and runtime-initiated termination."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests.hermes_cli.test_nfos_tool import adapter, board, eventually, invoke, read


def _json_when_complete(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _saved_call(adapter, board, call_id):
    with adapter.connect(board) as conn:
        return adapter.get_call(conn, call_id)


def _stop_own_call(adapter, board, call_id):
    row = _saved_call(adapter, board, call_id)
    if row:
        assert adapter._stop_tree(row) == []


def _expire(adapter, board, call_id):
    with adapter.connect(board) as conn:
        conn.execute('UPDATE nfos_tool_calls SET deadline_at=? WHERE id=?', (time.time() - 1, call_id))
        conn.commit()
        return adapter.reconcile_calls(conn)


def _evidence(name, value):
    directory = os.environ.get('NFOS_OUTCOME_TEST_EVIDENCE_DIR')
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / (name + '.json')).write_text(json.dumps(value, indent=2))


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux fixture subreaper preserves test ancestry')
def test_runtime_deadline_preserves_a_committed_native_zero_with_live_descendant(adapter, board, monkeypatch):
    # Adopt the fixture's orphan in its wrapper, so the real live-system guard
    # can verify pytest ancestry. The production stop and receipt stay intact.
    source = Path(adapter.__file__).resolve()
    shim = board.parent / 'owned-subreaper.py'
    shim.write_text('''import ctypes,importlib.util
assert ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
spec=importlib.util.spec_from_file_location('fixture_adapter',%r)
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
raise SystemExit(adapter._child())
''' % str(source))
    monkeypatch.setattr(adapter, '__file__', str(shim))
    marker = board.parent / 'owned-descendant.json'
    code = '''import json,subprocess,sys,time,psutil
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])
target=Path(%r); temporary=target.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid':child.pid,'started_at':psutil.Process(child.pid).create_time()}))
temporary.replace(target)
print('command exiting zero with an owned live descendant',flush=True)
''' % str(marker)
    call_id = 'committed-zero-before-deadline'
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(invoke, adapter, board, code, timeout_seconds=90, call_id=call_id)
        try:
            child = eventually(lambda: _json_when_complete(marker), timeout=35)
            def committed():
                try:
                    events = read(board, "SELECT payload FROM task_events WHERE kind='nfos_tool_command_exited'")
                    row = _saved_call(adapter, board, call_id)
                    return row if events and row['returncode'] == 0 else None
                except Exception:
                    return None
            before = eventually(committed, timeout=35)
            assert adapter._matches(child['pid'], child['started_at'])
            assert adapter.psutil.Process(child['pid']).ppid() == before['worker_pid']
            assert child in adapter._descendants(before, include_group=True)
            assert not future.done()
            changes = _expire(adapter, board, call_id)
            result = future.result(timeout=15)
            assert len(changes) == 1 and changes[0]['status'] == 'timed_out'
            assert result['status'] == 'timed_out' and result['timed_out'] is True
            assert result['returncode'] == 0, result
            assert not adapter._matches(child['pid'], child['started_at'])
            assert not adapter._matches(before['worker_pid'], before['worker_started_at'])
            exits = read(board, "SELECT payload FROM task_events WHERE kind='nfos_tool_command_exited'")
            assert len(exits) == 1 and json.loads(exits[0]['payload'])['returncode'] == 0
            _evidence('committed-native-zero', {'before': before, 'descendant': child, 'result': result,
                'native_exit_events': exits, 'survivors_before_cleanup': []})
        finally:
            _stop_own_call(adapter, board, call_id)
            future.result(timeout=15)


@pytest.mark.skipif(os.name == 'nt', reason='Real POSIX wrapper termination')
@pytest.mark.parametrize('finalizer_order', ['reconciler_first', 'runner_first'])
def test_unpublished_native_exit_stays_unknown_without_success_or_replay(adapter, board, monkeypatch, finalizer_order):
    # Pause only the fixture publisher. Native command exit and the unmodified
    # _stop_tree/reconcile_calls remain real. No native return code is injected.
    source = Path(adapter.__file__).resolve()
    shim = board.parent / 'publication-barrier.py'
    marker = board.parent / 'native-exit-before-commit.json'
    effect = board.parent / 'effects.txt'
    shim.write_text('''import importlib.util,json,os,sys,time,psutil
from pathlib import Path
spec=importlib.util.spec_from_file_location('fixture_adapter',%r)
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
def hold_before_publication(payload,command_pid,returncode):
    target=Path(%r);temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':os.getpid(),'started_at':psutil.Process().create_time(),
        'command_pid':command_pid,'native_returncode':returncode,'call_id':payload['call_id']}))
    temporary.replace(target)
    while True:time.sleep(.02)
adapter._publish_command_exit=hold_before_publication
raise SystemExit(adapter._child())
''' % (str(source), str(marker)))
    monkeypatch.setattr(adapter, '__file__', str(shim))
    original_finish = adapter._finish
    first_finished = threading.Event()
    observed_finalizers = []
    def finish_in_chosen_order(conn, call_id, status, **kwargs):
        actor = 'reconciler' if threading.current_thread() is threading.main_thread() else 'runner'
        preferred = actor == finalizer_order.removesuffix('_first')
        if not preferred:
            assert first_finished.wait(15), 'Fixture finalization barrier was not reached'
        value = original_finish(conn, call_id, status, **kwargs)
        observed_finalizers.append({'actor': actor, 'argument_returncode': kwargs.get('returncode'),
            'stored_returncode': value['returncode'] if value else None})
        if preferred:
            first_finished.set()
        return value
    monkeypatch.setattr(adapter, '_finish', finish_in_chosen_order)
    code = "from pathlib import Path;p=Path(%r);p.write_text(p.read_text()+'one\\n' if p.exists() else 'one\\n')" % str(effect)
    call_id = 'unpublished-native-exit'
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(invoke, adapter, board, code, timeout_seconds=90, call_id=call_id)
        try:
            observed = eventually(lambda: _json_when_complete(marker), timeout=35)
            before = _saved_call(adapter, board, call_id)
            assert observed['native_returncode'] == 0
            assert (observed['pid'], observed['started_at']) == (before['worker_pid'], before['worker_started_at'])
            assert adapter._matches(observed['pid'], observed['started_at'])
            assert before['returncode'] is None
            assert read(board, "SELECT payload FROM task_events WHERE kind='nfos_tool_command_exited'") == []
            changes = _expire(adapter, board, call_id)
            result = future.result(timeout=15)
            _evidence('unpublished-before-assert-' + finalizer_order, {'before': before,
                'native_result_observed_by_fixture': observed, 'result': result,
                'observed_finalizers': observed_finalizers,
                'wrapper_alive': adapter._matches(observed['pid'], observed['started_at'])})
            assert len(changes) == 1 and changes[0]['status'] == 'timed_out'
            assert result['status'] == 'timed_out' and result['timed_out'] is True
            assert result['returncode'] is None, result
            assert not adapter._matches(observed['pid'], observed['started_at'])
            assert adapter._descendants(result, include_group=True) == []
            # The real CLI must read the same durable receipt, return failure,
            # and neither spawn another native call nor repeat its effect.
            replay = subprocess.run([sys.executable, '-B', str(source), '--db', str(board), '--task', 't_one',
                '--run', '7', '--timeout', '90', '--cwd', str(board.parent), '--call-id', call_id,
                '--', sys.executable, '-u', '-c', code], capture_output=True, text=True, timeout=35)
            assert replay.returncode == 124, (replay.stdout, replay.stderr)
            replay_result = json.loads(replay.stdout)
            assert replay_result['status'] == 'timed_out' and replay_result['returncode'] is None
            assert effect.read_text() == 'one\n'
            assert len(read(board, 'SELECT id FROM nfos_tool_calls')) == 1
            assert len(read(board, "SELECT id FROM task_events WHERE kind='nfos_tool_started'")) == 1
            with adapter.connect(board) as conn:
                assert adapter.reconcile_calls(conn) == []
            _evidence('unpublished-native-exit-' + finalizer_order, {'before': before, 'native_result_observed_by_fixture': observed,
                'result': result, 'cli_exitcode': replay.returncode, 'replay_result': replay_result,
                'observed_finalizers': observed_finalizers,
                'effect_count': 1, 'native_call_count': 1, 'survivors_before_cleanup': []})
        finally:
            _stop_own_call(adapter, board, call_id)
            future.result(timeout=15)


@pytest.mark.skipif(os.name == 'nt', reason='Real POSIX signal outcomes')
def test_signal_exit_reports_the_command_result_not_wrapper_encoding(adapter,board):
    result=invoke(adapter,board,
        "import os,signal;os.kill(os.getpid(),signal.SIGTERM)",
        timeout_seconds=20,call_id='actual-signal-result')
    assert result['status']=='failed'
    assert result['returncode']==-signal.SIGTERM, result


def test_wrapper_zero_without_native_receipt_is_interrupted_and_not_replayed(adapter, board, monkeypatch):
    source = Path(adapter.__file__).resolve()
    shim = board.parent / 'missing-publication.py'
    shim.write_text('''import importlib.util
spec=importlib.util.spec_from_file_location('fixture_adapter',%r)
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
adapter._publish_command_exit=lambda *_:None
raise SystemExit(adapter._child())
''' % str(source))
    monkeypatch.setattr(adapter, '__file__', str(shim))
    effect = board.parent / 'wrapper-zero-effects.txt'
    code = "from pathlib import Path;p=Path(%r);p.write_text(p.read_text()+'one\\n' if p.exists() else 'one\\n')" % str(effect)
    result = invoke(adapter, board, code, timeout_seconds=90, call_id='wrapper-zero')
    _evidence('wrapper-zero-before-assert', {'result': result})
    assert result['status'] == 'interrupted', result
    assert result['returncode'] is None and result['timed_out'] is False
    assert result['error'] == 'Native exit receipt missing; effect may be unknown'
    assert read(board, "SELECT payload FROM task_events WHERE kind='nfos_tool_command_exited'") == []
    replay = invoke(adapter, board, code, timeout_seconds=90, call_id='wrapper-zero')
    assert replay == result
    assert effect.read_text() == 'one\n'
    assert len(read(board, "SELECT id FROM task_events WHERE kind='nfos_tool_started'")) == 1
    assert not adapter._matches(result['worker_pid'], result['worker_started_at'])
    assert adapter._descendants(result, include_group=True) == []
    _evidence('wrapper-zero-missing-receipt', {'result': result, 'replay_result': replay,
        'effect_count': 1, 'native_call_count': 1, 'survivors_before_cleanup': []})
