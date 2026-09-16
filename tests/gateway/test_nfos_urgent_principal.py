"""Urgent work gets the next Principal admission, retaining normal work and evidence."""
import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway.config import Platform, PlatformConfig
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RealAdapter


def waiting_card(label, priority):
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(conn,
            source={'platform': 'telegram', 'chat_id': 'project', 'thread_id': '8',
                    'message_id': label, 'chat_type': 'group'},
            text=label, project={'profile': 'default', 'delivery_type': 'report', 'priority': priority})
        request = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
        did = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
                                     question='Qual fonte atende ao critério?', context={})
        return task, did


@pytest.fixture
def waiting(tmp_path, monkeypatch, request):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'board.db'))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {'kanban': {'agent_wake_on_events': True}})
    rows = [waiting_card('normal', 0)]
    if getattr(request, 'param', True):
        rows.append(waiting_card('urgent', 100))
    with kb.connect_closing() as conn:
        for index, (_, did) in enumerate(rows):
            conn.execute('UPDATE nfos_decisions SET created_at=? WHERE id=?', (100+index*100, did))
        conn.commit()
    return rows


def test_pending_decisions_prioritize_newer_urgent_card(waiting):
    normal, urgent = waiting
    with kb.connect_closing() as conn:
        assert [r['id'] for r in delivery.pending_decisions(conn)] == [urgent[1], normal[1]]


def test_real_adapter_admits_urgent_first_and_worker_consumes_without_losing_normal(waiting, tmp_path, monkeypatch):
    normal, urgent = waiting
    started = []

    def wait_worker(did):
        with kb.connect_closing() as conn:
            return delivery.wait_decision(conn, did, timeout=4)

    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        adapter.config.typing_indicator = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def principal(event):
            task, did = next(row for row in waiting if row[0].id in event.text)
            started.append(task.id)
            agent = SimpleNamespace(session_id='principal', _session_db=SimpleNamespace(db_path=tmp_path/'state.db'))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                                    turn_id=did, user_content=event.text, messages=[])
            entered.set()
            with kb.connect_closing() as conn:
                delivery.resolve_decision(conn, did, action='continue', answer='Use the saved source', author='Principal')
            await release.wait()

        adapter._message_handler = principal
        runner = _make_runner(adapter)
        consumer = asyncio.create_task(asyncio.to_thread(wait_worker, urgent[1]))
        try:
            await _run_one_notifier_tick(monkeypatch, runner)
            await asyncio.wait_for(entered.wait(), 3)
            assert started == [urgent[0].id], 'A normal wake occupied the Principal before the urgent decision'
            response = await asyncio.wait_for(consumer, 3)
            assert response['status'] == 'resolved' and response['action'] == 'continue'
            with kb.connect_closing() as conn:
                assert delivery.get_decision(conn, normal[1])['status'] == 'pending'
                assert kb.get_task(conn, urgent[0].id).current_run_id == urgent[0].current_run_id
        finally:
            tasks = list(adapter._session_tasks.values())
            release.set()
            await asyncio.gather(*tasks)
            await consumer
        runner._running = True
        await _run_one_notifier_tick(monkeypatch, runner)
        tasks = list(adapter._session_tasks.values())
        await asyncio.gather(*tasks)
        assert started == [urgent[0].id, normal[0].id]
        with kb.connect_closing() as conn:
            assert delivery.get_decision(conn, normal[1])['status'] == 'resolved'
            assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2

    asyncio.run(run())


@pytest.mark.parametrize('waiting', [False], indirect=True)
def test_urgent_arriving_during_active_principal_retries_after_idle_with_real_polling(waiting, tmp_path):
    """Real default 5s notifier and 1s worker polling; no sleep/clock substitution."""
    normal = waiting[0]
    timing = {}

    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        adapter.config.typing_indicator = False
        normal_entered, release_normal, urgent_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        urgent = None

        async def principal(event):
            row = normal if normal[0].id in event.text else urgent
            assert row is not None
            agent = SimpleNamespace(session_id='principal-live-poll', _session_db=SimpleNamespace(db_path=tmp_path/'state.db'))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                                    turn_id=row[1], user_content=event.text, messages=[])
            if row is normal:
                normal_entered.set()
                await release_normal.wait()
            else:
                timing['admitted'] = time.monotonic()
                urgent_entered.set()
            with kb.connect_closing() as conn:
                delivery.resolve_decision(conn, row[1], action='continue', answer='Use the saved source', author='Principal')
            if row is not normal:
                timing['resolved'] = time.monotonic()

        adapter._message_handler = principal
        runner = _make_runner(adapter)
        watcher = asyncio.create_task(runner._kanban_notifier_watcher())
        consumer = None
        try:
            await asyncio.wait_for(normal_entered.wait(), 10)
            urgent = waiting_card('arriving-urgent', 100)
            timing['requested'] = time.monotonic()
            deadline = time.monotonic()+8
            while True:
                with kb.connect_closing() as conn:
                    claim = conn.execute('SELECT wake_accepted FROM kanban_notify_claims WHERE task_id=?', (urgent[0].id,)).fetchone()
                if claim is not None:
                    assert claim[0] == 0
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
            assert not urgent_entered.is_set()
            assert all(not interrupted.is_set() for interrupted in adapter._active_sessions.values())

            def worker():
                with kb.connect_closing() as conn:
                    response = delivery.wait_decision(conn, urgent[1], timeout=15)
                    timing['consumed'] = time.monotonic()
                    return response

            consumer = asyncio.create_task(asyncio.to_thread(worker))
            timing['idle_released'] = time.monotonic()
            release_normal.set()
            await asyncio.wait_for(urgent_entered.wait(), 8)
            assert (await asyncio.wait_for(consumer, 4))['action'] == 'continue'
            assert timing['consumed']-timing['idle_released'] < 8
            with kb.connect_closing() as conn:
                assert kb.get_task(conn, urgent[0].id).current_run_id == urgent[0].current_run_id
            (tmp_path/'urgent-timing.json').write_text(json.dumps(timing, indent=2))
        finally:
            release_normal.set()
            runner._running = False
            await asyncio.wait_for(watcher, 8)
            await asyncio.gather(*list(adapter._session_tasks.values()))
            if consumer is not None:
                await consumer

    asyncio.run(run())
