from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.turn_checkpoint import (
    build_checkpoint_continuation_nudge,
    CheckpointConflictError,
    CheckpointIntegrityError,
    CheckpointWriteError,
    TurnCheckpointStore,
    bind_checkpoint_delivery_obligation,
    checkpoint_delivery_fence,
    checkpoint_delivery_fence_matches,
    checkpoint_is_resumable,
    transcript_hash,
    update_checkpoint_delivery,
)


def _messages(*contents: str) -> list[dict]:
    rows: list[dict] = [{"role": "system", "content": "system"}]
    for index, content in enumerate(contents):
        rows.append({"role": "user" if index % 2 == 0 else "assistant", "content": content})
    return rows


def _store(tmp_path: Path) -> TurnCheckpointStore:
    return TurnCheckpointStore(tmp_path / "checkpoints")


def test_start_turn_writes_versioned_checksumbound_redacted_checkpoint(tmp_path):
    store = _store(tmp_path)
    messages = _messages("implement continuity")

    state = store.start_turn(
        session_id="session-1",
        turn_id="turn-1",
        user_content="implement continuity\napi_key: sk-test-secret-value-123456",
        messages=messages,
        routing={"platform": "telegram", "chat_id": "42", "thread_id": "9"},
    )

    assert state["schema_version"] == 1
    assert state["revision"] == 1
    assert state["phase"] == "turn_started"
    assert state["active_user_turn"]["sha256"]
    assert "sk-test-secret" not in state["active_user_turn"]["content"]
    assert "[REDACTED]" in state["active_user_turn"]["content"]
    assert state["transcript"]["current_hash"] == transcript_hash(messages)
    assert state["payload_sha256"]
    assert store.load("session-1") == state


