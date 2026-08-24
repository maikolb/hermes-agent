"""Tests for the gateway delivery-obligation ledger (gateway/delivery_ledger.py).

State machine, attempt-token CAS, dead-owner claiming, attempts cap, stale
cutoff, retention, id stability, and the startup redelivery sweep's contract:
- pending rows redeliver plainly (send never started, no dup risk)
- durable recovery claims remain replay-safe until the pre-network handoff
- legacy non-durable attempting/failed rows carry the recovered-reply marker
- durable attempting/failed rows stop as ambiguous and are never auto-retried
- rows owned by a LIVE process are never claimed
- poison rows abandon at the attempts cap / stale cutoff
- row-cap pruning never deletes an active obligation
"""

import sqlite3
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Isolated state.db per test (autouse HERMES_HOME isolation already
    redirects get_hermes_home; make the redirect explicit and per-test)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
    return dl.record_obligation(
        obligation_id=oid,
        session_key=session_key,
        platform=kw.get("platform", "slack"),
        chat_id=kw.get("chat_id", "C1"),
        thread_id=kw.get("thread_id", "171.001"),
        content=kw.get("content", "the final answer"),
        session_id=kw.get("session_id"),
        checkpoint_turn_id=kw.get("checkpoint_turn_id"),
        checkpoint_revision=kw.get("checkpoint_revision"),
        checkpoint_content_sha256=kw.get("checkpoint_content_sha256"),
        storage_home=kw.get("storage_home"),
    )


def _record_durable(oid="ob-durable", **kw):
    content = kw.pop("content", "the final answer")
    return _record(
        oid=oid,
        session_id=kw.pop("session_id", "durable-session"),
        checkpoint_turn_id=kw.pop("checkpoint_turn_id", "turn-1"),
        checkpoint_revision=kw.pop("checkpoint_revision", "revision-1"),
        checkpoint_content_sha256=kw.pop(
            "checkpoint_content_sha256",
            dl.hashlib.sha256(content.encode("utf-8")).hexdigest(),
        ),
        content=content,
        **kw,
    )


def _row(oid):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT state, attempts, owner_pid, content, attempt_token,
                      content_sha256
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return None if r is None else {
        "state": r[0], "attempts": r[1], "owner_pid": r[2], "content": r[3],
        "attempt_token": r[4], "content_sha256": r[5],
    }


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
        import asyncio

        deadline = asyncio.get_running_loop().time() + 10
        while not ledger_started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("ledger call never started")
            await asyncio.sleep(0)
        event_loop_progressed.set()

    return _slow_ledger_call, _event_loop_witness, blocked_event_loop


def _orphan(oid):
    """Make the row look like it belongs to a dead process."""
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=999999999, "
            "owner_started_at=1 WHERE obligation_id=?",
            (oid,),
        )


class TestStateMachine:
    def test_record_starts_pending(self):
        _record()
        assert _row("ob-1")["state"] == "pending"

    def test_durable_producer_claim_stays_pre_network_until_handoff(self):
        _record_durable()

        token = dl.mark_claimed("ob-durable")

        assert token
        assert _row("ob-durable")["state"] == "claimed"
        assert _row("ob-durable")["attempts"] == 0
        assert dl.mark_claimed_attempting(
            "ob-durable", attempt_token=token
        )
        assert _row("ob-durable")["state"] == "attempting"
        assert _row("ob-durable")["attempts"] == 1

    def test_legacy_row_cannot_enter_durable_claim_state(self):
        _record()

        assert dl.mark_claimed("ob-1") == ""
        assert _row("ob-1")["state"] == "pending"

    def test_rerecord_delivered_is_monotonic(self):
        assert _record() == "created"
        token = dl.mark_attempting("ob-1")
        assert token
        assert dl.mark_delivered("ob-1", attempt_token=token) is True

        assert _record() == "already_delivered"
        assert _row("ob-1")["state"] == "delivered"
        assert _row("ob-1")["attempt_token"] == token

    def test_stable_id_collision_fails_closed(self):
        _record()
        with pytest.raises(dl.DeliveryLedgerIntegrityError, match="collided"):
            _record(content="different immutable payload")

    def test_non_durable_identity_cannot_be_upgraded_in_place(self):
        _record(
            checkpoint_turn_id="turn-1",
            checkpoint_revision="revision-1",
            checkpoint_content_sha256=dl.hashlib.sha256(
                b"the final answer"
            ).hexdigest(),
        )
        with pytest.raises(
            dl.DeliveryLedgerIntegrityError,
            match="durable sessions",
        ):
            _record_durable(oid="ob-1")

    def test_durable_content_must_match_checkpoint_digest(self):
        digest = dl.hashlib.sha256(b"different content").hexdigest()

        with pytest.raises(
            dl.DeliveryLedgerIntegrityError,
            match="does not match its checkpoint digest",
        ):
            _record_durable(checkpoint_content_sha256=digest)

    def test_late_ack_cannot_finish_new_durable_attempt(self):
        _record_durable()
        first = dl.mark_attempting("ob-durable")
        assert first
        assert dl.mark_deferred(
            "ob-durable", "network untouched", attempt_token=first
        )
        assert dl.mark_attempting(
            "ob-durable", attempt_token=first
        ) == "", "an explicit token may not be reused for a later attempt"
        second = dl.mark_attempting("ob-durable")
        assert second and second != first

        assert dl.mark_delivered(
            "ob-durable", attempt_token=first
        ) is False
        assert _row("ob-durable")["state"] == "attempting"
        assert dl.mark_delivered(
            "ob-durable", attempt_token=second
        ) is True

    def test_durable_transition_requires_explicit_attempt_token(self):
        _record_durable()
        token = dl.mark_attempting("ob-durable")
        assert token
        assert dl.mark_delivered("ob-durable") is False
        assert dl.mark_failed("ob-durable", "no token") is False
        assert dl.mark_deferred("ob-durable", "no token") is False
        assert _row("ob-durable")["state"] == "attempting"


