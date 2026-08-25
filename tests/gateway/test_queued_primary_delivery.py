from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner, _send_queued_response_durably
from tools.process_registry import format_process_notification


class _Platform:
    value = "telegram"


def _source():
    return SimpleNamespace(
        platform=_Platform(),
        chat_id="chat-1",
        thread_id="thread-1",
    )


def _delivery_namespace(tmp_path, content):
    from agent.turn_checkpoint import TurnCheckpointStore, checkpoint_delivery_fence

    storage_home = tmp_path / ".hermes"
    store = TurnCheckpointStore(storage_home / "sessions" / "turn-checkpoints")
    store.start_turn(
        "session-1",
        "turn-1",
        "deliver queued answer",
        [{"role": "user", "content": "deliver queued answer"}],
        routing={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "thread-1",
        },
    )
    state = store.mark_deliverable(
        "session-1",
        content,
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    namespace = store.delivery_namespace()
    return {
        "session_id": "session-1",
        "checkpoint_fence": checkpoint_delivery_fence(state),
        "checkpoint_root": namespace["checkpoint_root"],
        "storage_home": namespace["storage_home"],
    }


@pytest.mark.asyncio
async def test_queued_primary_uses_retry_rail_and_durable_ledger(tmp_path):
    delivery = _delivery_namespace(tmp_path, "the complete primary answer")
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="sent-1")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "the complete primary answer",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
        metadata={"thread_id": "thread-1"},
    )

    assert delivered is True
    adapter._send_with_retry.assert_awaited_once()
    adapter.send.assert_not_awaited()
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        row = conn.execute(
            "SELECT content, state FROM delivery_obligations"
        ).fetchone()
    assert row == ("the complete primary answer", "delivered")


@pytest.mark.asyncio
async def test_queued_checkpoint_gate_precedes_attempting_and_remote_send(tmp_path):
    from agent import turn_checkpoint

    content = "the complete primary answer"
    delivery = _delivery_namespace(tmp_path, content)
    observed = []
    real_update = turn_checkpoint.update_checkpoint_delivery

    def guarded_checkpoint_update(*args, **kwargs):
        if kwargs.get("status") == "attempting":
            with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
                observed.append(
                    (
                        "checkpoint",
                        *conn.execute(
                            "SELECT state, attempts FROM delivery_obligations"
                        ).fetchone(),
                    )
                )
        return real_update(*args, **kwargs)

    async def inspected_send(**_kwargs):
        with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
            observed.append(
                (
                    "send",
                    *conn.execute(
                        "SELECT state, attempts FROM delivery_obligations"
                    ).fetchone(),
                )
            )
        return SendResult(success=True, message_id="sent-1")

    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(side_effect=inspected_send),
        send=AsyncMock(),
    )
    with patch(
        "agent.turn_checkpoint.update_checkpoint_delivery",
        side_effect=guarded_checkpoint_update,
    ):
        delivered = await _send_queued_response_durably(
            adapter,
            _source(),
            content,
            session_key="agent:main:telegram:group:chat-1:thread-1",
            session_id=delivery["session_id"],
            checkpoint_fence=delivery["checkpoint_fence"],
            checkpoint_root=delivery["checkpoint_root"],
            storage_home=delivery["storage_home"],
        )

    assert delivered is True
    assert observed == [
        ("checkpoint", "claimed", 0),
        ("send", "attempting", 1),
    ]


@pytest.mark.asyncio
async def test_queued_cancellation_before_send_releases_live_claim(tmp_path):
    content = "owed answer"
    delivery = _delivery_namespace(tmp_path, content)
    entered_checkpoint_handoff = threading.Event()
    release_checkpoint_handoff = threading.Event()

    def blocked_checkpoint_handoff(*_args, **_kwargs):
        entered_checkpoint_handoff.set()
        release_checkpoint_handoff.wait(timeout=5.0)
        return True

    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="must-not-send")
        ),
        send=AsyncMock(),
    )
    with patch(
        "agent.turn_checkpoint.update_checkpoint_delivery",
        side_effect=blocked_checkpoint_handoff,
    ):
        delivery_task = asyncio.create_task(
            _send_queued_response_durably(
                adapter,
                _source(),
                content,
                session_key="agent:main:telegram:group:chat-1:thread-1",
                session_id=delivery["session_id"],
                checkpoint_fence=delivery["checkpoint_fence"],
                checkpoint_root=delivery["checkpoint_root"],
                storage_home=delivery["storage_home"],
            )
        )
        assert await asyncio.to_thread(entered_checkpoint_handoff.wait, 2.0)
        with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
            assert conn.execute(
                "SELECT state, attempts FROM delivery_obligations"
            ).fetchone() == ("claimed", 0)
        delivery_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery_task
        release_checkpoint_handoff.set()

    adapter._send_with_retry.assert_not_awaited()
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        assert conn.execute(
            "SELECT state, attempts FROM delivery_obligations"
        ).fetchone() == ("deferred", 0)


