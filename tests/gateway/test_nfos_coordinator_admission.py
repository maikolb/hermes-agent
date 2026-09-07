"""An accepted background turn must keep its input claim during slow startup."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, Platform, SendResult, SessionSource
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_kanban_notifier_durable import RealAdapter
from tests.gateway.test_nfos_receipts import Runner


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    clock = [1000.0]
    monkeypatch.setattr(delivery.time, 'time', lambda: clock[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-1000',
        thread_id='8', chat_type='group', user_id='owner', profile='default')
    return tmp_path, clock, source


def stored():
    with kb.connect_closing() as conn:
        row = dict(conn.execute('SELECT * FROM nfos_requests').fetchone())
    row['payload'] = json.loads(row['payload'])
    return row


@pytest.mark.asyncio
async def test_slow_real_adapter_startup_does_not_rotate_live_input_claim(inbox):
    root, clock, source = inbox
    adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
    adapter.config.typing_indicator = False
    runner = Runner(adapter=adapter)
    entered, release, checkpointed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen, failures = [], []
    async def handle(event):
        seen.append(event)
        entered.set()
        await release.wait()
        try:
            agent = SimpleNamespace(session_id='principal',
                _session_db=SimpleNamespace(db_path=root/'state.db'))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                turn_id='slow-start', user_content=event.text, messages=[])
            checkpointed.set()
        except Exception as exc:
            failures.append(type(exc).__name__)
    adapter._message_handler = handle
    event = MessageEvent(text='Use a conta autorizada para conferir a entrega.',
        message_id='13170', reply_to_message_id='13168', source=source)
    try:
        await runner._nfos_receive(event)
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.gather(*list(runner._background_tasks))
        original = stored()['payload']['coordination']['claim_token']
        for _ in range(3):
            clock[0] += 70
            await runner._nfos_retry_coordinator_inputs()
            await asyncio.gather(*list(runner._background_tasks))
            assert stored()['payload']['coordination']['claim_token'] == original
        assert len(seen) == 1
        release.set()
        await asyncio.wait_for(checkpointed.wait(), 5)
        assert failures == []
        assert stored()['payload']['coordination']['wake_accepted'] is True
    finally:
        tasks = list(adapter._session_tasks.values())
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_adapter_failed_startup_can_retry_same_saved_request(inbox):
    root, clock, source = inbox
    adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
    adapter.config.typing_indicator = False
    runner = Runner(adapter=adapter)
    calls = []
    async def handle(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError('startup failed before checkpoint')
        agent = SimpleNamespace(session_id='principal',
            _session_db=SimpleNamespace(db_path=root/'state.db'))
        await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
            turn_id='recovered-start', user_content=event.text, messages=[])
    adapter._message_handler = handle
    event = MessageEvent(text='Continue a conferência.', message_id='13170',
        reply_to_message_id='13168', source=source)
    await runner._nfos_receive(event)
    await asyncio.gather(*list(runner._background_tasks))
    await asyncio.gather(*list(adapter._session_tasks.values()))
    before = stored()
    assert not before['payload']['coordination'].get('wake_accepted')
    clock[0] += 70
    await runner._nfos_retry_coordinator_inputs()
    await asyncio.gather(*list(runner._background_tasks))
    await asyncio.gather(*list(adapter._session_tasks.values()))
    after = stored()
    assert after['id'] == before['id']
    assert after['payload']['coordination']['wake_accepted'] is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_historical_replay_without_platform_id_does_not_send_false_reply(inbox):
    root, clock, source = inbox
    adapter = SimpleNamespace(_send_with_retry=AsyncMock(
        return_value=SendResult(success=True, message_id='999')))
    runner = Runner(adapter=adapter)
    with kb.connect_closing() as conn:
        delivery.receive_request(conn, source={
            'platform':'telegram', 'chat_id':'-1000', 'thread_id':'8',
            'message_id':'retained-session-row:304858', 'profile':'default',
            'message_identity_kind':'retained-session-row',
            'original_platform_message_id':None}, text='Retained task', project={})
    await runner._nfos_retry_receipts()
    adapter._send_with_retry.assert_not_awaited()
    row = stored()
    assert row['acknowledged_at'] is None
    assert row['payload']['source']['message_id'] == 'retained-session-row:304858'
