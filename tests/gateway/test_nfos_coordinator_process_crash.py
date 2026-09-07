"""Real process death between durable Telegram intake and Principal delivery.

The two interpreters use SQLite, the watcher, adapter admission and checkpoint
writer. Telegram transport and the Principal's classification are fixture inputs;
there is no network or model call.
"""
import asyncio
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.platforms.base import MessageEvent, Platform, SessionSource
from gateway.config import PlatformConfig
from tests.gateway.test_kanban_notifier_durable import RealAdapter
from tests.hermes_cli.test_nfos_bootstrap_process_crashes import (
    assert_child_identity, terminate_at_boundary, wait_path,
)


def _signal(path, **value):
    value.update(pid=os.getpid(), monotonic=time.monotonic())
    temporary = path.with_suffix('.writing')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Runner(GatewayKanbanWatchersMixin):
    def __init__(self, adapter):
        self.adapter = adapter

    def _active_profile_name(self): return 'default'
    def _adapter_profile_for_source(self, source): return 'default'
    def _adapter_for_source(self, source): return self.adapter
    def _kanban_parallel_dispatch_config(self, source):
        return {'kanban': {'delivery': {'projects': {'default': {
            'enabled':True, 'profile':'default', 'delivery_type':'report', 'workers':2,
        }}}}}
    def _resolve_project_context_for_message(self, event, source):
        return SimpleNamespace(board_slug='default', project_id='', is_management=False), None


def _snapshot(db):
    with kb.connect_closing(db) as conn:
        rows = conn.execute('SELECT * FROM nfos_requests').fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert row['task_id'] is None and row['worker_pid'] is None
        return row, json.loads(row['payload'])


def test_process_crash_after_intake_recovers_checkpoint_and_one_task_identity(tmp_path, monkeypatch):
    import psutil
    db = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with kb.connect_closing(db) as conn:
        delivery.init_schema(conn)
    original = tmp_path/'original.bin'
    original.write_bytes(b'Original attachment survives the first gateway process')
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_KANBAN_DB=str(db),
               HERMES_KANBAN_BOARD='default', PYTHONPATH=str(ROOT))
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID',
                'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        env.pop(key, None)
    children = []

    def launch(mode):
        marker = tmp_path/(mode+'.json')
        stderr_path = marker.with_suffix('.stderr.log')
        with stderr_path.open('w', encoding='utf-8') as errors:
            proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                '--child', mode, str(marker)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=errors, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        proc.nfos_stderr_path = stderr_path
        children.append(proc)
        wait_path(marker.with_suffix('.ready.json'), proc)
        return proc, marker

    try:
        producer, first = launch('receive')
        wait_path(first, producer)
        before, payload = _snapshot(db)
        assert before['status'] == 'coordinating'
        assert not payload['coordination'].get('wake_accepted')
        assert payload['coordination']['reply_to_message_id'] == '122'
        preserved = Path(payload['attachments'][0]['original'])
        original.unlink()
        assert preserved.read_bytes() == b'Original attachment survives the first gateway process'
        terminate_at_boundary(producer, first)
        assert producer.returncode != 0

        consumer, checkpoint = launch('consume')
        wait_path(checkpoint, consumer)
        accepted = json.loads(checkpoint.read_text(encoding='utf-8'))
        assert_child_identity(consumer, accepted['pid'])
        current, stored = _snapshot(db)
        assert current['id'] == before['id'] and current['status'] == 'coordinating'
        assert stored['coordination']['wake_accepted']
        assert accepted['request_id'] == before['id']
        assert accepted['request']['source'] == payload['source']
        assert accepted['request']['attachments'] == payload['attachments']
        assert accepted['checkpoint_has_request'] is True
        # Let the fixture Principal classify the independent request only now.
        consumer.stdin.write('classify\n')
        consumer.stdin.flush()
        result_path = checkpoint.with_suffix('.result.json')
        wait_path(result_path, consumer)
        result = json.loads(result_path.read_text(encoding='utf-8'))
        stdout, _ = consumer.communicate(timeout=10)
        assert consumer.returncode == 0, stdout
        final, final_payload = _snapshot(db)
        assert final['status'] == 'pending'
        assert result['request_ids'] == [before['id'], before['id']]
        assert final_payload['source'] == payload['source']
        print(json.dumps({'producer_exit':producer.returncode, 'consumer_exit':consumer.returncode,
            'request_id':final['id'], 'status':final['status'], 'request_count':1, 'card_count':0,
            'checkpoint_ack':True, 'duplicate_request':False}))
    finally:
        for proc in children:
            if proc.poll() is None:
                descendants = psutil.Process(proc.pid).children(recursive=True)
                for child in reversed(descendants):
                    try: child.kill()
                    except psutil.NoSuchProcess: pass
                psutil.wait_procs(descendants, timeout=10)
                proc.kill()
            proc.communicate(timeout=10)