@pytest.mark.asyncio
async def test_unconfirmed_primary_stays_in_ledger_and_blocks_followup(
    tmp_path,
):
    delivery = _delivery_namespace(tmp_path, "owed answer")
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=False, error="Timed out")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "owed answer",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
    )

    assert delivered is False
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        row = conn.execute(
            "SELECT content, state, last_error FROM delivery_obligations"
        ).fetchone()
    assert row == ("owed answer", "failed", "Timed out")


@pytest.mark.asyncio
async def test_queued_pre_network_failure_defers_without_spending_attempt(tmp_path):
    content = "owed answer"
    delivery = _delivery_namespace(tmp_path, content)
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(
            return_value=SendResult(
                success=False,
                error="Not connected",
                raw_response={"send_attempted": False},
            )
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        content,
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
    )

    assert delivered is False
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        assert conn.execute(
            "SELECT state, attempts FROM delivery_obligations"
        ).fetchone() == ("deferred", 0)


@pytest.mark.asyncio
async def test_queued_exact_delivery_disables_mutating_format_fallback(tmp_path):
    from agent.turn_checkpoint import TurnCheckpointStore

    content = "**the exact queued answer**"
    delivery = _delivery_namespace(tmp_path, content)
    formatting_failure = SendResult(
        success=False,
        error="Bad Request: can't parse entities",
    )
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        _send_with_retry=AsyncMock(return_value=formatting_failure),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        content,
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
    )

    assert delivered is False
    adapter._send_with_retry.assert_awaited_once_with(
        chat_id="chat-1",
        content=content,
        reply_to=None,
        metadata=None,
        allow_content_fallback=False,
    )
    adapter.send.assert_not_awaited()
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        row = conn.execute(
            "SELECT content, state, last_error FROM delivery_obligations"
        ).fetchone()
    assert row == (content, "failed", formatting_failure.error)
    checkpoint = TurnCheckpointStore(delivery["checkpoint_root"]).load("session-1")
    assert checkpoint["delivery"]["status"] == "failed"


@pytest.mark.asyncio
async def test_unproven_adapter_records_best_effort_without_exact_ledger(tmp_path):
    from agent.turn_checkpoint import TurnCheckpointStore

    content = "adapter may truncate this response"
    delivery = _delivery_namespace(tmp_path, content)
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="best-effort-1")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        content,
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
    )

    assert delivered is True
    adapter._send_with_retry.assert_awaited_once_with(
        chat_id="chat-1",
        content=content,
        reply_to=None,
        metadata=None,
    )
    checkpoint = TurnCheckpointStore(delivery["checkpoint_root"]).load("session-1")
    assert checkpoint["phase"] == "terminal"
    assert checkpoint["delivery"] == {
        "obligation_id": None,
        "status": "best_effort",
        "reported_success": True,
    }
    assert not (tmp_path / ".hermes" / "state.db").exists()


@pytest.mark.asyncio
async def test_queued_reconcile_sends_exact_final_in_new_message(tmp_path):
    delivery = _delivery_namespace(tmp_path, "corrected final")
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        edit_message=AsyncMock(),
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="stream-1")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "corrected final",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
        edit_message_id="stream-1",
    )

    assert delivered is True
    adapter.edit_message.assert_not_awaited()
    adapter._send_with_retry.assert_awaited_once_with(
        chat_id="chat-1",
        content="corrected final",
        reply_to=None,
        metadata=None,
        allow_content_fallback=False,
    )
    adapter.send.assert_not_awaited()
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        assert conn.execute(
            "SELECT state FROM delivery_obligations"
        ).fetchone() == ("delivered",)