class TestObligationId:
    def test_stable_and_distinct(self):
        a = dl.compute_obligation_id("sk1", "msg1", "hello")
        assert a == dl.compute_obligation_id("sk1", "msg1", "hello")
        # Different thread (baked into session_key) → different id. This is
        # the cron-topic collision class from the earlier outbox attempt.
        assert a != dl.compute_obligation_id("sk1:threadB", "msg1", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg2", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg1", "other")
        assert len(a) == 24


class TestSweep:
    def test_live_owner_rows_never_claimed(self):
        _record()  # owner = this (live) process
        assert dl.sweep_recoverable() == []

    def test_live_owner_deferred_claimed_only_when_enabled(self):
        _record()
        dl.mark_deferred("ob-1", "send_path_degraded")
        assert dl.sweep_recoverable() == []

        claimed = dl.sweep_recoverable(include_live_deferred=True)

        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False
        assert _row("ob-1")["state"] == "attempting"
        assert dl.sweep_recoverable(include_live_deferred=True) == []

    def test_live_owner_non_deferred_rows_stay_unclaimed(self):
        for oid, state in (
            ("pending", None),
            ("attempting", "attempting"),
            ("failed", "failed"),
        ):
            _record(oid=oid)
            if state == "attempting":
                dl.mark_attempting(oid)
            elif state == "failed":
                dl.mark_failed(oid, "rejected")

        assert dl.sweep_recoverable(include_live_deferred=True) == []

    def test_dead_owner_pending_claimed_without_marker(self):
        _record()
        _orphan("ob-1")
        claimed = dl.sweep_recoverable()
        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False
        assert claimed[0]["attempts"] == 1
        assert len(claimed[0]["attempt_token"]) == 32
        assert claimed[0]["storage_home"] == str(dl._db_path().parent.resolve())
        # Claim re-stamps ownership: a second sweep in the same (live)
        # process must not double-claim.
        assert dl.sweep_recoverable() == []

    def test_content_digest_corruption_is_blocked(self):
        _record()
        _orphan("ob-1")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET content=? WHERE obligation_id=?",
                ("tampered after persistence", "ob-1"),
            )

        assert dl.sweep_recoverable() == []
        assert _row("ob-1")["state"] == "recovery_blocked"

    def test_checkpoint_validation_holds_no_state_db_write_lock(
        self, monkeypatch
    ):
        """Fence I/O must not invert the reseal checkpoint->state.db order.

        The first legacy row forces a content-digest backfill.  If the sweep
        kept one write transaction open while validating the following
        durable row, this concurrent state.db writer would fail immediately
        with ``database is locked`` (the pre-fix lock inversion).
        """
        _record(oid="legacy-first", content="legacy payload")
        _record_durable(oid="durable-second", content="durable payload")
        _orphan("legacy-first")
        _orphan("durable-second")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET content_sha256=NULL "
                "WHERE obligation_id='legacy-first'"
            )

        writer_errors = []

        def fence_probe(**_kwargs):
            def concurrent_state_writer():
                conn = None
                try:
                    conn = sqlite3.connect(dl._db_path(), timeout=0.2)
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE delivery_obligations SET updated_at=updated_at "
                        "WHERE obligation_id='legacy-first'"
                    )
                    conn.rollback()
                except BaseException as exc:  # surfaced in the parent assertion
                    writer_errors.append(exc)
                finally:
                    if conn is not None:
                        conn.close()

            writer = threading.Thread(target=concurrent_state_writer)
            writer.start()
            writer.join(timeout=1.0)
            assert not writer.is_alive(), "state.db writer stalled during fence lookup"
            assert writer_errors == []
            return "match"

        monkeypatch.setattr(dl, "_checkpoint_fence_disposition", fence_probe)

        claimed = dl.sweep_recoverable()

        assert {row["obligation_id"] for row in claimed} == {
            "legacy-first",
            "durable-second",
        }
        assert _row("legacy-first")["state"] == "attempting"
        assert _row("durable-second")["state"] == "claimed"

    def test_competing_claim_between_fence_validation_and_commit_wins_cas(
        self, monkeypatch
    ):
        """A competitor may win between phases; the stale snapshot must not."""
        _record_durable(oid="race-row")
        _orphan("race-row")
        competitor_token = []

        def competing_fence_probe(**_kwargs):
            # Fence validation must hold neither the Python ledger lock nor a
            # state.db write transaction.  Interleave a real competing claim
            # at this exact boundary without sleeps or timeout-based polling.
            assert dl._DB_LOCK.acquire(blocking=False)
            dl._DB_LOCK.release()
            competitor_token.append(dl.mark_attempting("race-row"))
            assert competitor_token[-1]
            return "match"

        monkeypatch.setattr(
            dl, "_checkpoint_fence_disposition", competing_fence_probe
        )

        assert dl.sweep_recoverable() == []
        assert _row("race-row")["state"] == "attempting"
        assert _row("race-row")["attempt_token"] == competitor_token[0]
        assert _row("race-row")["attempts"] == 0

    def test_durable_claim_is_replay_safe_until_network_handoff(
        self, monkeypatch
    ):
        """A process death after ownership claim must not imply network I/O."""
        _record_durable(oid="durable-claim")
        _orphan("durable-claim")
        monkeypatch.setattr(
            dl, "_checkpoint_fence_disposition", lambda **_kwargs: "match"
        )

        first = dl.sweep_recoverable()

        assert len(first) == 1
        first_token = first[0]["attempt_token"]
        assert _row("durable-claim")["state"] == "claimed"
        assert _row("durable-claim")["attempts"] == 0

        # Model process death before the runner reaches adapter.send(). The
        # next owner reclaims safely with a new token and no spent attempt.
        _orphan("durable-claim")
        second = dl.sweep_recoverable()

        assert len(second) == 1
        second_token = second[0]["attempt_token"]
        assert second_token != first_token
        assert _row("durable-claim")["state"] == "claimed"
        assert _row("durable-claim")["attempts"] == 0

        assert dl.mark_claimed_attempting(
            "durable-claim", attempt_token=second_token
        )
        assert _row("durable-claim")["state"] == "attempting"
        assert _row("durable-claim")["attempts"] == 1

    def test_defer_from_claimed_does_not_refund_an_earlier_attempt(
        self, monkeypatch
    ):
        _record_durable(oid="durable-reclaim")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET attempts=2 "
                "WHERE obligation_id='durable-reclaim'"
            )
        _orphan("durable-reclaim")
        monkeypatch.setattr(
            dl, "_checkpoint_fence_disposition", lambda **_kwargs: "match"
        )
        claimed = dl.sweep_recoverable()
        token = claimed[0]["attempt_token"]

        assert dl.mark_deferred(
            "durable-reclaim", "pre-network", attempt_token=token,
            refund_attempt=True,
        )
        assert _row("durable-reclaim")["state"] == "deferred"
        assert _row("durable-reclaim")["attempts"] == 2


