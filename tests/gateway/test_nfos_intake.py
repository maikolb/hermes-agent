"""Telegram intake uses the real board database before acknowledging."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.platforms.base import MessageEvent,Platform,SessionSource
from hermes_cli import kanban_db as kb


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    runner=GatewayKanbanWatchersMixin()
    config={'kanban':{'delivery':{'projects':{'default':{'enabled':True,'profile':'default','delivery_type':'report','workers':2}}}}}
    runner._kanban_parallel_dispatch_config=lambda source:config
    runner._resolve_project_context_for_message=lambda event,source:(SimpleNamespace(board_slug='default',project_id='',is_management=False),None)
    runner._active_profile_name=lambda:'default'
    runner._reply_anchor_for_event=lambda event:event.message_id
    runner._thread_metadata_for_source=lambda source,anchor:{'thread_id':source.thread_id}
    adapter=SimpleNamespace(_send_with_retry=AsyncMock())
    runner._adapter_for_source=lambda source:adapter
    runner._handle_kanban_command=AsyncMock(side_effect=AssertionError('Legacy card creation was invoked'))
    event=MessageEvent(text='Analise os dados e entregue um relatório.',message_id='123',source=SessionSource(
        platform=Platform.TELEGRAM,chat_id='-1000',thread_id='8',chat_type='group',user_id='owner'))
    return runner,event,adapter,tmp_path


@pytest.mark.asyncio
async def test_idle_intake_persists_request_before_ack_without_principal_creating_card(setup):
    runner,event,adapter,root=setup
    reply=await runner._nfos_receive(event)
    assert 'registrado' in reply.lower()
    with kb.connect_closing() as conn:
        request=conn.execute('select * from nfos_requests').fetchone()
        assert json.loads(request['payload'])['text']==event.text
        assert conn.execute('select count(*) from tasks').fetchone()[0]==0
    assert await runner._nfos_receive(event)==reply
    with kb.connect_closing() as conn:
        assert conn.execute('select count(*) from nfos_requests').fetchone()[0]==1


@pytest.mark.asyncio
async def test_telegram_caption_pins_only_flagged_worker(setup, monkeypatch):
    from hermes_cli import nfos_delivery as delivery, nfos_principal_review as review
    runner,event,adapter,root=setup
    monkeypatch.setattr(review,'settings',lambda:{'deepseek_worker_trial':True,
        'worker_model':'gpt-5.6-luna','worker_provider':'openai-codex','worker_reasoning_effort':'high'})
    event.text='#deepseek Analise os dados e entregue um relatório.'
    image=root/'request.png';image.write_bytes(b'synthetic-intake-attachment')
    event.media_urls=[str(image)];event.media_types=['image/png']
    await runner._nfos_receive(event)
    with kb.connect_closing() as conn:
        row=conn.execute('SELECT * FROM nfos_requests').fetchone()
        payload=json.loads(row['payload'])
        assert payload['text']==event.text
        assert payload['attachments'][0]['mime_type']=='image/png'
        assert payload['project']['provider']=='opencode-go'
        reservation=delivery.reserve_request(conn,capacity=2)
        import os
        task=delivery.bootstrap_card(conn,row['id'],reservation['claim_token'],pid=os.getpid())
        assert review.worker_model_args(task)==[
            '-m','deepseek-v4.1-flash','--provider','opencode-go','--reasoning','max']
    event.message_id='124';event.text='Analise outro relatório.';event.media_urls=[];event.media_types=[]
    await runner._nfos_receive(event)
    with kb.connect_closing() as conn:
        payload=json.loads(conn.execute('SELECT payload FROM nfos_requests WHERE id<>?',(row['id'],)).fetchone()[0])
        assert 'model' not in payload['project']


@pytest.mark.asyncio
async def test_busy_intake_uses_same_path_and_survives_ack_failure(setup):
    runner,event,adapter,root=setup
    adapter._send_with_retry.side_effect=ConnectionError('Telegram connection lost')
    assert await runner._kanban_parallel_dispatch_busy_message(event,'principal') is True
    runner._handle_kanban_command.assert_not_called()
    with kb.connect_closing() as conn:
        assert conn.execute('select count(*) from nfos_requests').fetchone()[0]==1


@pytest.mark.asyncio
async def test_media_original_survives_cache_removal(setup):
    runner,event,adapter,root=setup
    source=root/'voice.ogg';source.write_bytes(b'original-media-bytes')
    event.text='';event.media_urls=[str(source)];event.media_types=['audio/ogg']
    assert await runner._nfos_receive(event)
    source.unlink()
    with kb.connect_closing() as conn:
        payload=json.loads(conn.execute('select payload from nfos_requests').fetchone()[0])
    from pathlib import Path
    assert Path(payload['attachments'][0]['original']).read_bytes()==b'original-media-bytes'


@pytest.mark.asyncio
async def test_status_and_replies_stay_with_principal(setup):
    runner,event,adapter,root=setup
    event.text='Qual é o status?'
    assert await runner._nfos_receive(event) is not None
    event.text='Corrija o que falei antes';event.reply_to_message_id='122';event.message_id='124'
    assert await runner._nfos_receive(event) is not None
    with kb.connect_closing() as conn:
        rows=conn.execute('SELECT status,task_id,worker_pid FROM nfos_requests').fetchall()
        assert len(rows)==2
        assert all(row['status']=='coordinating' and row['task_id'] is None and row['worker_pid'] is None for row in rows)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    adapter._send_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_other_project_does_not_use_pilot_path(setup):
    runner,event,adapter,root=setup
    runner._resolve_project_context_for_message=lambda event,source:(SimpleNamespace(board_slug='other',is_management=False),None)
    assert await runner._nfos_receive(event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('board,topic', [('concursa-ai','41'),('recuperacli','6'),('dovcrm','4')])
async def test_each_enabled_project_uses_its_own_durable_intake(setup,board,topic):
    runner,event,adapter,root=setup
    projects={name:{'enabled':True,'profile':'default','delivery_type':'report','workers':2}
              for name in ('concursa-ai','recuperacli','dovcrm')}
    runner._kanban_parallel_dispatch_config=lambda source:{'kanban':{'delivery':{'projects':projects}}}
    runner._resolve_project_context_for_message=lambda event,source:(
        SimpleNamespace(board_slug=board,project_id=board,is_management=False),None)
    event.source.thread_id=topic
    assert await runner._nfos_receive(event)
    assert await runner._nfos_receive(event)
    with kb.connect_closing() as conn:
        rows=conn.execute('SELECT payload FROM nfos_requests').fetchall()
        assert len(rows)==1
        payload=json.loads(rows[0]['payload'])
        assert payload['project']['board']==board
        assert payload['project']['project_id']==board
        assert payload['source']['thread_id']==topic
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    runner._handle_kanban_command.assert_not_called()
