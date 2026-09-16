"""A slow Telegram receipt must not hold a newly arriving internal decision."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_kanban_notifier import _make_runner
from tests.gateway.test_kanban_notifier_durable import RealAdapter
from tests.gateway.test_nfos_urgent_principal import waiting, waiting_card


@pytest.mark.parametrize('waiting', [False], indirect=True)
@pytest.mark.parametrize('priority', [0, 100], ids=['normal', 'urgent'])
def test_decision_arrives_after_slow_receipt_started_and_is_consumed_before_transport_released(waiting, priority, tmp_path, monkeypatch):
    normal = waiting[0]
    timing = {}

    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        adapter.config.typing_indicator = False
        normal_started, normal_release = asyncio.Event(), asyncio.Event()
        transport_started, transport_release = asyncio.Event(), asyncio.Event()
        urgent_started, urgent_release = asyncio.Event(), asyncio.Event()
        sends = []
        urgent = None

        async def send_receipt(**kwargs):
            sends.append(kwargs['reply_to'])
            if kwargs['reply_to'] == 'normal':
                timing['transport_started'] = time.monotonic()
                transport_started.set()
                await transport_release.wait()
            return SendResult(success=True, message_id='receipt-'+kwargs['reply_to'])

        adapter._send_with_retry = send_receipt

        async def principal(event):
            row = normal if normal[0].id in event.text else urgent
            assert row is not None
            agent = SimpleNamespace(session_id='principal-slow-transport', _session_db=SimpleNamespace(db_path=tmp_path/'state.db'))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                                    turn_id=row[1], user_content=event.text, messages=[])
            if row is normal:
                normal_started.set()
                await normal_release.wait()
            else:
                timing['principal_admitted'] = time.monotonic()
                urgent_started.set()
            with kb.connect_closing() as conn:
                delivery.resolve_decision(conn, row[1], action='continue', answer='Use the saved evidence', author='Principal')
            if row is not normal:
                timing['resolved'] = time.monotonic()
                await urgent_release.wait()

        adapter._message_handler = principal
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='project', thread_id='8', chat_type='group', profile='default')
        await adapter.handle_message(MessageEvent(text='Review '+normal[0].id, message_type=MessageType.TEXT, source=source, internal=True))
        await asyncio.wait_for(normal_started.wait(), 3)
        runner = _make_runner(adapter)
        monkeypatch.setattr(runner, '_nfos_receipt_profiles', lambda: {'default'})
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: adapter)
        watcher = asyncio.create_task(runner._kanban_notifier_watcher())
        consumer = None
        try:
            await asyncio.wait_for(transport_started.wait(), 10)
            urgent = waiting_card('arriving-decision', priority)
            timing['urgent_created'] = time.monotonic()
            assert not urgent_started.is_set()
            assert all(not flag.is_set() for flag in adapter._active_sessions.values())

            def worker():
                with kb.connect_closing() as conn:
                    result = delivery.wait_decision(conn, urgent[1], timeout=20)
                    timing['worker_consumed'] = time.monotonic()
                    return result

            consumer = asyncio.create_task(asyncio.to_thread(worker))
            timing['principal_released'] = time.monotonic()
            normal_release.set()
            await asyncio.wait_for(urgent_started.wait(), 8)
            assert (await asyncio.wait_for(consumer, 4))['action'] == 'continue'
            assert not transport_release.is_set()
            assert timing['worker_consumed']-timing['principal_released'] < 8
            assert sends.count('normal') == 1
            with kb.connect_closing() as conn:
                rid = delivery.get_workflow(conn, normal[0].id)['request_id']
                assert delivery.get_request(conn, rid)['acknowledged_at'] is None
                assert kb.get_task(conn, urgent[0].id).current_run_id == urgent[0].current_run_id
            timing['transport_released'] = time.monotonic()
            transport_release.set()
            deadline = time.monotonic()+3
            while True:
                with kb.connect_closing() as conn:
                    if delivery.get_request(conn, rid)['acknowledged_at'] is not None:
                        break
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
            assert sends.count('normal') == 1
            (tmp_path/'slow-receipt-timing.json').write_text(json.dumps(timing, indent=2))
        finally:
            runner._running = False
            normal_release.set(); urgent_release.set(); transport_release.set()
            await asyncio.wait_for(watcher, 10)
            await asyncio.gather(*list(adapter._session_tasks.values()))
            await asyncio.gather(*list(getattr(runner, '_background_tasks', set())))
            if consumer is not None:
                await consumer

    asyncio.run(run())


@pytest.mark.parametrize('waiting', [False], indirect=True)
def test_single_receipt_batch_is_tracked_and_cancelled_receipt_remains_retryable(waiting, monkeypatch):
    normal = waiting[0]

    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        started, release = asyncio.Event(), asyncio.Event()
        sends = []

        async def send_receipt(**kwargs):
            sends.append(kwargs)
            started.set()
            await release.wait()
            return SendResult(success=True, message_id='confirmed-receipt')

        adapter._send_with_retry = send_receipt
        runner = _make_runner(adapter)
        monkeypatch.setattr(runner, '_nfos_receipt_profiles', lambda: {'default'})
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: adapter)
        runner._nfos_schedule_receipt_retry()
        task = runner._nfos_receipt_retry_task
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(3):
            runner._nfos_schedule_receipt_retry()
        assert runner._background_tasks == {task}
        assert runner._nfos_receipt_retry_task is task
        assert len(sends) == 1
        # Cancel through the same tracked collection used by gateway shutdown.
        runner._running = False
        for pending in list(runner._background_tasks):
            pending.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not runner._background_tasks
        assert runner._nfos_receipt_retry_task is None
        runner._nfos_schedule_receipt_retry()
        assert not runner._background_tasks
        with kb.connect_closing() as conn:
            rid = delivery.get_workflow(conn, normal[0].id)['request_id']
            row = delivery.get_request(conn, rid)
            assert row['acknowledged_at'] is None
            payload = json.loads(row['payload'])
            assert payload['receipt']['claim_token']
            assert payload['receipt']['attempts'] == 1
            assert payload['receipt']['claim_until'] > time.time()
            # Model lease expiry, not a second simultaneous claim.
            payload['receipt']['claim_until'] = 0
            conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), rid))
            conn.commit()
        release.set()
        runner._running = True
        runner._nfos_schedule_receipt_retry()
        await runner._nfos_receipt_retry_task
        assert len(sends) == 2
        assert sends[0]['content'] == sends[1]['content']
        with kb.connect_closing() as conn:
            row = delivery.get_request(conn, rid)
            assert row['acknowledged_at'] is not None
            payload = json.loads(row['payload'])
            assert payload['receipt']['message_id'] == 'confirmed-receipt'
            assert payload['receipt']['attempts'] == 2

    asyncio.run(run())