class TestPrune:
    def test_old_delivered_rows_pruned(self):
        _record()
        token = dl.mark_attempting("ob-1")
        assert token
        dl.mark_delivered("ob-1", attempt_token=token)
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?",
                (time.time() - dl._RETENTION_SECONDS - 60, "ob-1"),
            )
        dl._prune()
        assert _row("ob-1") is None

    def test_row_cap_never_deletes_active_obligations(self):
        now = time.time()
        active = dl._MAX_ROWS + 1
        terminal = 20
        with dl._connect() as conn:
            conn.executemany(
                """INSERT INTO delivery_obligations
                   (obligation_id, session_key, platform, chat_id, content,
                    state, attempts, created_at, updated_at, content_sha256)
                   VALUES (?, ?, 'slack', 'C1', ?, ?, 0, ?, ?, ?)""",
                [
                    (
                        f"active-{index}",
                        f"session-{index}",
                        f"active payload {index}",
                        "pending",
                        now,
                        now,
                        dl.hashlib.sha256(
                            f"active payload {index}".encode()
                        ).hexdigest(),
                    )
                    for index in range(active)
                ]
                + [
                    (
                        f"terminal-{index}",
                        f"terminal-session-{index}",
                        f"terminal payload {index}",
                        "delivered",
                        now,
                        now,
                        dl.hashlib.sha256(
                            f"terminal payload {index}".encode()
                        ).hexdigest(),
                    )
                    for index in range(terminal)
                ],
            )

        dl._prune(now=now)

        with dl._connect() as conn:
            active_after = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations WHERE state='pending'"
            ).fetchone()[0]
            terminal_after = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations WHERE state='delivered'"
            ).fetchone()[0]
        assert active_after == active
        assert terminal_after == 0


