"""Exercise the real notifier, adapter admission and checkpoint receipt boundary."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.wake import (deliver_wake, current_notify_receipt, notify_wake_accepted,
                          record_notify_progress)
from agent.turn_checkpoint import initialize_agent_turn_checkpoint, TurnCheckpointStore
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"agent_wake_on_events": True}})
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="A report", assignee="default", requires_repo=False)
        sub = dict(task_id=tid, platform="telegram", chat_id="test", thread_id="")
        kb.add_notify_sub(conn, **sub, delivery_mode="notify+wake")
        kb.complete_task(conn, tid, summary="Done")
    return sub


def reserve(sub):
    with kb.connect_closing() as conn:
        old, cursor, events = kb.claim_unseen_events_for_sub(conn, **sub, claim_token="test")
        row = kb.get_notify_claim(conn, **sub)
        return dict(db_path=conn.execute("PRAGMA database_list").fetchone()[2],
                    delivery_id=row["delivery_id"])


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []
        self.fail = True

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        if self.fail:
            raise RuntimeError("delivery failed")
        record_notify_progress(event.metadata["kanban_wake_delivery"], wake_accepted=True)


def test_notify_and_wake_have_independent_acknowledgement(pending, monkeypatch):
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    with kb.connect_closing() as conn:
        assert kb.unseen_events_for_sub(conn, **pending)[1]
        assert kb.get_notify_claim(conn, **pending)["notified"] == 1
    adapter.fail = False
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 2
    with kb.connect_closing() as conn:
        assert not kb.unseen_events_for_sub(conn, **pending)[1]


def test_restart_does_not_filter_out_unacknowledged_event(pending, monkeypatch):
    adapter = RecordingAdapter()
    adapter.fail = False
    runner = _make_runner(adapter)
    runner._gateway_started_at = 9999999999
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1


def test_twelve_failures_keep_pending_delivery(pending, monkeypatch):
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._kanban_sub_fail_counts = {tuple(pending[k] for k in
                                          ("task_id", "platform", "chat_id", "thread_id")): 12}
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    with kb.connect_closing() as conn:
        assert kb.list_notify_subs(conn, pending["task_id"])
        assert kb.unseen_events_for_sub(conn, **pending)[1]


def test_lost_checkpoint_ack_recovers_without_second_handoff(pending, tmp_path, monkeypatch):
    receipt = reserve(pending)
    agent = SimpleNamespace(session_id="creator", _session_db=SimpleNamespace(db_path=tmp_path / "state.db"))
    import gateway.wake as wake
    original = wake.record_notify_progress
    def interrupted(receipt, **progress):
        if progress.get("wake_accepted"):
            raise RuntimeError("crash after checkpoint before ACK")
        return original(receipt, **progress)
    monkeypatch.setattr(wake, "record_notify_progress", interrupted)
    token = current_notify_receipt.set(receipt)
    try:
        with pytest.raises(RuntimeError, match="crash after checkpoint"):
            initialize_agent_turn_checkpoint(agent, turn_id="original-turn", user_content="continue", messages=[])
    finally:
        current_notify_receipt.reset(token)
    monkeypatch.setattr(wake, "record_notify_progress", original)
    adapter = RecordingAdapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", chat_type="group")
    assert asyncio.run(deliver_wake(adapter, text="continue", source=source, receipt=receipt)) is True
    assert adapter.handled == []
    assert agent._turn_checkpoint_store.load("creator")["turn_id"] == "original-turn"


def test_next_checkpoint_preserves_prior_lost_ack(pending, tmp_path, monkeypatch):
    receipt = reserve(pending)
    store = TurnCheckpointStore(tmp_path / "sessions" / "turn-checkpoints")
    record_notify_progress(receipt, checkpoint_root=str(store.root), session_id="creator")
    store.start_turn("creator", "first", "wake", [], routing={"kanban_wake_delivery": receipt})
    store.start_turn("creator", "second", "new human request", [])
    assert notify_wake_accepted(receipt)


def test_destination_pointer_alone_is_not_acceptance(pending, tmp_path):
    receipt = reserve(pending)
    record_notify_progress(receipt, checkpoint_root=str(tmp_path / "empty"), session_id="creator")
    assert not notify_wake_accepted(receipt)


class RealAdapter(BasePlatformAdapter):
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, chat_id, content, **kwargs): pass
    async def get_chat_info(self, chat_id): return {}


def test_busy_adapter_keeps_wake_in_db_instead_of_memory(pending):
    receipt = reserve(pending)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", chat_type="group")
    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        async def handler(event): pass
        adapter._message_handler = handler
        key = build_session_key(source, group_sessions_per_user=True)
        owner = asyncio.create_task(asyncio.Event().wait())
        adapter._active_sessions[key] = asyncio.Event()
        adapter._session_tasks[key] = owner
        try:
            assert await deliver_wake(adapter, text="wake", source=source, receipt=receipt) is False
            assert not adapter._pending_messages
            assert adapter._session_tasks[key] is owner
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
    asyncio.run(run())


def test_real_adapter_accepts_checkpoint_once_before_turn_finishes(pending, tmp_path):
    receipt = reserve(pending)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", chat_type="group")
    calls = []
    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        adapter.config.typing_indicator = False
        entered, finish = asyncio.Event(), asyncio.Event()
        async def handler(event):
            calls.append(event)
            agent = SimpleNamespace(session_id="creator", _session_db=SimpleNamespace(db_path=tmp_path / "state.db"))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                                    turn_id="single-turn", user_content=event.text, messages=[])
            entered.set()
            await finish.wait()
        adapter._message_handler = handler
        await deliver_wake(adapter, text="wake", source=source, receipt=receipt)
        await asyncio.wait_for(entered.wait(), 5)
        assert await deliver_wake(adapter, text="wake", source=source, receipt=receipt) is True
        assert len(calls) == 1
        tasks = list(adapter._session_tasks.values())
        finish.set()
        await asyncio.gather(*tasks)
    asyncio.run(run())