@pytest.mark.asyncio
async def test_queued_reconcile_never_uses_mutable_edit_for_durable_final(tmp_path):
    delivery = _delivery_namespace(tmp_path, "corrected final")
    adapter = SimpleNamespace(
        supports_exact_text_delivery=True,
        edit_message=AsyncMock(),
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=False, error="send timeout")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "corrected final",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=delivery["session_id"],
        checkpoint_fence=delivery["checkpoint_fence"],
        checkpoint_root=delivery["checkpoint_root"],
        storage_home=delivery["storage_home"],
        edit_message_id="stream-1",
    )

    assert delivered is False
    adapter.edit_message.assert_not_awaited()
    adapter._send_with_retry.assert_awaited_once_with(
        chat_id="chat-1",
        content="corrected final",
        reply_to=None,
        metadata=None,
        allow_content_fallback=False,
    )
    adapter.send.assert_not_awaited()
    with sqlite3.connect(tmp_path / ".hermes" / "state.db") as conn:
        assert conn.execute(
            "SELECT state FROM delivery_obligations"
        ).fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_disabled_ledger_preserves_unfenced_legacy_queued_send(monkeypatch):
    monkeypatch.setattr("gateway.delivery_ledger.ledger_enabled", lambda: False)
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="legacy-1")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "legacy proxy answer",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=None,
    )

    assert delivered is True
    adapter._send_with_retry.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_best_effort_preserves_unfenced_nonstream_queued_send(
    monkeypatch,
):
    monkeypatch.setattr("gateway.delivery_ledger.ledger_enabled", lambda: True)
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SendResult(success=True, message_id="proxy-1")
        ),
        send=AsyncMock(),
    )

    delivered = await _send_queued_response_durably(
        adapter,
        _source(),
        "proxy answer",
        session_key="agent:main:telegram:group:chat-1:thread-1",
        session_id=None,
        allow_legacy_unfenced=True,
    )

    assert delivered is True
    adapter._send_with_retry.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_best_effort_can_advance_streamed_queued_followup():
    runner = object.__new__(GatewayRunner)

    delivered = await runner._deliver_queued_first_response(
        "already streamed proxy answer",
        _source(),
        SimpleNamespace(),
        text_already_delivered=True,
        deliver_media=False,
        allow_legacy_unfenced=True,
    )

    assert delivered is True


def test_failed_primary_requeues_synthetic_event_ahead_of_existing_fifo():
    runner = object.__new__(GatewayRunner)
    current = SimpleNamespace(text="process completion")
    displaced = SimpleNamespace(text="next user message")
    tail = SimpleNamespace(text="tail")
    adapter = SimpleNamespace(_pending_messages={"session": displaced})
    runner._queued_events = {"session": [tail]}

    runner._requeue_event_front("session", current, adapter)

    assert adapter._pending_messages["session"] is current
    assert runner._queued_events["session"] == [displaced, tail]


@pytest.mark.asyncio
async def test_cancelled_primary_delivery_restores_dequeued_followup_fifo():
    runner = object.__new__(GatewayRunner)
    current = SimpleNamespace(text="dequeued follow-up")
    displaced = SimpleNamespace(text="next queued follow-up")
    adapter = SimpleNamespace(_pending_messages={"session": displaced})
    runner._queued_events = {"session": []}
    delivery_started = asyncio.Event()

    async def blocked_delivery():
        delivery_started.set()
        await asyncio.Event().wait()
        return True

    task = asyncio.create_task(
        runner._await_queued_primary_delivery(
            blocked_delivery(),
            session_key="session",
            pending_event=current,
            pending_text=None,
            adapter=adapter,
        )
    )
    await delivery_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter._pending_messages["session"] is current
    assert runner._queued_events["session"] == [displaced]


def test_process_completion_cannot_claim_global_terminality():
    text = format_process_notification(
        {
            "type": "completion",
            "session_id": "proc-1",
            "command": "backup",
            "exit_code": 0,
            "output": "ok",
        }
    )

    assert "does not supersede the latest real user request" in text
    assert "do not declare that no actions are pending" in text
    assert "delivery obligation" in text