class TestLedgerEnabled:
    def test_default_on(self):
        assert dl.ledger_enabled({}) is True
        assert dl.ledger_enabled({"gateway": {}}) is True


class TestGatewayRedeliverySweep:
    """Drive the real GatewayRunner._redeliver_pending_obligations."""

    @staticmethod
    def _runner(adapter=None):
        from gateway.config import Platform
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter} if adapter else {}
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _adapter(success=True):
        adapter = MagicMock()
        adapter.send = AsyncMock(
            return_value=MagicMock(success=success, error="" if success else "nope")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_pending_redelivers_plain_and_clears_resume(self):
        _record()  # pending
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = await runner._redeliver_pending_obligations()

        assert n == 1
        sent = adapter.send.call_args.kwargs
        assert sent["content"] == "the final answer"  # no marker
        assert sent["metadata"] == {"thread_id": "171.001"}
        assert _row("ob-1")["state"] == "delivered"
        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )

    @pytest.mark.asyncio
    async def test_durable_recovery_defers_when_exact_capability_is_unavailable(
        self,
    ):
        from agent.turn_checkpoint import (
            TurnCheckpointStore,
            bind_checkpoint_delivery_obligation,
            checkpoint_delivery_fence,
        )

        storage_home = dl._db_path().parent
        checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
        store = TurnCheckpointStore(checkpoint_root)
        route = {"platform": "slack", "chat_id": "C1", "thread_id": "171.001"}
        store.start_turn(
            "durable-session",
            "turn-1",
            "request",
            [{"role": "user", "content": "request"}],
            routing=route,
        )
        state = store.mark_deliverable(
            "durable-session",
            "the final answer",
            verification_pending=False,
            verification_kind="ordinary_final",
        )
        fence = checkpoint_delivery_fence(state)
        assert fence is not None
        obligation_id = "ob-durable-capability"
        assert bind_checkpoint_delivery_obligation(
            "durable-session",
            obligation_id=obligation_id,
            routing=route,
            checkpoint_root=checkpoint_root,
            **fence,
        )
        _record_durable(
            oid=obligation_id,
            checkpoint_turn_id=fence["turn_id"],
            checkpoint_revision=fence["deliverable_revision"],
            checkpoint_content_sha256=fence["content_sha256"],
            storage_home=storage_home,
        )
        _orphan(obligation_id)
        adapter = self._adapter()
        adapter.can_deliver_exact_text = MagicMock(return_value=False)
        adapter.supports_exact_text_delivery = False
        runner = self._runner(adapter)

        assert await runner._redeliver_pending_obligations() == 0

        adapter.send.assert_not_awaited()
        assert _row(obligation_id)["state"] == "deferred"
        assert _row(obligation_id)["attempts"] == 0

        reclaimed = dl.sweep_recoverable(
            include_live_deferred=True,
            storage_home=storage_home,
        )
        assert [row["obligation_id"] for row in reclaimed] == [obligation_id]
        assert _row(obligation_id)["state"] == "claimed"
        assert _row(obligation_id)["attempts"] == 0

    @pytest.mark.asyncio
    async def test_cancellation_before_recovered_send_releases_live_claim(self):
        """Restore-task cancellation must not strand ``claimed`` under a live PID."""
        import asyncio

        from agent.turn_checkpoint import (
            TurnCheckpointStore,
            bind_checkpoint_delivery_obligation,
            checkpoint_delivery_fence,
        )

        storage_home = dl._db_path().parent
        checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
        store = TurnCheckpointStore(checkpoint_root)
        route = {"platform": "slack", "chat_id": "C1", "thread_id": "171.001"}
        store.start_turn(
            "cancel-session",
            "cancel-turn",
            "request",
            [{"role": "user", "content": "request"}],
            routing=route,
        )
        state = store.mark_deliverable(
            "cancel-session",
            "the final answer",
            verification_pending=False,
            verification_kind="ordinary_final",
        )
        fence = checkpoint_delivery_fence(state)
        assert fence is not None
        obligation_id = "ob-durable-cancel"
        assert bind_checkpoint_delivery_obligation(
            "cancel-session",
            obligation_id=obligation_id,
            routing=route,
            checkpoint_root=checkpoint_root,
            **fence,
        )
        _record_durable(
            oid=obligation_id,
            session_id="cancel-session",
            checkpoint_turn_id=fence["turn_id"],
            checkpoint_revision=fence["deliverable_revision"],
            checkpoint_content_sha256=fence["content_sha256"],
            storage_home=storage_home,
        )
        _orphan(obligation_id)

        adapter = self._adapter()
        adapter.supports_exact_text_delivery = True
        adapter.can_deliver_exact_text = MagicMock(return_value=True)
        runner = self._runner(adapter)
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
            recovery = asyncio.create_task(
                runner._redeliver_pending_obligations()
            )
            assert await asyncio.to_thread(
                entered_checkpoint_handoff.wait, 2.0
            )
            assert _row(obligation_id)["state"] == "claimed"
            recovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await recovery
            release_checkpoint_handoff.set()

        adapter.send.assert_not_awaited()
        assert _row(obligation_id)["state"] == "deferred"
        assert _row(obligation_id)["attempts"] == 0

    @pytest.mark.asyncio
    async def test_terminal_failure_does_not_block_later_pending_row(self):
        _record(oid="ob-1", content="first")
        _record(oid="ob-2", content="second")
        _orphan("ob-1")
        _orphan("ob-2")
        adapter = MagicMock()
        adapter.send = AsyncMock(
            side_effect=[
                MagicMock(success=False, error="rejected", raw_response={}),
                MagicMock(success=True, error="", raw_response={}),
            ]
        )
        runner = self._runner(adapter)

        assert await runner._redeliver_pending_obligations() == 1
        assert [call.kwargs["content"] for call in adapter.send.await_args_list] == [
            "first",
            "second",
        ]
        assert _row("ob-1")["state"] == "failed"
        assert _row("ob-2")["state"] == "delivered"

    @pytest.mark.asyncio
    async def test_attempting_redelivers_with_marker(self):
        _record()
        dl.mark_attempting("ob-1")
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        await runner._redeliver_pending_obligations()

        sent = adapter.send.call_args.kwargs
        assert sent["content"].startswith(dl.RECOVERED_MARKER)
        assert sent["content"].endswith("the final answer")

    @pytest.mark.parametrize(
        ("send_success", "ledger_method"),
        [(True, "mark_delivered"), (False, "mark_failed")],
    )
    @pytest.mark.asyncio
    async def test_slow_state_update_does_not_block_event_loop(
        self, send_success, ledger_method
    ):
        import asyncio

        _record()
        _orphan("ob-1")
        runner = self._runner(self._adapter(success=send_success))
        slow_update, event_loop_witness, blocked_event_loop = _blocking_probe()

        with patch.object(dl, ledger_method, side_effect=slow_update):
            await asyncio.gather(
                runner._redeliver_pending_obligations(), event_loop_witness()
            )

        assert blocked_event_loop == []

    @pytest.mark.asyncio
    async def test_clear_resume_pending_before_send_so_a_hang_cannot_also_resume(
        self,
    ):
        """A hung redelivery send must still clear resume_pending.

        Otherwise a timed-out startup-restore gate would schedule resume and
        replay a turn whose answer is already in the ledger (#91969).
        """
        import asyncio

        _record()
        _orphan("ob-1")
        hang = asyncio.Event()

        async def hanging_send(**_kwargs):
            await hang.wait()
            return MagicMock(success=True, error="")

        adapter = MagicMock()
        adapter.send = hanging_send
        runner = self._runner(adapter)
        task = asyncio.create_task(runner._redeliver_pending_obligations())

        deadline = asyncio.get_running_loop().time() + 2
        while runner._async_session_store.clear_resume_pending.await_count == 0:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("resume_pending was not cleared before send")
            await asyncio.sleep(0)

        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )
        assert not task.done()

        hang.set()
        assert await task == 1


