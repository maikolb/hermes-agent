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
