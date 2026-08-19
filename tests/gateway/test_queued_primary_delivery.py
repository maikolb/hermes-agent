from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_queued_primary_uses_retry_rail_and_durable_ledger(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    adapter = SimpleNamespace(
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
        session_id=None,
        metadata={"thread_id": "thread-1"},
    )

    assert delivered is True
    adapter._send_with_retry.assert_awaited_once()
    adapter.send.assert_not_awaited()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        row = conn.execute(
            "SELECT content, state FROM delivery_obligations"
        ).fetchone()
    assert row == ("the complete primary answer", "delivered")


@pytest.mark.asyncio
async def test_unconfirmed_primary_stays_in_ledger_and_blocks_followup(
    tmp_path, monkeypatch
):
    from gateway import delivery_ledger as ledger

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    adapter = SimpleNamespace(
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
        session_id=None,
    )

    assert delivered is False
    with sqlite3.connect(tmp_path / "state.db") as conn:
        row = conn.execute(
            "SELECT content, state, last_error FROM delivery_obligations"
        ).fetchone()
    assert row == ("owed answer", "failed", "Timed out")


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
