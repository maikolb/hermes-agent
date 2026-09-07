"""Coordinator input reaches the Principal through the durable wake boundary."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.platforms.base import Platform, SessionSource
from gateway.wake import (current_notify_receipt, deliver_wake,
                          notify_wake_accepted, record_notify_progress)
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from agent.turn_checkpoint import initialize_agent_turn_checkpoint, TurnCheckpointStore
from tests.gateway.test_kanban_notifier_durable import RealAdapter
from gateway.config import PlatformConfig
from gateway.session import build_session_key


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'board.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    request = {
        'source': {'platform':'telegram', 'chat_id':'test', 'thread_id':'8',
                   'message_id':'123', 'user_id':'owner', 'profile':'default',
                   'transport_profile':'default', 'chat_type':'group'},
        'project': {'board':'default', 'enabled':True, 'profile':'default',
                    'delivery_type':'report', 'workers':2},
        'text':'quero um relatório', 'attachments':[], 'part':'0',
    }
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
        request_id = delivery.receive_request(conn, **request,
            defer_to_principal=True, reply_to_message_id='122')
        receipt = delivery.claim_coordinator_input(conn, request_id)
    assert receipt['kind'] == 'nfos_coordinator_input'
    assert receipt['delivery_id'] == 'nfos-input:'+request_id
    return tmp_path, receipt, {
        'nfos_coordinator_intake': {
            'db_path':str((tmp_path/'board.db').resolve()),
            'request':request, 'reply_to_message_id':'122',
        },
    }


def _stored(receipt):
    with kb.connect_closing(Path(receipt['db_path'])) as conn:
        row = delivery.get_request(conn, receipt['request_id'])
        return row, json.loads(row['payload'])['coordination'], conn.execute(
            'SELECT count(*) FROM tasks').fetchone()[0]


@pytest.mark.asyncio
async def test_wake_preserves_optional_context_without_mutating_caller():
    seen = []

    async def handle(event):
        seen.append(event)

    metadata = {'nfos_coordinator_intake': {'db_path': '/isolated/board.db',
                'request': {'text': 'quero um relatório'}}}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='test', thread_id='8')
    adapter = SimpleNamespace(handle_message=handle)
    await deliver_wake(adapter, text='Coordinator input', source=source, metadata=metadata)
    assert len(seen) == 1
    assert seen[0].internal is True
    assert seen[0].metadata == metadata
    assert seen[0].metadata is not metadata
    assert 'kanban_wake_delivery' not in metadata


def test_destination_pointer_is_not_coordinator_acceptance(pending):
    root, receipt, metadata = pending
    record_notify_progress(receipt, checkpoint_root=str(root/'missing'), session_id='principal')
    assert notify_wake_accepted(receipt) is False
    row, coordination, task_count = _stored(receipt)
    assert row['status'] == 'coordinating' and task_count == 0
    assert not coordination.get('wake_accepted')


def test_different_checkpoint_receipt_does_not_accept_input(pending):
    root, receipt, metadata = pending
    store = TurnCheckpointStore(root/'checkpoints')
    record_notify_progress(receipt, checkpoint_root=str(store.root), session_id='principal')
    store.start_turn('principal', 'another-turn', 'different input', [],
                     routing={'kanban_wake_delivery':dict(receipt, delivery_id='another-delivery')})
    assert notify_wake_accepted(receipt) is False
    assert not _stored(receipt)[1].get('wake_accepted')


@pytest.mark.asyncio
async def test_checkpoint_recovers_lost_coordinator_ack_without_second_delivery(pending, monkeypatch):
    root, receipt, metadata = pending
    import gateway.wake as wake
    real_progress = wake.record_notify_progress

    def crash_after_checkpoint(receipt, **progress):
        if progress.get('wake_accepted'):
            raise RuntimeError('Crash after coordinator checkpoint before ACK')
        return real_progress(receipt, **progress)

    monkeypatch.setattr(wake, 'record_notify_progress', crash_after_checkpoint)
    agent = SimpleNamespace(session_id='principal',
                            _session_db=SimpleNamespace(db_path=root/'state.db'))
    token = current_notify_receipt.set(receipt)
    try:
        with pytest.raises(RuntimeError, match='after coordinator checkpoint'):
            initialize_agent_turn_checkpoint(agent, turn_id='first-turn',
                user_content='Coordinator input', messages=[])
    finally:
        current_notify_receipt.reset(token)
    assert not _stored(receipt)[1].get('wake_accepted')
    monkeypatch.setattr(wake, 'record_notify_progress', real_progress)
    # Reopen through the bridge from a new adapter and serialized receipt. The
    # prior live agent object is not needed to find the persisted checkpoint.
    restored_receipt = json.loads(json.dumps(receipt))
    seen = []

    async def unexpected_delivery(event):
        seen.append(event)

    assert await deliver_wake(SimpleNamespace(handle_message=unexpected_delivery),
        text='Coordinator input', source=SessionSource(platform=Platform.TELEGRAM,
        chat_id='test', thread_id='8'), receipt=restored_receipt, metadata=metadata) is True
    assert seen == []
    assert _stored(receipt)[1]['wake_accepted']
    assert agent._turn_checkpoint_store.load('principal')['turn_id'] == 'first-turn'


@pytest.mark.asyncio
async def test_busy_wake_survives_adapter_replacement_and_acks_only_at_real_checkpoint(pending):
    root, receipt, metadata = pending
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='test', thread_id='8', chat_type='group')
    blocked = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
    blocked._message_handler = lambda event: None
    key = build_session_key(source, group_sessions_per_user=True)
    owner = asyncio.create_task(asyncio.Event().wait())
    blocked._active_sessions[key] = asyncio.Event()
    blocked._session_tasks[key] = owner
    try:
        assert await deliver_wake(blocked, text='Coordinator input', source=source,
            receipt=receipt, metadata=metadata) is False
        assert blocked._pending_messages == {}
        assert not _stored(receipt)[1].get('wake_accepted')
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)

    # A fresh adapter and SQLite connection reconstruct from durable storage.
    with kb.connect_closing(Path(receipt['db_path'])) as conn:
        row = delivery.get_request(conn, receipt['request_id'])
        payload = json.loads(row['payload'])
    restored_context = {'db_path':receipt['db_path'], 'request':{
        field:payload[field] for field in ('source', 'text', 'project', 'attachments')},
        'reply_to_message_id':payload['coordination']['reply_to_message_id']}
    restored_context['request']['part'] = '0'
    restored = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
    restored.config.typing_indicator = False
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(event):
        calls.append(event)
        assert event.metadata['nfos_coordinator_intake'] == restored_context
        assert event.metadata['kanban_wake_delivery'] == receipt
        assert not notify_wake_accepted(receipt)
        agent = SimpleNamespace(session_id='principal',
                                _session_db=SimpleNamespace(db_path=root/'state.db'))
        await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
            turn_id='one-real-turn', user_content=event.text, messages=[])
        entered.set()
        await finish.wait()

    restored._message_handler = handle
    try:
        await deliver_wake(restored, text='Coordinator input', source=source,
            receipt=receipt, metadata={'nfos_coordinator_intake':restored_context})
        await asyncio.wait_for(entered.wait(), 5)
        assert await deliver_wake(restored, text='Coordinator input', source=source,
            receipt=receipt, metadata=metadata) is True
        assert len(calls) == 1
        row, coordination, task_count = _stored(receipt)
        assert row['status'] == 'coordinating' and task_count == 0
        assert coordination['wake_accepted']
    finally:
        tasks = list(restored._session_tasks.values())
        finish.set()
        await asyncio.gather(*tasks)
