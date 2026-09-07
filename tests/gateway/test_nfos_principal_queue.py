"""A worker's persisted question reaches the Principal without ending its run."""
import asyncio
import os

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from tests.gateway.test_kanban_notifier import _make_runner,_run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RecordingAdapter


def test_principal_wake_survives_failed_acceptance_and_keeps_same_worker(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    monkeypatch.setattr('hermes_cli.config.load_config',lambda:{'kanban':{'agent_wake_on_events':True}})
    with kb.connect_closing() as conn:
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'test','thread_id':'8','message_id':'1','chat_type':'group'},
            text='Audit',project={'profile':'default','delivery_type':'report'})
        req=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,req['claim_token'],pid=os.getpid())
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,kind='impediment',
            question='Qual fonte atende ao critério?',context={'tried':['Read existing evidence']})
    adapter=RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch,_make_runner(adapter)))
    assert len(adapter.handled)==1
    from hermes_cli.nfos_runtime import workflow_command
    assert workflow_command()+' pending' in adapter.handled[0].text
    with kb.connect_closing() as conn:
        assert delivery.get_decision(conn,decision)['status']=='pending'
        assert kb.get_task(conn,task.id).current_run_id==task.current_run_id
    adapter.fail=False
    asyncio.run(_run_one_notifier_tick(monkeypatch,_make_runner(adapter)))
    assert len(adapter.handled)==2
    assert len(adapter.sent)==1
    with kb.connect_closing() as conn:
        assert kb.get_task(conn,task.id).worker_pid==os.getpid()


def test_retained_notify_only_block_reaches_principal_via_existing_notifier(tmp_path,monkeypatch):
    from hermes_cli.nfos_runtime import adopt_existing_tasks
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    monkeypatch.setattr('hermes_cli.config.load_config',lambda:{'kanban':{'agent_wake_on_events':True}})
    with kb.connect_closing() as conn:
        tid=kb.create_task(conn,title='Existing impediment',assignee='default',delivery_type='report',requires_repo=False)
        kb.block_task(conn,tid,reason='Existing tool error',kind='transient')
        prior_runs=[tuple(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]
        kb.add_notify_sub(conn,task_id=tid,platform='telegram',chat_id='test',thread_id='8',
                          chat_type='group',notifier_profile='default',delivery_mode='notify')
        cursor=conn.execute('SELECT last_event_id FROM kanban_notify_subs').fetchone()[0]
        adopt_existing_tasks(conn,board='default',project={'profile':'default','delivery_type':'report'})
        assert conn.execute('SELECT last_event_id FROM kanban_notify_subs').fetchone()[0]==cursor
    adapter=RecordingAdapter();adapter.fail=False
    asyncio.run(_run_one_notifier_tick(monkeypatch,_make_runner(adapter)))
    assert len(adapter.handled)==1
    with kb.connect_closing() as conn:
        assert len(delivery.pending_decisions(conn))==1
        assert kb.get_task(conn,tid).status=='blocked'
        assert [tuple(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]==prior_runs
    asyncio.run(_run_one_notifier_tick(monkeypatch,_make_runner(adapter)))
    assert len(adapter.handled)==1


def test_telegram_outage_does_not_block_principal_decision_delivery(tmp_path, monkeypatch):
    """Internal handoff and human notification have separate durable ACKs."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {'kanban': {'agent_wake_on_events': True}})
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(conn, source={'platform': 'telegram', 'chat_id': 'test', 'thread_id': '8', 'message_id': '1', 'chat_type': 'group'},
            text='Audit', project={'profile': 'default', 'delivery_type': 'report'})
        request = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
        delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Review the saved evidence', context={})

    class OfflineTelegram(RecordingAdapter):
        offline = True

        async def send(self, chat_id, text, metadata=None):
            if self.offline:
                raise ConnectionError('Telegram unavailable')
            return await super().send(chat_id, text, metadata)

    adapter = OfflineTelegram()
    adapter.fail = False
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1, 'Telegram egress incorrectly blocked the internal Principal'
    with kb.connect_closing() as conn:
        claim = conn.execute('SELECT notified,wake_accepted FROM kanban_notify_claims WHERE task_id=?', (task.id,)).fetchone()
        assert tuple(claim) == (0, 1)
        assert kb.get_task(conn, task.id).worker_pid == os.getpid()
    adapter.offline = False
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
