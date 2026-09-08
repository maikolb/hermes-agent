"""One durable delivery owns terminal text, including display closeouts."""
import asyncio

import gateway.kanban_watchers as kw
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RecordingAdapter


def test_block_closeout_is_once_across_failed_wakes_and_gateway_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'board.db'))
    config = {'kanban': {'agent_wake_on_events': True}, 'display': {'worker_rotation': True}}
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
    monkeypatch.setattr(kw, '_load_worker_focus_config', lambda *args: config)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='Publish tested candidate', assignee='default', requires_repo=False)
        kb.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='test', thread_id='8', delivery_mode='notify+wake')
        kb.block_task(conn, tid, reason='Homologation binding unsupported', kind='needs_input')
    adapter = RecordingAdapter()
    for _ in range(3):
        runner = _make_runner(adapter)
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        asyncio.run(runner._kanban_refresh_worker_focus())
    assert len(adapter.sent) == 1, 'Display closeout bypassed the durable delivery receipt'
    assert 'Worker bloqueado' in adapter.sent[0]
    assert 'Homologation binding unsupported' in adapter.sent[0]
    adapter.fail = False
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.sent) == 1
    with kb.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM kanban_notify_claims').fetchone()[0] == 0
        assert kb.get_task(conn, tid).status == 'blocked'

