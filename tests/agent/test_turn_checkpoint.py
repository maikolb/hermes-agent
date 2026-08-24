from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.turn_checkpoint import (
    build_checkpoint_continuation_nudge,
    CheckpointConflictError,
    CheckpointIntegrityError,
    CheckpointWriteError,
    TurnCheckpointStore,
    checkpoint_is_resumable,
    transcript_hash,
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


def test_interrupted_tool_becomes_unknown_and_exact_replay_is_blocked_once(tmp_path):
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
    assert second is None


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