def test_delivery_update_uses_explicit_profile_checkpoint_root(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "worker"
    checkpoint_root = profile_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic-9"}
    store.start_turn(
        "session-1",
        "turn-1",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    state = store.mark_deliverable(
        "session-1",
        "profile response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-default-home"))

    assert update_checkpoint_delivery(
        "session-1",
        obligation_id="obligation-1",
        status="attempting",
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert update_checkpoint_delivery(
        "session-1",
        obligation_id="obligation-1",
        status="delivered",
        checkpoint_root=checkpoint_root,
        **fence,
    )

    delivered = store.load("session-1")
    assert delivered["delivery"] == {
        "obligation_id": "obligation-1",
        "status": "delivered",
    }
    assert not (tmp_path / "wrong-default-home" / "sessions").exists()


def test_delivery_obligation_binding_is_durable_idempotent_and_first_bind_wins(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "worker"
    checkpoint_root = profile_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic-9"}
    store.start_turn(
        "session-bind",
        "turn-bind",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    composed = store.mark_deliverable(
        "session-bind",
        "bound response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    fence = checkpoint_delivery_fence(composed)
    assert fence is not None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-default-home"))

    def bind(candidate: str) -> tuple[str, bool]:
        return candidate, bind_checkpoint_delivery_obligation(
            "session-bind",
            obligation_id=candidate,
            routing=route,
            checkpoint_root=checkpoint_root,
            **fence,
        )

    outcomes = dict(bind(candidate) for candidate in ("obligation-a", "obligation-b"))

    assert sum(outcomes.values()) == 1
    winner = next(key for key, accepted in outcomes.items() if accepted)
    loser = next(key for key, accepted in outcomes.items() if not accepted)
    bound = store.load("session-bind")
    bound_revision = bound["revision"]
    assert bound["delivery"]["obligation_id"] == winner
    assert bound["delivery"]["status"] == "none"
    assert len(bound["delivery"]["route_sha256"]) == 64

    assert bind_checkpoint_delivery_obligation(
        "session-bind",
        obligation_id=winner,
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert store.load("session-bind")["revision"] == bound_revision
    assert not bind_checkpoint_delivery_obligation(
        "session-bind",
        obligation_id=loser,
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert store.load("session-bind")["revision"] == bound_revision

    assert checkpoint_delivery_fence_matches(
        "session-bind",
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert checkpoint_delivery_fence_matches(
        "session-bind",
        obligation_id=winner,
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert not checkpoint_delivery_fence_matches(
        "session-bind",
        obligation_id=loser,
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert not bind_checkpoint_delivery_obligation(
        "session-bind",
        obligation_id=winner,
        routing=route,
        checkpoint_root=checkpoint_root,
        **{**fence, "content_sha256": "0" * 64},
    )
    assert not bind_checkpoint_delivery_obligation(
        "session-bind",
        obligation_id=winner,
        routing=route,
        checkpoint_root=checkpoint_root,
        **{**fence, "turn_id": "stale-turn"},
    )
    assert not update_checkpoint_delivery(
        "session-bind",
        obligation_id=loser,
        status="attempting",
        checkpoint_root=checkpoint_root,
        **fence,
    )
    final_delivery = store.load("session-bind")["delivery"]
    assert final_delivery["obligation_id"] == winner
    assert final_delivery["status"] == "none"
    assert len(final_delivery["route_sha256"]) == 64
    assert not (tmp_path / "wrong-default-home" / "sessions").exists()


@pytest.mark.parametrize(
    "wrong_route",
    [
        {"platform": "slack", "chat_id": "chat-1", "thread_id": "topic-9"},
        {"platform": "telegram", "chat_id": "chat-2", "thread_id": "topic-9"},
        {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic-10"},
    ],
)
def test_delivery_obligation_rejects_cross_route_binding(tmp_path, wrong_route):
    checkpoint_root = tmp_path / "profile" / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic-9"}
    store.start_turn(
        "session-route",
        "turn-route",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    state = store.mark_deliverable(
        "session-route", "routed response", verification_pending=False
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None

    assert not bind_checkpoint_delivery_obligation(
        "session-route",
        obligation_id="wrong-route-obligation",
        routing=wrong_route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert store.load("session-route")["delivery"]["obligation_id"] is None

    assert bind_checkpoint_delivery_obligation(
        "session-route",
        obligation_id="correct-route-obligation",
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert not checkpoint_delivery_fence_matches(
        "session-route",
        obligation_id="correct-route-obligation",
        routing=wrong_route,
        checkpoint_root=checkpoint_root,
        **fence,
    )


def test_delivery_obligation_cas_reloads_after_competing_bind(tmp_path, monkeypatch):
    checkpoint_root = tmp_path / "profile" / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    competing_store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic-9"}
    store.start_turn(
        "session-race",
        "turn-race",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    state = store.mark_deliverable(
        "session-race", "routed response", verification_pending=False
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None
    original_write = store._write
    injected = False

    def write_after_competing_bind(candidate, *, precommit=None):
        nonlocal injected
        if not injected:
            injected = True
            assert competing_store.bind_delivery_obligation(
                "session-race",
                obligation_id="winner",
                routing=route,
                **fence,
            ) is not None
        return original_write(candidate, precommit=precommit)

    monkeypatch.setattr(store, "_write", write_after_competing_bind)
    assert store.bind_delivery_obligation(
        "session-race",
        obligation_id="loser",
        routing=route,
        **fence,
    ) is None
    delivery = competing_store.load("session-race")["delivery"]
    assert delivery["obligation_id"] == "winner"
    assert len(delivery["route_sha256"]) == 64


def test_fence_requires_binding_when_exact_obligation_is_requested(tmp_path):
    checkpoint_root = tmp_path / "profile" / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "telegram", "chat_id": "chat", "thread_id": ""}
    store.start_turn(
        "session-unbound",
        "turn-unbound",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    state = store.mark_deliverable(
        "session-unbound",
        "unbound response",
        verification_pending=False,
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None

    assert checkpoint_delivery_fence_matches(
        "session-unbound",
        checkpoint_root=checkpoint_root,
        **fence,
    )
    assert not checkpoint_delivery_fence_matches(
        "session-unbound",
        obligation_id="not-bound",
        routing=route,
        checkpoint_root=checkpoint_root,
        **fence,
    )


def test_stream_delivery_uses_explicit_profile_checkpoint_root(tmp_path, monkeypatch):
    from agent.turn_checkpoint import update_checkpoint_stream_delivery

    profile_home = tmp_path / "profiles" / "worker"
    checkpoint_root = profile_home / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "slack", "chat_id": "C1", "thread_id": ""}
    store.start_turn(
        "session-stream",
        "turn-stream",
        "deliver",
        _messages("deliver"),
        routing=route,
    )
    state = store.mark_deliverable(
        "session-stream",
        "streamed response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-default-home"))

    assert update_checkpoint_stream_delivery(
        "session-stream",
        final_response="streamed response",
        **fence,
        checkpoint_root=checkpoint_root,
    )
    state = store.load("session-stream")
    assert state["phase"] == "delivered"
    assert state["delivery"]["status"] == "delivered"
    assert not (tmp_path / "wrong-default-home" / "sessions").exists()


def test_late_stream_ack_cannot_close_new_turn_with_same_text(tmp_path):
    from agent.turn_checkpoint import update_checkpoint_stream_delivery

    checkpoint_root = tmp_path / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    route = {"platform": "slack", "chat_id": "C1", "thread_id": ""}
    store.start_turn(
        "session-stream", "turn-1", "one", _messages("one"), routing=route
    )
    first = store.mark_deliverable(
        "session-stream",
        "same response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    first_fence = checkpoint_delivery_fence(first)
    assert first_fence is not None

    store.start_turn(
        "session-stream", "turn-2", "two", _messages("two"), routing=route
    )
    second = store.mark_deliverable(
        "session-stream",
        "same response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    second_fence = checkpoint_delivery_fence(second)
    assert second_fence is not None

    assert not update_checkpoint_stream_delivery(
        "session-stream",
        final_response="same response",
        checkpoint_root=checkpoint_root,
        **first_fence,
    )
    state = store.load("session-stream")
    assert state["turn_id"] == "turn-2"
    assert state.get("delivery", {}).get("status") != "delivered"

    assert update_checkpoint_stream_delivery(
        "session-stream",
        final_response="same response",
        checkpoint_root=checkpoint_root,
        **second_fence,
    )


def test_best_effort_delivery_is_terminal_without_claiming_exact_ack(tmp_path):
    from agent.turn_checkpoint import update_checkpoint_best_effort_delivery

    checkpoint_root = tmp_path / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    store.start_turn(
        "session-best-effort",
        "turn-1",
        "request",
        _messages("request"),
    )
    state = store.mark_deliverable(
        "session-best-effort",
        "long platform response",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    fence = checkpoint_delivery_fence(state)
    assert fence is not None

    assert update_checkpoint_best_effort_delivery(
        "session-best-effort",
        reported_success=True,
        checkpoint_root=checkpoint_root,
        **fence,
    )

    state = store.load("session-best-effort")
    assert state["phase"] == "terminal"
    assert state["next_action"] == "none"
    assert state["delivery"] == {
        "obligation_id": None,
        "status": "best_effort",
        "reported_success": True,
    }


def test_best_effort_delivery_rejects_stale_fence(tmp_path):
    from agent.turn_checkpoint import (
        CheckpointConflictError,
        update_checkpoint_best_effort_delivery,
    )

    checkpoint_root = tmp_path / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    store.start_turn("session-best-effort", "turn-1", "one", _messages("one"))
    first = store.mark_deliverable(
        "session-best-effort",
        "first",
        verification_pending=False,
        verification_kind="ordinary_final",
    )
    first_fence = checkpoint_delivery_fence(first)
    assert first_fence is not None
    store.start_turn("session-best-effort", "turn-2", "two", _messages("two"))
    store.mark_deliverable(
        "session-best-effort",
        "second",
        verification_pending=False,
        verification_kind="ordinary_final",
    )

    with pytest.raises(CheckpointConflictError, match="older session turn"):
        update_checkpoint_best_effort_delivery(
            "session-best-effort",
            reported_success=True,
            checkpoint_root=checkpoint_root,
            **first_fence,
        )


def test_checkpoint_atomic_write_failure_preserves_previous_revision(tmp_path, monkeypatch):
    store = _store(tmp_path)
    original = store.start_turn(
        session_id="session-1",
        turn_id="turn-1",
        user_content="task",
        messages=_messages("task"),
    )

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("agent.turn_checkpoint.os.replace", fail_replace)
    with pytest.raises(CheckpointWriteError, match="simulated replace failure"):
        store.transition("session-1", phase="planning", next_action="inspect")

    monkeypatch.undo()
    assert store.load("session-1") == original


def test_compare_and_swap_rejects_stale_concurrent_revision(tmp_path):
    store_a = _store(tmp_path)
    store_b = _store(tmp_path)
    store_a.start_turn("session-1", "turn-1", "run", _messages("run"))
    stale = store_a.load("session-1")

    current = store_b.transition(
        "session-1", phase="tool_attempting", next_action="await_tool"
    )
    stale["revision"] = int(stale["revision"]) + 1
    stale["phase"] = "stale_writer"

    with pytest.raises(CheckpointConflictError, match="stale checkpoint write"):
        store_a._write(stale)
    assert store_a.load("session-1")["revision"] == current["revision"]
    assert store_a.load("session-1")["phase"] == "tool_attempting"


def test_older_turn_cannot_overwrite_newer_turn_checkpoint(tmp_path):
    store = _store(tmp_path)
    store.start_turn("session-1", "turn-1", "first", _messages("first"))
    stale = store.load("session-1")
    current = store.start_turn(
        "session-1", "turn-2", "second", _messages("first", "ok", "second")
    )

    stale["revision"] = int(stale["revision"]) + 1
    stale["phase"] = "late_old_turn"
    with pytest.raises(CheckpointConflictError, match="newer turn"):
        store._write(stale)

    assert store.load("session-1")["turn_id"] == current["turn_id"] == "turn-2"


def test_checksum_tamper_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.start_turn(
        session_id="session-1",
        turn_id="turn-1",
        user_content="task",
        messages=_messages("task"),
    )
    path = store.path_for("session-1")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["phase"] = "forged"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError, match="checksum"):
        store.load("session-1")


def test_prepared_checkpoint_recovers_both_crash_boundaries(tmp_path):
    before = _messages("task", "working", "tool request")
    after = _messages("handoff", "tool request")

    not_swapped = _store(tmp_path / "before")
    not_swapped.start_turn("session-1", "turn-1", "tool request", before)
    not_swapped.prepare_compaction("session-1", before, after)
    recovered_before = TurnCheckpointStore(not_swapped.root).restore(
        "session-1", before
    )
    assert recovered_before["compaction"]["state"] == "captured"
    assert recovered_before["recovery"]["resolution"] == "swap_not_applied"

    swapped = _store(tmp_path / "after")
    swapped.start_turn("session-1", "turn-1", "tool request", before)
    swapped.prepare_compaction("session-1", before, after)
    recovered_after = TurnCheckpointStore(swapped.root).restore("session-1", after)
    assert recovered_after["compaction"]["state"] == "committed"
    assert recovered_after["recovery"]["resolution"] == "swap_committed_before_ack"


def test_prepared_checkpoint_rejects_third_transcript_hash(tmp_path):
    store = _store(tmp_path)
    before = _messages("task", "working", "tool request")
    after = _messages("handoff", "tool request")
    divergent = _messages("different transcript")
    store.start_turn("session-1", "turn-1", "tool request", before)
    store.prepare_compaction("session-1", before, after)

    with pytest.raises(CheckpointConflictError, match="neither before nor after"):
        store.restore("session-1", divergent)

    assert store.load("session-1")["compaction"]["state"] == "conflict"


def test_interrupted_tool_becomes_unknown_and_exact_replay_stays_blocked(tmp_path):
    store = _store(tmp_path)
    messages = _messages("deploy")
    store.start_turn("session-1", "turn-1", "deploy", messages)
    store.mark_tool_attempt(
        "session-1",
        call_id="call-1",
        name="terminal",
        arguments={"command": "deploy --release abc"},
    )

    restarted = TurnCheckpointStore(store.root)
    restored = restarted.restore("session-1", messages)
    assert restored["phase"] == "reconcile_required"
    assert restored["pending_tool"] is None
    assert restored["unknown_outcomes"][0]["name"] == "terminal"

    first = restarted.guard_unknown_replay(
        "session-1", "terminal", {"command": "deploy --release abc"}
    )
    second = restarted.guard_unknown_replay(
        "session-1", "terminal", {"command": "deploy --release abc"}
    )
    assert first is not None
    assert "unknown outcome" in first.lower()
    assert second is not None

    fresh_process = TurnCheckpointStore(store.root)
    third = fresh_process.guard_unknown_replay(
        "session-1", "terminal", {"command": "deploy --release abc"}
    )
    assert third is not None


def test_safe_retry_requires_durable_readback_and_is_consumed_once(tmp_path):
    store = _store(tmp_path)
    messages = _messages("write")
    store.start_turn("session-1", "turn-1", "write", messages)
    store.mark_tool_attempt(
        "session-1",
        call_id="write-1",
        name="write_file",
        arguments={"path": "artifact.txt", "content": "one"},
    )
    store.mark_tool_result(
        "session-1",
        call_id="write-1",
        result_summary="process exited before result persistence",
        disposition="unknown_outcome",
    )
    fingerprint = store.load("session-1")["unknown_outcomes"][-1]["fingerprint"]

    with pytest.raises(CheckpointIntegrityError, match="readback"):
        store.reconcile_unknown_outcome(
            "session-1",
            fingerprint=fingerprint,
            disposition="safe_to_retry",
            readback_call_id="missing",
            reconciler_identity="write-file-readback-v1",
            evidence="target absent",
        )

    store.mark_tool_attempt(
        "session-1",
        call_id="readback-1",
        name="read_file",
        arguments={"path": "artifact.txt"},
    )
    store.mark_tool_result(
        "session-1",
        call_id="readback-1",
        result_summary="not found",
    )
    store.reconcile_unknown_outcome(
        "session-1",
        fingerprint=fingerprint,
        disposition="safe_to_retry",
        readback_call_id="readback-1",
        reconciler_identity="write-file-readback-v1",
        evidence="authoritative file read returned not found",
    )

    assert store.guard_unknown_replay(
        "session-1",
        "write_file",
        {"path": "artifact.txt", "content": "one"},
    ) is None
    store.mark_tool_attempt(
        "session-1",
        call_id="write-2",
        name="write_file",
        arguments={"path": "artifact.txt", "content": "one"},
    )
    assert store.guard_unknown_replay(
        "session-1",
        "write_file",
        {"path": "artifact.txt", "content": "one"},
    ) is not None
    store.mark_tool_result(
        "session-1",
        call_id="write-2",
        result_summary="written",
    )
    assert store.guard_unknown_replay(
        "session-1",
        "write_file",
        {"path": "artifact.txt", "content": "one"},
    ) is None


def test_production_tool_middleware_reserves_effect_before_dispatch(
    tmp_path, monkeypatch
):
    from agent import relay_tools
    from agent import tool_executor
    from hermes_cli import middleware

    store = _store(tmp_path)
    store.start_turn("session-1", "turn-1", "write", _messages("write"))
    agent = SimpleNamespace(
        session_id="session-1",
        _turn_checkpoint_store=store,
        _turn_checkpoint_state=store.load("session-1"),
        _tool_guardrails=SimpleNamespace(
            before_call=lambda *_args, **_kwargs: SimpleNamespace(
                allows_execution=True
            )
        ),
        _turns_since_memory=0,
        _iters_since_skill=0,
        _touch_activity=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *a, **k: None)
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda _name, args, callback, **_kwargs: (callback(args), args),
    )
    monkeypatch.setattr(
        middleware,
        "apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        middleware,
        "run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )

    observed = []
    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="write_file",
        function_args={"path": "artifact.txt", "content": "one"},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=lambda args: observed.append(dict(args)) or "written",
    )

    assert outcome.result == "written"
    assert observed == [{"path": "artifact.txt", "content": "one"}]
    pending = store.load("session-1")["pending_tools"]
    assert [row["call_id"] for row in pending] == ["call-1"]

    assert tool_executor._checkpoint_tool_result(
        agent,
        tool_call_id="call-1",
        result_summary="written",
        disposition="completed",
    )
    state = store.load("session-1")
    assert state["pending_tools"] == []
    assert state["completed_tools"][-1]["call_id"] == "call-1"


def test_production_tool_middleware_records_uncertainty_on_abrupt_exception(
    tmp_path, monkeypatch
):
    from agent import relay_tools
    from agent import tool_executor
    from hermes_cli import middleware

    store = _store(tmp_path)
    store.start_turn("session-1", "turn-1", "write", _messages("write"))
    agent = SimpleNamespace(
        session_id="session-1",
        _turn_checkpoint_store=store,
        _turn_checkpoint_state=store.load("session-1"),
        _tool_guardrails=SimpleNamespace(
            before_call=lambda *_args, **_kwargs: SimpleNamespace(
                allows_execution=True
            )
        ),
        _turns_since_memory=0,
        _iters_since_skill=0,
        _touch_activity=lambda *_args, **_kwargs: None,
        _incremental_persistence_failed=False,
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *a, **k: None)
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda _name, args, callback, **_kwargs: (callback(args), args),
    )
    monkeypatch.setattr(
        middleware,
        "apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        middleware,
        "run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )
    sentinel = tmp_path / "effect.txt"

    def effect_then_exit(_args):
        sentinel.write_text("effect", encoding="utf-8")
        raise SystemExit(88)

    with pytest.raises(SystemExit, match="88"):
        tool_executor._run_agent_tool_execution_middleware(
            agent,
            function_name="write_file",
            function_args={"path": str(sentinel), "content": "effect"},
            effective_task_id="task-1",
            tool_call_id="call-1",
            execute=effect_then_exit,
        )

    assert sentinel.read_text(encoding="utf-8") == "effect"
    state = TurnCheckpointStore(store.root).load("session-1")
    assert state["pending_tools"] == []
    assert state["unknown_outcomes"][-1]["call_id"] == "call-1"
    assert TurnCheckpointStore(store.root).guard_unknown_replay(
        "session-1",
        "write_file",
        {"path": str(sentinel), "content": "effect"},
    ) is not None


def test_tool_result_and_pending_deliverable_survive_restart(tmp_path):
    store = _store(tmp_path)
    messages = _messages("build report")
    store.start_turn("session-1", "turn-1", "build report", messages)
    store.mark_tool_attempt(
        "session-1", call_id="call-1", name="read_file", arguments={"path": "a.md"}
    )
    store.mark_tool_result("session-1", call_id="call-1", result_summary="read 10 lines")
    store.mark_deliverable(
        "session-1",
        content="full report\npassword: super-secret-password",
        verification_pending=True,
    )

    restored = TurnCheckpointStore(store.root).restore("session-1", messages)
    assert restored["phase"] == "verification_pending"
    assert restored["pending_tool"] is None
    assert restored["pending_deliverable"]["sha256"]
    assert "full report" in restored["pending_deliverable"]["content"]
    assert "super-secret-password" not in restored["pending_deliverable"]["content"]
    assert restored["verification"]["pending"] is True


def test_commit_compaction_requires_after_hash_readback(tmp_path):
    store = _store(tmp_path)
    before = _messages("task", "working", "current")
    after = _messages("handoff", "current")
    store.start_turn("session-1", "turn-1", "current", before)
    store.prepare_compaction("session-1", before, after)

    with pytest.raises(CheckpointConflictError, match="after transcript"):
        store.commit_compaction("session-1", before)

    committed = store.commit_compaction("session-1", after)
    assert committed["compaction"]["state"] == "committed"
    assert committed["transcript"]["current_hash"] == transcript_hash(after)


def test_transcript_hash_canonicalizes_live_and_persisted_tool_names():
    """The loop emits ``name`` while SessionDB replays ``tool_name``."""
    live = [
        {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call-1",
            "content": "ok",
        }
    ]
    persisted = [
        {
            "role": "tool",
            "tool_name": "read_file",
            "tool_call_id": "call-1",
            "content": "ok",
        }
    ]

    assert transcript_hash(live) == transcript_hash(persisted)


def test_terminal_checkpoint_keeps_delivery_reference_without_payload_duplication(tmp_path):
    store = _store(tmp_path)
    messages = _messages("answer")
    store.start_turn("session-1", "turn-1", "answer", messages)
    store.mark_deliverable("session-1", "final answer", verification_pending=False)
    state = store.mark_terminal(
        "session-1",
        final_response="final answer",
        delivery_obligation_id="obligation-123",
        delivery_status="pending",
    )

    assert state["phase"] == "delivery_pending"
    assert state["delivery"] == {
        "obligation_id": "obligation-123",
        "status": "pending",
    }
    assert state["pending_deliverable"]["sha256"]


def test_stream_delivery_closes_only_the_exact_pending_deliverable(tmp_path):
    store = _store(tmp_path)
    store.start_turn("session-1", "turn-1", "answer", _messages("answer"))
    store.mark_deliverable("session-1", "final answer", verification_pending=False)

    mismatch = store.mark_delivery_if_content_matches(
        "session-1",
        content="different answer",
        obligation_id="stream:wrong",
    )
    assert mismatch is None
    assert store.load("session-1")["phase"] == "deliverable_composed"

    delivered = store.mark_delivery_if_content_matches(
        "session-1",
        content="final answer",
        obligation_id="stream:exact",
    )
    assert delivered is not None
    assert delivered["phase"] == "delivered"
    assert delivered["next_action"] == "none"
    assert delivered["delivery"] == {
        "obligation_id": "stream:exact",
        "status": "delivered",
    }


def test_explicit_tool_effect_disposition_is_preserved(tmp_path):
    store = TurnCheckpointStore(tmp_path)
    store.start_turn("s1", "t1", "run", [{"role": "user", "content": "run"}])
    store.mark_tool_attempt(
        "s1", call_id="c1", name="deploy", arguments={"target": "prod"}
    )

    state = store.mark_tool_batch_results(
        "s1",
        [
            {
                "call_id": "c1",
                "result_summary": "transport disconnected after submission",
                "disposition": "unknown_outcome",
            }
        ],
    )

    assert state["phase"] == "reconcile_required"
    assert state["completed_tools"] == []
    assert state["unknown_outcomes"][-1]["effect_disposition"] == "unknown_outcome"


def test_agent_restart_restores_pending_deliverable_and_unknown_tool(tmp_path):
    from types import SimpleNamespace

    from agent.turn_checkpoint import initialize_agent_turn_checkpoint

    candidate = "exact pending deliverable"

    class FakeSessionDB:
        db_path = tmp_path / "state.db"

        def get_messages(self, session_id, include_inactive=False):
            return [{"role": "assistant", "content": candidate}]

    messages = _messages("continue the operation")
    first = SimpleNamespace(session_id="session-1", _session_db=FakeSessionDB())
    initialize_agent_turn_checkpoint(
        first,
        turn_id="turn-original",
        user_content="continue the operation",
        messages=messages,
    )
    first._turn_checkpoint_store.mark_deliverable(
        "session-1",
        candidate,
        verification_pending=True,
        verification_attempts=1,
    )
    first._turn_checkpoint_store.mark_tool_attempt(
        "session-1",
        call_id="call-side-effect",
        name="write_file",
        arguments={"path": "artifact.txt", "content": "value"},
    )

    restarted = SimpleNamespace(session_id="session-1", _session_db=FakeSessionDB())
    state = initialize_agent_turn_checkpoint(
        restarted,
        turn_id="turn-process-restart",
        user_content="continue the operation",
        messages=messages,
    )

    assert state["turn_id"] == "turn-original"
    assert state["recovery"]["restored"] is True
    assert restarted._restored_pending_deliverable == candidate
    assert state["unknown_outcomes"][0]["name"] == "write_file"
    assert state["pending_tool"] is None


def test_agent_checkpoint_uses_private_gateway_route_fields(tmp_path):
    from types import SimpleNamespace

    from agent.turn_checkpoint import initialize_agent_turn_checkpoint

    class FakeSessionDB:
        db_path = tmp_path / "state.db"

        def get_messages(self, session_id, include_inactive=False):
            return []

    agent = SimpleNamespace(
        session_id="nf-session",
        _session_db=FakeSessionDB(),
        platform="telegram",
        _chat_id="-100-dovcrm",
        _thread_id="kanban-topic",
    )

    state = initialize_agent_turn_checkpoint(
        agent,
        turn_id="nf-turn",
        user_content="mostra o kanban",
        messages=_messages("mostra o kanban"),
    )

    assert state["routing"] == {
        "platform": "telegram",
        "chat_id": "-100-dovcrm",
        "thread_id": "kanban-topic",
        "task_id": "",
    }


def test_synthetic_gateway_resume_restores_unfinished_checkpoint_despite_empty_event(tmp_path):
    store = _store(tmp_path)
    messages = _messages("perform the long operation")
    original = store.start_turn(
        "session-1",
        "turn-original",
        "perform the long operation",
        messages,
    )
    store.transition(
        "session-1",
        phase="planning",
        next_action="inspect_authoritative_target",
    )

    restored = store.start_turn(
        "session-1",
        "synthetic-restart-event",
        "",
        messages,
        resume_existing=True,
    )

    assert restored["turn_id"] == original["turn_id"]
    assert restored["active_user_turn"] == original["active_user_turn"]
    assert restored["next_action"] == "inspect_authoritative_target"
    assert restored["recovery"]["restored"] is True


def test_agent_synthetic_resume_flag_is_consumed_once(tmp_path):
    from types import SimpleNamespace

    from agent.turn_checkpoint import initialize_agent_turn_checkpoint

    class FakeSessionDB:
        db_path = tmp_path / "state.db"

        def get_messages(self, session_id, include_inactive=False):
            return []

    messages = _messages("perform the long operation")
    first = SimpleNamespace(session_id="session-1", _session_db=FakeSessionDB())
    initialize_agent_turn_checkpoint(
        first,
        turn_id="turn-original",
        user_content="perform the long operation",
        messages=messages,
    )
    first._turn_checkpoint_store.transition(
        "session-1",
        phase="planning",
        next_action="continue_original_operation",
    )

    restarted = SimpleNamespace(
        session_id="session-1",
        _session_db=FakeSessionDB(),
        _resume_turn_from_checkpoint=True,
    )
    state = initialize_agent_turn_checkpoint(
        restarted,
        turn_id="synthetic-restart-event",
        user_content="",
        messages=messages,
    )

    assert state["turn_id"] == "turn-original"
    assert state["next_action"] == "continue_original_operation"
    assert restarted._resume_turn_from_checkpoint is False


def test_checkpoint_resumable_requires_unfinished_phase_and_next_action():
    assert checkpoint_is_resumable({"phase": "planning", "next_action": "run tests"})
    assert not checkpoint_is_resumable({"phase": "delivered", "next_action": "run tests"})
    assert not checkpoint_is_resumable({"phase": "planning", "next_action": "none"})


def test_explicit_resume_guard_rejects_false_restore_acknowledgement():
    from types import SimpleNamespace

    agent = SimpleNamespace(
        _gateway_explicit_checkpoint_resume=True,
        _turn_checkpoint_state={
            "phase": "planning",
            "next_action": "implement fix",
            "recovery": {"restored": True},
        },
        _checkpoint_resume_guard_nudges=0,
    )

    nudge = build_checkpoint_continuation_nudge(
        agent,
        "Retomado do checkpoint. A implementação está em execução.",
    )

    assert nudge is not None
    assert "status-only acknowledgement is not a resumed task" in nudge
    assert agent._checkpoint_resume_guard_nudges == 1


def test_explicit_resume_guard_accepts_concrete_result():
    from types import SimpleNamespace

    agent = SimpleNamespace(
        _gateway_explicit_checkpoint_resume=True,
        _turn_checkpoint_state={
            "phase": "planning",
            "next_action": "implement fix",
            "recovery": {"restored": True},
        },
        _checkpoint_resume_guard_nudges=0,
    )

    assert build_checkpoint_continuation_nudge(
        agent,
        "Corrigi o gerador, rodei 18 testes e todos passaram.",
    ) is None


def test_explicit_resume_guard_exhausts_instead_of_accepting_false_status():
    from types import SimpleNamespace

    agent = SimpleNamespace(
        _gateway_explicit_checkpoint_resume=True,
        _turn_checkpoint_state={
            "phase": "planning",
            "next_action": "implement fix",
            "recovery": {"restored": True},
        },
        _checkpoint_resume_guard_nudges=3,
        _checkpoint_resume_guard_exhausted=False,
    )

    assert build_checkpoint_continuation_nudge(
        agent,
        "Continuidade ativa; a implementação está em execução.",
    ) is None
    assert agent._checkpoint_resume_guard_exhausted is True