class TestAttemptsOnlySpentOnRealSends:
    """``attempts`` is the redelivery budget — it must buy a send.

    ``self.adapters`` only holds a platform after its ``connect()`` succeeded,
    and the sweep claimed every dead-owner row regardless. A platform that
    failed to connect this boot therefore burned one attempt per boot while
    the caller's ``adapter is None`` branch skipped it without sending — so
    after MAX_ATTEMPTS boots the row abandoned having never been sent once,
    losing exactly the response the ledger exists to guarantee. That failure
    correlates with the crash that created the obligation: the network
    trouble that killed the send tends to still be there on the next boot.
    """

    def test_absent_platform_does_not_burn_attempts(self):
        _record(platform="telegram")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            assert dl.sweep_recoverable(deliverable_platforms={"discord"}) == []

        row = dl.debug_rows()
        assert "abandoned" not in row
        with dl._connect() as conn:
            state, attempts = conn.execute(
                "SELECT state, attempts FROM delivery_obligations "
                "WHERE obligation_id=?", ("ob-1",),
            ).fetchone()
        assert attempts == 0, "an unsendable boot must not spend the budget"
        assert state == "attempting"

    def test_row_still_delivers_once_its_platform_returns(self):
        _record(platform="telegram")
        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            dl.sweep_recoverable(deliverable_platforms={"discord"})

        _orphan("ob-1")
        claimed = dl.sweep_recoverable(deliverable_platforms={"telegram"})
        assert len(claimed) == 1
        assert claimed[0]["attempts"] == 1


