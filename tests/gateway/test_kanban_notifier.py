import asyncio
import sqlite3
import time
from pathlib import Path


from gateway.config import Platform
from gateway.kanban_watchers import (
    _acquire_singleton_lock,
    _render_kanban_worker_focus,
    _release_singleton_lock,
    _resolve_agent_wake_on_events,
    _resolve_worker_focus_handoff,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


class EditableRecordingAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.edited = []
        self.deleted = []
        self._message_seq = 0

    async def send(self, chat_id, text, metadata=None):
        from gateway.platforms.base import SendResult

        self._message_seq += 1
        message_id = str(self._message_seq)
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "metadata": metadata or {},
            "message_id": message_id,
        })
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        from gateway.platforms.base import SendResult

        self.edited.append({
            "chat_id": chat_id,
            "message_id": str(message_id),
            "content": content,
        })
        return SendResult(success=True, message_id=str(message_id))

    async def delete_message(self, chat_id, message_id, **kwargs):
        from gateway.platforms.base import SendResult

        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        return SendResult(success=True, message_id=str(message_id))


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
    # Existing tests create events after a logical boot at epoch zero. Tests
    # for startup backlog suppression override this explicitly.
    runner._gateway_started_at = 0.0
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def test_claimed_task_notifies_only_after_material_start(tmp_path, monkeypatch):
    db_path = tmp_path / "claimed-start.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="material start", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
        )
        assert kb.claim_task(conn, tid, claimer="worker:1") is not None
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert "started" in adapter.sent[0]["text"].lower()


def test_retry_chain_converges_to_one_final_notification(tmp_path, monkeypatch):
    db_path = tmp_path / "retry-chain.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry chain", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="claimed")
        kb._append_event(conn, tid, kind="timed_out")
        kb._append_event(conn, tid, kind="claimed")
        kb._append_event(conn, tid, kind="gave_up")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "gave up" in adapter.sent[0]["text"].lower()
    assert "started" not in adapter.sent[0]["text"].lower()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_delivers_dm_metadata_without_waking_agent(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert adapter.handled == []


def test_notifier_suppresses_pre_start_backlog_and_advances_cursor(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "pre-start-backlog.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="historical completion", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="historical result")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._gateway_started_at = time.time() + 60
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_terminal_events(tid) == []


def test_agent_wake_config_requires_literal_true():
    assert _resolve_agent_wake_on_events(lambda: {}) is False
    assert _resolve_agent_wake_on_events(
        lambda: {"kanban": {"agent_wake_on_events": "true"}}
    ) is False
    assert _resolve_agent_wake_on_events(
        lambda: {"kanban": {"agent_wake_on_events": True}}
    ) is True

    def _broken_config():
        raise RuntimeError("unavailable")

    assert _resolve_agent_wake_on_events(_broken_config) is False


def test_worker_focus_handoff_config_requires_literal_true():
    assert _resolve_worker_focus_handoff(lambda: {}) is False
    assert _resolve_worker_focus_handoff(
        lambda: {"kanban": {"worker_focus_handoff": "true"}}
    ) is False
    assert _resolve_worker_focus_handoff(
        lambda: {"kanban": {"worker_focus_handoff": True}}
    ) is True


def test_worker_focus_counter_adds_advances_and_stops_at_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "worker-focus-counter.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_focus_handoff": True}},
    )
    kb.init_db()
    conn = kb.connect()
    try:
        first = kb.create_task(conn, title="first worker", assignee="worker-a")
        second = kb.create_task(conn, title="second worker", assignee="worker-b")
        for task_id in (first, second):
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="chat-1",
                thread_id="topic-7",
                chat_type="group",
            )
            assert kb.claim_task(
                conn, task_id, claimer=f"worker:{task_id}"
            ) is not None
        now = int(time.time())
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (now - 120, first)
        )
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (now - 60, second)
        )
        conn.commit()
    finally:
        conn.close()

    adapter = EditableRecordingAdapter()
    runner = _make_runner(adapter)
    runner._is_session_running = lambda _key: True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert sum(len(bucket) for bucket in runner._kanban_worker_focus_active.values()) == 2
    assert not any("Now following worker" in item["text"] for item in adapter.sent)

    runner._running = True
    runner._is_session_running = lambda _key: False
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    focus_messages = [
        item for item in adapter.sent if "Now following worker" in item["text"]
    ]
    assert len(focus_messages) == 1
    assert "worker 1/2" in focus_messages[0]["text"]
    assert "first worker" in focus_messages[0]["text"]
    focus_message_id = focus_messages[0]["message_id"]

    conn = kb.connect()
    try:
        kb.complete_task(conn, first, summary="first done")
    finally:
        conn.close()
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert sum(len(bucket) for bucket in runner._kanban_worker_focus_active.values()) == 1
    assert adapter.edited[-1]["message_id"] == focus_message_id
    assert "second worker" in adapter.edited[-1]["content"]

    conn = kb.connect()
    try:
        kb.complete_task(conn, second, summary="second done")
    finally:
        conn.close()
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert runner._kanban_worker_focus_active == {}
    assert adapter.deleted[-1] == {
        "chat_id": "chat-1",
        "message_id": focus_message_id,
    }

    original_get_task = kb.get_task
    get_task_calls = 0

    def _counted_get_task(*args, **kwargs):
        nonlocal get_task_calls
        get_task_calls += 1
        return original_get_task(*args, **kwargs)

    monkeypatch.setattr(kb, "get_task", _counted_get_task)
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert get_task_calls == 0
    assert runner._kanban_worker_focus_active == {}