async def _child(mode, marker):
    root = marker.parent
    _signal(marker.with_suffix('.ready.json'), stage='imports_ready')
    if mode == 'receive':
        runner = Runner(SimpleNamespace())

        async def crash_before_wake(**kwargs):
            _signal(marker, stage='receive_committed_before_wake')
            await asyncio.to_thread(sys.stdin.readline)
            raise AssertionError('Crash boundary was released without process termination')

        runner._nfos_retry_coordinator_inputs = crash_before_wake
        event = MessageEvent(text='quero um relatório', message_id='123', reply_to_message_id='122',
            media_urls=[str(root/'original.bin')], media_types=['application/octet-stream'],
            source=SessionSource(platform=Platform.TELEGRAM, chat_id='test', thread_id='8',
                                 chat_type='group', user_id='owner'))
        await runner._nfos_receive(event)
        return

    from agent.turn_checkpoint import initialize_agent_turn_checkpoint
    from gateway.wake import notify_wake_accepted
    adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
    adapter.config.typing_indicator = False
    done = asyncio.Event()

    async def handle(event):
        context = event.metadata['nfos_coordinator_intake']
        receipt = event.metadata['kanban_wake_delivery']
        assert not notify_wake_accepted(receipt)
        agent = SimpleNamespace(session_id='principal-after-crash',
            _session_db=SimpleNamespace(db_path=root/'state.db'))
        await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
            turn_id='restored-original-input', user_content=event.text, messages=[])
        assert notify_wake_accepted(receipt)
        checkpoint = agent._turn_checkpoint_store.load(agent.session_id)
        from tests.gateway.test_nfos_coordinator_intake import _request_json_from_note
        recovered_request = _request_json_from_note(checkpoint['active_user_turn']['content'])
        assert recovered_request == context['request']
        _signal(marker, stage='checkpoint_before_classification', request_id=receipt['request_id'],
                request=context['request'], checkpoint_has_request=True)
        assert (await asyncio.to_thread(sys.stdin.readline)).strip() == 'classify'
        path = root/'classified-request.json'
        path.write_text(json.dumps(context['request']), encoding='utf-8')
        sys.argv = ['nfos_delivery', 'receive', '--db', receipt['db_path'], '--input', str(path)]
        ids = []
        for _ in range(2):
            output = io.StringIO()
            with redirect_stdout(output): delivery.main()
            ids.append(json.loads(output.getvalue())['request_id'])
        _signal(marker.with_suffix('.result.json'), stage='classified', request_ids=ids)
        done.set()

    adapter._message_handler = handle
    runner = Runner(adapter)
    await runner._nfos_retry_coordinator_inputs(board='default')
    await asyncio.wait_for(done.wait(), 25)
    await asyncio.gather(*list(adapter._session_tasks.values()))


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--child':
    asyncio.run(_child(sys.argv[2], Path(sys.argv[3])))
