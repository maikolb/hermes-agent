"""Native exit results survive the wrapper and runtime-initiated termination."""
import concurrent.futures
import os
import signal
import time

import pytest

from tests.hermes_cli.test_nfos_tool import adapter, board, eventually, invoke, read


@pytest.mark.skipif(os.name == 'nt', reason='Real POSIX signal outcomes')
def test_runtime_deadline_preserves_zero_after_handler_is_confirmed(adapter, board):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future=pool.submit(invoke,adapter,board,
            "import signal,sys,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));print('handler-ready',flush=True);time.sleep(90)",
            timeout_seconds=90,call_id='ready-before-deadline')
        def ready():
            try:
                return b'handler-ready' in b''.join(row['content'] for row in read(board,'SELECT content FROM nfos_tool_chunks'))
            except Exception:
                return False
        eventually(ready,timeout=20)
        assert not future.done()
        with adapter.connect(board) as conn:
            conn.execute("UPDATE nfos_tool_calls SET deadline_at=? WHERE id='ready-before-deadline'",(time.time()-1,))
            conn.commit()
            changed=adapter.reconcile_calls(conn)
            assert len(changed)==1 and changed[0]['status']=='timed_out'
        result=future.result(timeout=15)
    assert result['timed_out'] is True
    assert result['returncode']==0, result
    exits=read(board,"SELECT payload FROM task_events WHERE kind='nfos_tool_command_exited'")
    assert len(exits)==1


@pytest.mark.skipif(os.name == 'nt', reason='Real POSIX signal outcomes')
def test_signal_exit_reports_the_command_result_not_wrapper_encoding(adapter,board):
    result=invoke(adapter,board,
        "import os,signal;os.kill(os.getpid(),signal.SIGTERM)",
        timeout_seconds=20,call_id='actual-signal-result')
    assert result['status']=='failed'
    assert result['returncode']==-signal.SIGTERM, result
