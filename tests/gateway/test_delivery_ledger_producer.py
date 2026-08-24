"""Producer-hook tests: _process_message_background records delivery
obligations around the final send (gateway/platforms/base.py).

Contract: a checkpoint-fenced obligation is recorded
(pending→claimed→attempting) BEFORE the send await and sealed
delivered/failed by SendResult afterward;
slash commands, ephemeral replies, and empty responses are never recorded;
failure to persist the durable boundary blocks the send.
"""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


class _Adapter(BasePlatformAdapter):  # type: ignore[misc]
    """Minimal concrete adapter driving the real base-class pipeline."""

    supports_exact_text_delivery = True

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):  # pragma: no cover
        return True

    async def disconnect(self):  # pragma: no cover - unused
        return None

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="m1")


def _event(text="hello agent"):
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK, chat_id="C1", chat_type="channel"
        ),
        message_id="msg-42",
    )
    from agent.turn_checkpoint import TurnCheckpointStore, checkpoint_delivery_fence

    storage_home = Path(dl._db_path()).parent
    store = TurnCheckpointStore(storage_home / "sessions" / "turn-checkpoints")
    store.start_turn(
        "session-1",
        "turn-1",
        text,
        [{"role": "user", "content": text}],
        routing={"platform": "slack", "chat_id": "C1", "thread_id": ""},
    )
    state = store.mark_deliverable(
        "session-1",
        "final answer",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    namespace = store.delivery_namespace()
    event.delivery_checkpoint_session_id = "session-1"
    event.delivery_checkpoint_fence = checkpoint_delivery_fence(state)
    event.delivery_checkpoint_root = namespace["checkpoint_root"]
    event.delivery_storage_home = namespace["storage_home"]
    return event


def _rows():
    with dl._connect() as conn:
        return conn.execute(
            "SELECT obligation_id, state, content FROM delivery_obligations"
        ).fetchall()


def _blocking_probe():
    """Return a blocking ledger call and an event-loop progress witness."""
    ledger_started = threading.Event()
    event_loop_progressed = threading.Event()
    blocked_event_loop = []

    def _slow_ledger_call(*args, **kwargs):
        ledger_started.set()
        # Generous timeout: a genuinely blocked loop can never set the event
        # (the witness coroutine cannot run), so a longer wait only guards
        # against loaded-CI scheduling flake, not against missing the bug.
        if not event_loop_progressed.wait(timeout=5.0):
            blocked_event_loop.append(True)

    async def _event_loop_witness():
        deadline = asyncio.get_running_loop().time() + 10
        while not ledger_started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("ledger call never started")
            await asyncio.sleep(0)
        event_loop_progressed.set()

    return _slow_ledger_call, _event_loop_witness, blocked_event_loop


async def _run(adapter, event, response="final answer"):
    adapter._message_handler = AsyncMock(return_value=response)
    session_key = "agent:main:slack:channel:C1"
    adapter._active_sessions[session_key] = asyncio.Event()
    await adapter._process_message_background(event, session_key)


class TestProducerHook:
    @pytest.mark.asyncio
    async def test_normal_turn_records_and_delivers(self):
        adapter = _Adapter()
        await _run(adapter, _event())

        assert adapter.sent == ["final answer"]
        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1] == "delivered"
        assert rows[0][2] == "final answer"

    @pytest.mark.asyncio
    async def test_send_failure_leaves_failed_row(self):
        adapter = _Adapter()
        adapter.send = AsyncMock(
            return_value=SendResult(success=False, error="chat_not_found")
        )
        await _run(adapter, _event())

        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1] == "failed"

    @pytest.mark.asyncio
    async def test_explicit_pre_network_failure_defers_without_spending_attempt(self):
        adapter = _Adapter()
        adapter.send = AsyncMock(
            return_value=SendResult(
                success=False,
                error="Not connected",
                raw_response={"send_attempted": False},
            )
        )

        await _run(adapter, _event())

        with dl._connect() as conn:
            assert conn.execute(
                "SELECT state, attempts FROM delivery_obligations"
            ).fetchone() == ("deferred", 0)

    @pytest.mark.asyncio
    async def test_unproven_adapter_records_best_effort_not_exact_ack(self):
        from agent.turn_checkpoint import TurnCheckpointStore

        adapter = _Adapter()
        adapter.supports_exact_text_delivery = False
        event = _event()

        await _run(adapter, event)

        assert adapter.sent == ["final answer"]
        assert _rows() == []
        checkpoint = TurnCheckpointStore(
            event.delivery_checkpoint_root
        ).load("session-1")
        assert checkpoint["phase"] == "terminal"
        assert checkpoint["delivery"] == {
            "obligation_id": None,
            "status": "best_effort",
            "reported_success": True,
        }

    @pytest.mark.asyncio
    async def test_reconnect_capability_loss_blocks_before_network_handoff(self):
        adapter = _Adapter()
        replacement = _Adapter()
        replacement.supports_exact_text_delivery = False
        adapter._final_delivery_adapter = MagicMock(
            side_effect=[adapter, replacement]
        )

        await _run(adapter, _event())

        assert adapter.sent == []
        assert replacement.sent == []
        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1] == "pending"
        with dl._connect() as conn:
            row = conn.execute(
                "SELECT attempt_token, attempts FROM delivery_obligations"
            ).fetchone()
        assert row[0] is None
        assert row[1] == 0

    @pytest.mark.asyncio
    async def test_checkpoint_gate_precedes_attempting_and_remote_send(self):
        from agent import turn_checkpoint

        adapter = _Adapter()
        observed = []
        real_update = turn_checkpoint.update_checkpoint_delivery

        def guarded_checkpoint_update(*args, **kwargs):
            if kwargs.get("status") == "attempting":
                with dl._connect() as conn:
                    observed.append(
                        (
                            "checkpoint",
                            *conn.execute(
                                "SELECT state, attempts FROM delivery_obligations"
                            ).fetchone(),
                        )
                    )
            return real_update(*args, **kwargs)

        async def inspected_send(
            chat_id, content, reply_to=None, metadata=None
        ):
            with dl._connect() as conn:
                observed.append(
                    (
                        "send",
                        *conn.execute(
                            "SELECT state, attempts FROM delivery_obligations"
                        ).fetchone(),
                    )
                )
            adapter.sent.append(content)
            return SendResult(success=True, message_id="m1")

        adapter.send = inspected_send
        with patch(
            "agent.turn_checkpoint.update_checkpoint_delivery",
            side_effect=guarded_checkpoint_update,
        ):
            await _run(adapter, _event())

        assert observed == [
            ("checkpoint", "claimed", 0),
            ("send", "attempting", 1),
        ]
        assert _rows()[0][1] == "delivered"

    @pytest.mark.asyncio
    async def test_direct_cancellation_before_send_releases_live_claim(self):
        adapter = _Adapter()
        entered_checkpoint_handoff = threading.Event()
        release_checkpoint_handoff = threading.Event()

        def blocked_checkpoint_handoff(*_args, **_kwargs):
            entered_checkpoint_handoff.set()
            release_checkpoint_handoff.wait(timeout=5.0)
            return True

        with patch(
            "agent.turn_checkpoint.update_checkpoint_delivery",
            side_effect=blocked_checkpoint_handoff,
        ):
            delivery_task = asyncio.create_task(_run(adapter, _event()))
            assert await asyncio.to_thread(
                entered_checkpoint_handoff.wait, 2.0
            )
            with dl._connect() as conn:
                assert conn.execute(
                    "SELECT state, attempts FROM delivery_obligations"
                ).fetchone() == ("claimed", 0)
            delivery_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await delivery_task
            release_checkpoint_handoff.set()

        assert adapter.sent == []
        with dl._connect() as conn:
            assert conn.execute(
                "SELECT state, attempts FROM delivery_obligations"
            ).fetchone() == ("deferred", 0)


    @pytest.mark.asyncio
    async def test_slow_ledger_record_does_not_block_event_loop(self):
        adapter = _Adapter()
        slow_record, event_loop_witness, blocked_event_loop = _blocking_probe()

        with patch(
            "gateway.delivery_ledger.record_obligation",
            side_effect=slow_record,
        ):
            await asyncio.gather(_run(adapter, _event()), event_loop_witness())

        assert blocked_event_loop == []
        assert adapter.sent == []

    @pytest.mark.asyncio
    async def test_pre_network_ledger_failure_marks_exact_final_for_recovery(self):
        adapter = _Adapter()
        mark_recovery = AsyncMock(return_value=True)
        adapter.gateway_runner = SimpleNamespace(
            _mark_delivery_checkpoint_recovery_pending=mark_recovery
        )

        with patch(
            "gateway.delivery_ledger.record_obligation",
            side_effect=OSError("state db temporarily unavailable"),
        ):
            await _run(adapter, _event())

        assert adapter.sent == []
        mark_recovery.assert_awaited_once_with(
            "agent:main:slack:channel:C1",
            Platform.SLACK,
        )

    @pytest.mark.asyncio
    async def test_slow_ledger_update_does_not_block_event_loop(self):
        adapter = _Adapter()
        slow_delivered, event_loop_witness, blocked_event_loop = _blocking_probe()

        with patch(
            "gateway.delivery_ledger.record_obligation",
            return_value="created",
        ), patch(
            "gateway.delivery_ledger.mark_claimed",
            return_value="attempt-token",
        ), patch(
            "gateway.delivery_ledger.mark_claimed_attempting",
            return_value=True,
        ), patch(
            "gateway.delivery_ledger.mark_delivered",
            side_effect=slow_delivered,
        ):
            await asyncio.gather(_run(adapter, _event()), event_loop_witness())

        assert blocked_event_loop == []
        assert adapter.sent == ["final answer"]

    @pytest.mark.asyncio
    async def test_crash_between_attempting_and_ack_is_blocked_as_ambiguous(self):
        """After the adapter boundary, automatic replay could duplicate.

        A fresh process therefore classifies the durable obligation as
        ``delivery_ambiguous`` and waits for authoritative reconciliation.
        """
        adapter = _Adapter()

        async def _dies_mid_send(chat_id, content, reply_to=None, metadata=None):
            raise ConnectionError("gateway killed mid-await")

        adapter.send = _dies_mid_send
        # _send_with_retry raising propagates; the background task catches
        # broadly — drive only through the send block by tolerating the error.
        try:
            await _run(adapter, _event())
        except Exception:
            pass

        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1] == "failed"
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET owner_pid=999999999, owner_started_at=1"
            )
        claimed = dl.sweep_recoverable()
        assert claimed == []
        assert _rows()[0][1] == "delivery_ambiguous"