class TestUnconnectedPlatformKeepsItsBudget:
    """End-to-end through the real runner: boots where the platform failed to
    connect must not consume the row's redelivery budget."""

    @staticmethod
    def _runner_without_slack():
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}  # slack failed to connect this boot
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @pytest.mark.asyncio
    async def test_row_survives_boots_where_its_platform_is_down(self):
        _record(platform="slack")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 1):
            _orphan("ob-1")
            runner = self._runner_without_slack()
            assert await runner._redeliver_pending_obligations() == 0

        assert _row("ob-1")["state"] != "abandoned", (
            "the obligation was abandoned without a single send being attempted"
        )
        assert _row("ob-1")["attempts"] == 0



class TestOwnerAlivePidProbe:
    """_owner_alive's no-start-time fallback must route through
    gateway.status._pid_exists, never a raw ``os.kill(pid, 0)`` probe.

    On Windows ``os.kill(pid, 0)`` is NOT a no-op: CPython maps sig=0 to
    ``GenerateConsoleCtrlEvent(0, pid)`` (bpo-14484), so probing a LIVE pid
    whose start time psutil could not read would Ctrl+C its console group.
    Pattern per the windows-native-support reference: patch
    ``gateway.status._pid_exists``, not ``os.kill``.
    """

    def _no_start_time(self, monkeypatch):
        from gateway import status

        monkeypatch.setattr(status, "get_process_start_time", lambda pid: None)

    def test_alive_when_pid_exists(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
        assert dl._owner_alive(12345, 999) is True

    def test_dead_when_pid_gone(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
        assert dl._owner_alive(12345, 999) is False

    def test_raw_os_kill_probe_never_used(self, monkeypatch):
        """Regression guard: the probe must not touch os.kill when
        gateway.status._pid_exists is importable (i.e. always in-tree)."""
        from gateway import status

        self._no_start_time(monkeypatch)
        calls = []
        monkeypatch.setattr(status, "_pid_exists", lambda pid: calls.append(pid) or True)
        monkeypatch.setattr(
            dl.os, "kill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("raw os.kill probe used"))
        )
        assert dl._owner_alive(4242, 999) is True
        assert calls == [4242]

    def test_probe_exception_means_dead(self, monkeypatch):
        from gateway import status

        self._no_start_time(monkeypatch)

        def boom(pid):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(status, "_pid_exists", boom)
        assert dl._owner_alive(12345, 999) is False
