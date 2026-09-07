"""Persisted intake receipts use the real SQLite store and one egress path."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import kanban_watchers as watchers
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.platforms.base import MessageEvent, Platform, SendResult, SessionSource
from hermes_cli import kanban_db as kb, nfos_delivery as delivery


class Runner(watchers.GatewayKanbanWatchersMixin, GatewayAuthorizationMixin):
    def __init__(self, profile='default', adapter=None):
        self.profile=profile
        self.adapters={Platform.TELEGRAM:adapter} if adapter else {}
        self._profile_adapters={}
        self._running=True

    def _active_profile_name(self):return self.profile
    def _recover_telegram_topic_thread_id(self, source):return None
    def _resolve_project_context_for_message(self,event,source):
        return SimpleNamespace(board_slug='default',project_id='pilot',is_management=False),None
    def _kanban_parallel_dispatch_config(self,source):
        return {'kanban':{'delivery':{'projects':{'default':{'enabled':True,'profile':'worker','delivery_type':'report','workers':2}}}}}
    def _reply_anchor_for_event(self,event):return event.message_id
    def _thread_metadata_for_source(self,source,anchor=None):
        return {'thread_id':source.thread_id,'reply_to_message_id':anchor}


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    clock=[1000.0]
    monkeypatch.setattr(watchers.time,'time',lambda:clock[0])
    adapter=SimpleNamespace(_send_with_retry=AsyncMock(return_value=SendResult(success=True,message_id='sent-1')))
    runner=Runner(adapter=adapter)
    event=MessageEvent(text='Analise os dados e entregue um relatório.',message_id='123',source=SessionSource(
        platform=Platform.TELEGRAM,chat_id='-1000',thread_id='8',chat_type='group',user_id='owner',profile='default'))
    return runner,event,adapter,clock


def request():
    with kb.connect_closing() as conn:
        row=dict(conn.execute('SELECT * FROM nfos_requests').fetchone())
        row['payload']=json.loads(row['payload'])
        row['task_count']=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        return row


@pytest.mark.asyncio
async def test_idle_handler_delivers_once_and_does_not_return_a_second_response(env):
    from gateway.run import GatewayRunner
    runner,event,adapter,clock=env
    result=await GatewayRunner._handle_message_with_agent(runner,event,event.source,'topic',1)
    assert result is None
    row=request()
    assert row['task_count']==0
    assert row['acknowledged_at']==1000
    assert row['payload']['receipt']['message_id']=='sent-1'
    assert row['id'] in adapter._send_with_retry.call_args.kwargs['content']
    assert adapter._send_with_retry.call_args.kwargs['metadata']['thread_id']=='8'
    assert adapter._send_with_retry.call_args.kwargs['reply_to']=='123'
    await runner._nfos_receive(event)
    await runner._nfos_retry_receipts()
    assert adapter._send_with_retry.await_count==1


@pytest.mark.asyncio
async def test_busy_failure_retries_after_restart_without_a_card_or_subscription(env):
    runner,event,adapter,clock=env
    adapter._send_with_retry.side_effect=ConnectionError('lost connection')
    assert await runner._kanban_parallel_dispatch_busy_message(event,'topic') is True
    before=request()
    assert before['acknowledged_at'] is None
    assert before['payload']['receipt']['attempts']==1
    assert before['payload']['receipt']['next_attempt_at']>clock[0]
    adapter._send_with_retry.side_effect=None
    restored=Runner(adapter=adapter)
    await restored._nfos_retry_receipts()
    assert adapter._send_with_retry.await_count==1
    clock[0]=before['payload']['receipt']['next_attempt_at']+1
    await restored._nfos_retry_receipts()
    after=request()
    assert after['id']==before['id'] and after['task_count']==0
    assert after['acknowledged_at']==int(clock[0])
    assert adapter._send_with_retry.await_count==2
    assert adapter._send_with_retry.call_args_list[0].kwargs['content']==adapter._send_with_retry.call_args_list[1].kwargs['content']


@pytest.mark.asyncio
async def test_returned_transport_failure_is_not_acknowledged(env):
    runner,event,adapter,clock=env
    adapter._send_with_retry.return_value=SendResult(success=False,error='response unknown',retry_after=90)
    await runner._nfos_receive(event)
    row=request()
    assert row['acknowledged_at'] is None
    assert row['payload']['receipt']['next_attempt_at']>=clock[0]+90
    assert adapter._send_with_retry.call_args.kwargs['max_retries']==0
    assert adapter._send_with_retry.call_args.kwargs['allow_content_fallback'] is False


@pytest.mark.asyncio
async def test_inflight_claim_survives_cancel_and_prevents_parallel_receipt(env):
    runner,event,adapter,clock=env
    started=asyncio.Event()
    async def waiting(**kwargs):
        started.set()
        await asyncio.Event().wait()
    adapter._send_with_retry.side_effect=waiting
    first=asyncio.create_task(runner._nfos_receive(event))
    await asyncio.wait_for(started.wait(),5)
    other=Runner(adapter=adapter)
    await other._nfos_retry_receipts()
    assert adapter._send_with_retry.await_count==1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    row=request()
    assert row['acknowledged_at'] is None and row['payload']['receipt']['claim_until']>clock[0]
    adapter._send_with_retry.side_effect=None
    await other._nfos_retry_receipts()
    assert adapter._send_with_retry.await_count==1
    clock[0]=row['payload']['receipt']['claim_until']+1
    await other._nfos_retry_receipts()
    assert request()['acknowledged_at']==int(clock[0])
    assert request()['task_count']==0


@pytest.mark.asyncio
async def test_secondary_profile_never_uses_default_bot_and_test_source_never_sends(env):
    runner,event,adapter,clock=env
    event.source.profile='secondary'
    await runner._nfos_receive(event)
    assert request()['acknowledged_at'] is None
    adapter._send_with_retry.assert_not_awaited()
    with kb.connect_closing() as conn:
        delivery.receive_request(conn,source={'platform':'test','chat_id':'-1000','thread_id':'8','message_id':'test','profile':'default'},text='Test only',project={})
    secondary_adapter=SimpleNamespace(_send_with_retry=AsyncMock(return_value=SendResult(success=True,message_id='secondary-1')))
    restored=Runner('secondary',secondary_adapter)
    await restored._nfos_retry_receipts()
    assert secondary_adapter._send_with_retry.await_count==1
    await runner._nfos_retry_receipts()
    adapter._send_with_retry.assert_not_awaited()
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM nfos_requests WHERE acknowledged_at IS NULL").fetchone()[0]==1


@pytest.mark.asyncio
async def test_transport_owner_is_persisted_when_runtime_profile_is_routed(env):
    runner,event,adapter,clock=env
    # The primary bot transported an event routed to a different runtime profile.
    event.source.profile='secondary'
    # SimpleNamespace cannot be weak-referenced; the registered reference is callable.
    event.source._transport_adapter_ref=lambda:adapter
    adapter._send_with_retry.return_value=SendResult(success=False,error='offline')
    await runner._nfos_receive(event)
    row=request()
    assert row['payload']['source']['transport_profile']=='default'
    adapter._send_with_retry.return_value=SendResult(success=True,message_id='sent-after-restart')
    clock[0]=row['payload']['receipt']['next_attempt_at']+1
    await Runner(adapter=adapter)._nfos_retry_receipts()
    assert request()['acknowledged_at']==int(clock[0])


@pytest.mark.asyncio
async def test_additional_request_receipt_identifies_its_origin(env):
    runner,event,adapter,clock=env
    with kb.connect_closing() as conn:
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'-1000','thread_id':'8','message_id':'123','profile':'default'},text='Additional task',project={},part='attachment-task-1')
        row=conn.execute('SELECT payload FROM nfos_requests WHERE id=?',(rid,)).fetchone()
        payload=json.loads(row[0]);payload['origin']={'task_id':'t_parent','request_id':'req_parent','item_key':'attachment-task-1'}
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?',(json.dumps(payload),rid));conn.commit()
    await runner._nfos_retry_receipts()
    content=adapter._send_with_retry.call_args.kwargs['content']
    assert 'adicional' in content.lower() and 't_parent' in content and rid in content


@pytest.mark.asyncio
async def test_lost_response_can_repeat_receipt_but_cannot_repeat_intake(env):
    runner,event,adapter,clock=env
    delivered=[]
    async def response_lost(**kwargs):
        delivered.append(kwargs['content'])
        raise TimeoutError('Telegram accepted the message but its response was lost')
    adapter._send_with_retry.side_effect=response_lost
    await runner._nfos_receive(event)
    row=request()
    assert row['acknowledged_at'] is None
    async def confirmed(**kwargs):
        delivered.append(kwargs['content'])
        return SendResult(success=True,message_id='second-receipt')
    adapter._send_with_retry.side_effect=confirmed
    clock[0]=row['payload']['receipt']['next_attempt_at']+1
    await Runner(adapter=adapter)._nfos_receive(event)
    assert delivered==[delivered[0],delivered[0]]
    with kb.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    assert request()['acknowledged_at']==int(clock[0])


@pytest.mark.asyncio
async def test_existing_notifier_recovers_receipts_at_startup_without_subscriptions(env,monkeypatch):
    runner,event,adapter,clock=env
    adapter._send_with_retry.return_value=SendResult(success=False,error='offline')
    await runner._nfos_receive(event)
    clock[0]=request()['payload']['receipt']['next_attempt_at']+1
    adapter._send_with_retry.return_value=SendResult(success=True,message_id='startup-receipt')
    restored=Runner(adapter=adapter)
    original=restored._nfos_retry_receipts
    async def one_tick():
        await original()
        restored._running=False
    restored._nfos_retry_receipts=one_tick
    restored._kanban_board_display_targets=lambda profiles:{}
    restored._kanban_refresh_worker_focus=AsyncMock()
    monkeypatch.setattr(watchers.asyncio,'sleep',AsyncMock())
    await restored._kanban_notifier_watcher(interval=0)
    assert request()['acknowledged_at']==int(clock[0])
    assert request()['payload']['receipt']['message_id']=='startup-receipt'
    assert adapter._send_with_retry.await_count==2


@pytest.mark.asyncio
async def test_receipts_preserve_real_additional_task_lineage_and_split_idempotency(env):
    runner,event,adapter,clock=env
    runner._kanban_parallel_dispatch_config=lambda source: {
        'kanban':{'delivery':{'projects':{'default':{
            'enabled':True,'profile':'default','delivery_type':'report','workers':2}}}}}
    await runner._nfos_receive(event)
    with kb.connect_closing() as conn:
        parent=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,parent['id'],parent['claim_token'],pid=os.getpid())
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,kind='additional_tasks',
            question='Despachar item independente?',context={'primary_task':event.text,
                'tasks':[{'key':'text:item-2','text':'Conferir duplicidades.','source_ref':'Pedido original, item 2'}]})
        delivery.resolve_decision(conn,decision,action='continue',answer='Item conferido.',author='Principal')
        child=dict(conn.execute('SELECT * FROM nfos_requests WHERE id<>?',(parent['id'],)).fetchone())
        original=json.loads(child['payload'])
    adapter._send_with_retry.reset_mock()
    await runner._nfos_retry_receipts()
    with kb.connect_closing() as conn:
        acknowledged=delivery.get_request(conn,child['id'])
        saved=json.loads(acknowledged['payload'])
        assert acknowledged['acknowledged_at']==int(clock[0])
        assert saved['origin']==original['origin']
        assert saved['source']==original['source']
        assert saved['attachments']==original['attachments']
        delivery.resolve_decision(conn,decision,action='continue',answer='Item conferido.',author='Principal')
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    await runner._nfos_retry_receipts()
    assert adapter._send_with_retry.await_count==1
    assert task.id in adapter._send_with_retry.call_args.kwargs['content']