def test_worker_focus_retry_boundary_decrements_without_chat_noise(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "worker-focus-retry.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_focus_handoff": True}},
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="retry worker", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="topic-7",
            chat_type="group",
        )
        assert kb.claim_task(conn, task_id, claimer="worker:first") is not None
    finally:
        conn.close()

    adapter = EditableRecordingAdapter()
    runner = _make_runner(adapter)
    runner._is_session_running = lambda _key: False
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert sum(len(bucket) for bucket in runner._kanban_worker_focus_active.values()) == 1

    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ?",
            (task_id,),
        )
        kb._append_event(conn, task_id, kind="crashed")
        conn.commit()
    finally:
        conn.close()
    sent_before_crash = len(adapter.sent)
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert runner._kanban_worker_focus_active == {}
    assert len(adapter.sent) == sent_before_crash
    assert adapter.deleted

    conn = kb.connect()
    try:
        assert kb.claim_task(conn, task_id, claimer="worker:retry") is not None
    finally:
        conn.close()
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert sum(len(bucket) for bucket in runner._kanban_worker_focus_active.values()) == 1
    assert not any(
        "started" in item["text"].lower()
        for item in adapter.sent[sent_before_crash:]
    )


def test_worker_focus_rehydrates_once_after_gateway_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "worker-focus-rehydrate.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_focus_handoff": True}},
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="survives restart", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="topic-7",
            chat_type="group",
        )
        assert kb.claim_task(conn, task_id, claimer="worker:restart") is not None
        latest = conn.execute(
            "SELECT MAX(id) AS id FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()["id"]
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ?",
            (latest, task_id),
        )
        conn.commit()
    finally:
        conn.close()

    adapter = EditableRecordingAdapter()
    runner = _make_runner(adapter)
    runner._is_session_running = lambda _key: False
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert runner._kanban_worker_focus_rehydrated is True
    assert sum(len(bucket) for bucket in runner._kanban_worker_focus_active.values()) == 1
    assert any("survives restart" in item["text"] for item in adapter.sent)


def test_worker_focus_text_contains_only_event_owned_state():
    class Task:
        id = "t_focus"
        title = "focus task"
        assignee = "worker"
        current_run_id = 7

    rendered = _render_kanban_worker_focus(
        Task(), board="board-a", active_count=2,
    )
    assert "worker 1/2" in rendered
    assert "run 7" in rendered
    assert "Heartbeat" not in rendered


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_silences_retry_crashes_until_final_give_up(tmp_path, monkeypatch):
    """Retry telemetry stays in the DB and only the final give-up is visible."""
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # More internal retry telemetry followed by the actionable final state.
        kb._append_event(conn, tid, kind="crashed")
        kb._append_event(conn, tid, kind="gave_up")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "gave up" in adapter.sent[0]["text"].lower()


def test_notifier_subscription_survives_done_reopen_until_archive(
    tmp_path, monkeypatch,
):
    """Done is reversible; archive alone ends notification ownership."""
    db_path = tmp_path / "done-reopen-archive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"agent_wake_on_events": True}},
    )
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.sent[0]["metadata"]["thread_id"] == "origin-thread"
    assert adapter.handled[0].source.thread_id == "origin-thread"
    assert adapter.handled[0].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, "completion must retain the origin subscription"
        first_cursor = subs[0]["last_event_id"]
    finally:
        conn.close()

    # A quiet tick proves the completed event cannot replay after its cursor
    # was advanced, even though the subscription now remains present.
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="corrected completion")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Chat converges to the latest material state in the tick instead of
    # replaying its event log. The intermediate ready status is consumed by
    # the cursor; only the corrected completion is sent and wakes the exact
    # original session/thread.
    assert len(adapter.sent) == 2
    assert len(adapter.handled) == 2
    assert all(item["chat_id"] == "origin-chat" for item in adapter.sent)
    assert adapter.handled[-1].source.thread_id == "origin-thread"
    assert adapter.handled[-1].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["last_event_id"] > first_cursor
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Archive itself is intentionally silent, but consumes its event and
    # removes the subscription so no later historical event can replay.
    assert len(adapter.sent) == 2
    assert len(adapter.handled) == 2
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_completed_then_archived_same_tick_delivers_completion_and_unsubscribes(
    tmp_path, monkeypatch,
):
    """Archive is silent control state, not a reason to lose completion."""
    db_path = tmp_path / "completed-archive-same-tick.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="deliver before archive", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
        )
        assert kb.complete_task(conn, tid, summary="delivered result")
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"]
    assert "delivered result" in adapter.sent[0]["text"]
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"agent_wake_on_events": True}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kb.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kb.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kb.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kb, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert "TRIAGE" in text
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []
