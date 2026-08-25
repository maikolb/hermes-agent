from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_gateway_transformed_artifact_replay_keeps_exact_fence(tmp_path):
    from agent.turn_checkpoint import TurnCheckpointStore, checkpoint_delivery_fence
    from gateway.run import (
        _is_checkpoint_delivery_replay,
        _reseal_gateway_delivery_response,
    )
    from hermes_state import SessionDB

    storage_home = tmp_path / ".hermes"
    checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    store.start_turn(
        "session-replay",
        "turn-1",
        "answer",
        [{"role": "user", "content": "answer"}],
    )
    initial = store.mark_deliverable(
        "session-replay",
        "plain answer",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    db = SessionDB(storage_home / "state.db")
    db.create_session("session-replay", source="gateway")
    result = {
        "session_id": "session-replay",
        "turn_checkpoint_fence": checkpoint_delivery_fence(initial),
        "turn_checkpoint_root": str(checkpoint_root),
        "storage_home": str(storage_home),
    }
    transformed = "plain answer\n\nfooter already applied"

    first = _reseal_gateway_delivery_response(result, transformed)
    first_state = store.load("session-replay")
    result["turn_exit_reason"] = "checkpoint_delivery_replay"

    assert _is_checkpoint_delivery_replay(result) is True
    second = _reseal_gateway_delivery_response(result, transformed)
    second_state = store.load("session-replay")
    assert second == first
    assert second_state["revision"] == first_state["revision"]
    assert checkpoint_delivery_fence(second_state) == first["fence"]
    active = db.get_messages("session-replay")
    all_rows = db.get_messages("session-replay", include_inactive=True)
    assert active == []
    assert [row["content"] for row in all_rows] == [transformed]
    db.close()


def test_gateway_stale_reseal_cannot_overwrite_new_turn_or_persist_artifact(
    tmp_path,
):
    from agent.turn_checkpoint import (
        CheckpointConflictError,
        TurnCheckpointStore,
        checkpoint_delivery_fence,
    )
    from gateway.run import _reseal_gateway_delivery_response
    from hermes_state import SessionDB

    storage_home = tmp_path / ".hermes"
    checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    db = SessionDB(storage_home / "state.db")
    db.create_session("session-stale-reseal", source="gateway")

    first = store.start_turn(
        "session-stale-reseal",
        "turn-1",
        "first request",
        [{"role": "user", "content": "first request"}],
    )
    first = store.mark_deliverable(
        "session-stale-reseal",
        "first answer",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    stale_result = {
        "session_id": "session-stale-reseal",
        "turn_checkpoint_fence": checkpoint_delivery_fence(first),
        "turn_checkpoint_root": str(checkpoint_root),
        "storage_home": str(storage_home),
    }

    store.start_turn(
        "session-stale-reseal",
        "turn-2",
        "second request",
        [{"role": "user", "content": "second request"}],
    )
    current = store.mark_deliverable(
        "session-stale-reseal",
        "second answer",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    current_fence = checkpoint_delivery_fence(current)

    with pytest.raises(CheckpointConflictError, match="older session turn"):
        _reseal_gateway_delivery_response(
            stale_result,
            "first answer\n\nstale footer",
        )

    unchanged = store.load("session-stale-reseal")
    assert unchanged["revision"] == current["revision"]
    assert checkpoint_delivery_fence(unchanged) == current_fence
    assert unchanged["pending_deliverable"]["content"] == "second answer"
    assert db.get_messages("session-stale-reseal", include_inactive=True) == []
    db.close()


def test_gateway_same_turn_stale_reseal_cannot_overwrite_newer_revision(
    tmp_path,
):
    from agent.turn_checkpoint import (
        CheckpointConflictError,
        TurnCheckpointStore,
        checkpoint_delivery_fence,
    )
    from gateway.run import _reseal_gateway_delivery_response
    from hermes_state import SessionDB

    storage_home = tmp_path / ".hermes"
    checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    db = SessionDB(storage_home / "state.db")
    db.create_session("session-same-turn-stale", source="gateway")

    store.start_turn(
        "session-same-turn-stale",
        "turn-1",
        "request",
        [{"role": "user", "content": "request"}],
    )
    first = store.mark_deliverable(
        "session-same-turn-stale",
        "first answer",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    stale_result = {
        "session_id": "session-same-turn-stale",
        "turn_checkpoint_fence": checkpoint_delivery_fence(first),
        "turn_checkpoint_root": str(checkpoint_root),
        "storage_home": str(storage_home),
    }

    current = store.mark_deliverable(
        "session-same-turn-stale",
        "newer answer from the same turn",
        verification_pending=False,
        verification_kind="ordinary_final_gateway_transform",
    )
    current_fence = checkpoint_delivery_fence(current)

    with pytest.raises(CheckpointConflictError, match="stale checkpoint fence"):
        _reseal_gateway_delivery_response(
            stale_result,
            "first answer\n\nstale footer",
        )

    unchanged = store.load("session-same-turn-stale")
    assert unchanged["revision"] == current["revision"]
    assert checkpoint_delivery_fence(unchanged) == current_fence
    assert (
        unchanged["pending_deliverable"]["content"]
        == "newer answer from the same turn"
    )
    assert db.get_messages("session-same-turn-stale", include_inactive=True) == []
    db.close()


def test_legacy_unfenced_durable_row_is_not_recovered(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: db_path)
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)

    # Simulate a row migrated from the pre-fence schema.  New durable rows
    # cannot be created this way through the public API anymore.
    now = time.time()
    with ledger._connect() as conn:
        conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                session_id, content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, content_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (
                "ob-checkpoint-link",
                "telegram:chat:thread:user",
                "telegram",
                "chat",
                "thread",
                "durable-session-id",
                "owed response",
                now,
                now,
                999999999,
                1,
                ledger.hashlib.sha256(b"owed response").hexdigest(),
            ),
        )

    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    rows = ledger.sweep_recoverable(
        deliverable_platforms={"telegram"},
    )

    assert rows == []
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?",
            ("ob-checkpoint-link",),
        ).fetchone()[0]
    assert state == "legacy_unfenced"


def test_new_durable_row_without_full_fence_is_rejected(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")

    with pytest.raises(
        ledger.DeliveryLedgerIntegrityError,
        match="complete checkpoint fence",
    ):
        ledger.record_obligation(
            obligation_id="unfenced-new-row",
            session_key="telegram:chat:thread:user",
            platform="telegram",
            chat_id="chat",
            thread_id="thread",
            content="owed response",
            session_id="durable-session-id",
        )


def test_fenced_recovery_preserves_durable_session_id(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: db_path)
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    monkeypatch.setattr(
        ledger,
        "_checkpoint_fence_disposition",
        lambda **kwargs: "match",
    )

    ledger.record_obligation(
        obligation_id="ob-checkpoint-link",
        session_key="telegram:chat:thread:user",
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        content="owed response",
        session_id="durable-session-id",
        checkpoint_turn_id="turn-1",
        checkpoint_revision="delivery-1",
        checkpoint_content_sha256=_content_digest("owed response"),
    )

    rows = ledger.sweep_recoverable(deliverable_platforms={"telegram"})

    assert len(rows) == 1
    assert rows[0]["obligation_id"] == "ob-checkpoint-link"
    assert rows[0]["session_id"] == "durable-session-id"
    assert rows[0]["checkpoint_turn_id"] == "turn-1"
    assert rows[0]["checkpoint_revision"] == "delivery-1"
    assert rows[0]["checkpoint_content_sha256"] == _content_digest(
        "owed response"
    )
    assert len(rows[0]["attempt_token"]) == 32
    assert rows[0]["storage_home"] == str(tmp_path.resolve())


@pytest.mark.parametrize("initial_state", ["pending", "deferred"])
def test_durable_pending_or_deferred_requires_matching_current_fence(
    tmp_path, monkeypatch, initial_state
):
    from gateway import delivery_ledger as ledger

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: db_path)
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    monkeypatch.setattr(
        ledger,
        "_checkpoint_fence_disposition",
        lambda **kwargs: "superseded",
    )
    ledger.record_obligation(
        obligation_id="ob-stale-fence",
        session_key="telegram:chat:thread:user",
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        content="stale response",
        session_id="durable-session-id",
        checkpoint_turn_id="turn-old",
        checkpoint_revision="delivery-old",
        checkpoint_content_sha256=_content_digest("stale response"),
    )
    if initial_state == "deferred":
        token = ledger.mark_attempting("ob-stale-fence")
        assert token
        assert ledger.mark_deferred(
            "ob-stale-fence", "transport untouched", attempt_token=token
        )

    assert ledger.sweep_recoverable(deliverable_platforms={"telegram"}) == []
    with sqlite3.connect(db_path) as conn:
        state, attempts = conn.execute(
            "SELECT state, attempts FROM delivery_obligations "
            "WHERE obligation_id='ob-stale-fence'"
        ).fetchone()
    assert state == "superseded"
    assert attempts == 0


@pytest.mark.parametrize("state", ["attempting", "failed"])
def test_durable_started_send_becomes_ambiguous_not_retried(
    tmp_path, monkeypatch, state
):
    from gateway import delivery_ledger as ledger

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: db_path)
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)

    ledger.record_obligation(
        obligation_id="ob-ambiguous",
        session_key="telegram:chat:thread:user",
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        content="owed response",
        session_id="durable-session-id",
        checkpoint_turn_id="turn-1",
        checkpoint_revision="delivery-1",
        checkpoint_content_sha256=_content_digest("owed response"),
    )
    token = ledger.mark_attempting("ob-ambiguous")
    assert token
    if state == "failed":
        assert ledger.mark_failed(
            "ob-ambiguous", "transport reset", attempt_token=token
        )

    assert ledger.sweep_recoverable(deliverable_platforms={"telegram"}) == []
    with sqlite3.connect(db_path) as conn:
        saved = conn.execute(
            "SELECT state, attempt_token FROM delivery_obligations "
            "WHERE obligation_id='ob-ambiguous'"
        ).fetchone()
    assert saved == ("delivery_ambiguous", token)


def test_trusted_storage_home_round_trip(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    default_home = tmp_path / "default"
    trusted_home = tmp_path / "tenant-a"
    monkeypatch.setattr(ledger, "_db_path", lambda: default_home / "state.db")
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    monkeypatch.setattr(
        ledger,
        "_checkpoint_fence_disposition",
        lambda **kwargs: "match",
    )

    ledger.record_obligation(
        obligation_id="ob-namespaced",
        session_key="telegram:chat:thread:user",
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        content="namespaced response",
        session_id="durable-session-id",
        checkpoint_turn_id="turn-1",
        checkpoint_revision="delivery-1",
        checkpoint_content_sha256=_content_digest("namespaced response"),
        storage_home=trusted_home,
    )

    claims = ledger.sweep_recoverable(
        deliverable_platforms={"telegram"}, storage_home=trusted_home
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim["storage_home"] == str(trusted_home.resolve())
    assert ledger.mark_claimed_attempting(
        claim["obligation_id"],
        attempt_token=claim["attempt_token"],
        storage_home=claim["storage_home"],
    )
    assert ledger.mark_delivered(
        claim["obligation_id"],
        attempt_token=claim["attempt_token"],
        storage_home=claim["storage_home"],
    )
    with sqlite3.connect(trusted_home / "state.db") as conn:
        assert conn.execute(
            "SELECT state FROM delivery_obligations"
        ).fetchone()[0] == "delivered"
    assert not (default_home / "state.db").exists()


@pytest.mark.parametrize(
    ("case", "ledger_route"),
    [
        (
            "platform",
            {"platform": "slack", "chat_id": "chat", "thread_id": "thread"},
        ),
        (
            "chat",
            {"platform": "telegram", "chat_id": "other-chat", "thread_id": "thread"},
        ),
        (
            "thread",
            {"platform": "telegram", "chat_id": "chat", "thread_id": "other-thread"},
        ),
    ],
)
def test_ledger_recovery_rejects_route_different_from_checkpoint_binding(
    tmp_path, monkeypatch, case, ledger_route
):
    from agent.turn_checkpoint import (
        TurnCheckpointStore,
        bind_checkpoint_delivery_obligation,
        checkpoint_delivery_fence,
    )
    from gateway import delivery_ledger as ledger

    storage_home = tmp_path / case
    checkpoint_root = storage_home / "sessions" / "turn-checkpoints"
    checkpoint_route = {
        "platform": "telegram",
        "chat_id": "chat",
        "thread_id": "thread",
    }
    store = TurnCheckpointStore(checkpoint_root)
    store.start_turn(
        "route-session",
        "route-turn",
        "deliver",
        [{"role": "user", "content": "deliver"}],
        routing=checkpoint_route,
    )
    state = store.mark_deliverable(
        "route-session", "owed response", verification_pending=False
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None
    obligation_id = f"route-bound-{case}"
    assert bind_checkpoint_delivery_obligation(
        "route-session",
        obligation_id=obligation_id,
        routing=checkpoint_route,
        checkpoint_root=checkpoint_root,
        **fence,
    )

    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    ledger.record_obligation(
        obligation_id=obligation_id,
        session_key="telegram:chat:thread:user",
        platform=ledger_route["platform"],
        chat_id=ledger_route["chat_id"],
        thread_id=ledger_route["thread_id"],
        content="owed response",
        session_id="route-session",
        checkpoint_turn_id=fence["turn_id"],
        checkpoint_revision=fence["deliverable_revision"],
        checkpoint_content_sha256=fence["content_sha256"],
        storage_home=storage_home,
    )

    assert ledger.sweep_recoverable(
        deliverable_platforms={ledger_route["platform"]},
        storage_home=storage_home,
    ) == []
    with sqlite3.connect(storage_home / "state.db") as conn:
        saved = conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
    assert saved == ("superseded",)
