from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.turn_checkpoint import (
    CheckpointWriteError,
    TurnCheckpointStore,
    transcript_hash,
)


def _agent(db, session_id: str, *, in_place: bool):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_in_place = in_place
    agent.context_compressor.compress = lambda *a, **k: [
        {"role": "user", "content": "[CONTEXT COMPACTION] durable summary"},
        {"role": "assistant", "content": "continue"},
    ]
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor.compression_count = 1
    return agent


def _seed(db, sid: str) -> list[dict[str, str]]:
    db.create_session(sid, "cli", model="test/model")
    messages = []
    for index in range(8):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"message {index}"
        messages.append({"role": role, "content": content})
        db.append_message(sid, role, content)
    return messages


def test_in_place_compaction_commits_checkpoint_after_db_readback(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    sid = "checkpoint-in-place"
    messages = _seed(db, sid)
    agent = _agent(db, sid, in_place=True)

    compressed, _ = compress_context(
        agent, messages, approx_tokens=100_000, system_message="sys"
    )

    state = agent._turn_checkpoint_store.load(sid)
    live = db.get_messages_as_conversation(sid)
    assert state["compaction"]["state"] == "committed"
    assert state["phase"] == "turn_active"
    assert state["transcript"]["current_hash"] == transcript_hash(live)
    assert transcript_hash(compressed) == transcript_hash(live)


def test_in_place_compaction_round_trips_live_tool_result_name(tmp_path):
    """Regression: live tool results use ``name`` but SQLite stores ``tool_name``."""
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    sid = "checkpoint-tool-name-roundtrip"
    messages = _seed(db, sid)
    agent = _agent(db, sid, in_place=True)
    agent.context_compressor.compress = lambda *a, **k: [
        {"role": "user", "content": "[CONTEXT COMPACTION] durable summary"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call-1",
            "content": "ok",
        },
    ]

    compressed, _ = compress_context(
        agent, messages, approx_tokens=100_000, system_message="sys"
    )

    live = db.get_messages_as_conversation(sid)
    assert live[-1]["tool_name"] == "read_file"
    assert transcript_hash(compressed) == transcript_hash(live)
    assert agent._turn_checkpoint_store.load(sid)["compaction"]["state"] == "committed"


def test_prepare_failure_aborts_before_transcript_swap(tmp_path, monkeypatch):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    sid = "checkpoint-prepare-fail"
    messages = _seed(db, sid)
    agent = _agent(db, sid, in_place=True)

    def fail_prepare(self, *args, **kwargs):
        raise CheckpointWriteError("disk unavailable")

    monkeypatch.setattr(TurnCheckpointStore, "prepare_compaction", fail_prepare)
    with pytest.raises(CheckpointWriteError, match="disk unavailable"):
        compress_context(agent, messages, approx_tokens=100_000, system_message="sys")

    live = db.get_messages_as_conversation(sid)
    assert [m["content"] for m in live] == [m["content"] for m in messages]
    assert all(row.get("active", 1) for row in db.get_messages(sid, include_inactive=True))


def test_commit_ack_failure_restores_original_active_transcript(tmp_path, monkeypatch):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    sid = "checkpoint-commit-fail"
    messages = _seed(db, sid)
    agent = _agent(db, sid, in_place=True)

    def fail_commit(self, *args, **kwargs):
        raise CheckpointWriteError("ack failed")

    monkeypatch.setattr(TurnCheckpointStore, "commit_compaction", fail_commit)
    with pytest.raises(CheckpointWriteError, match="original transcript restored"):
        compress_context(agent, messages, approx_tokens=100_000, system_message="sys")

    live = db.get_messages_as_conversation(sid)
    assert [m["content"] for m in live] == [m["content"] for m in messages]
    state = agent._turn_checkpoint_store.restore(sid, live)
    assert state["recovery"]["resolution"] in {"swap_not_applied", "none"}


def test_rotation_migrates_checkpoint_to_child_session(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    parent = "checkpoint-rotation-parent"
    messages = _seed(db, parent)
    agent = _agent(db, parent, in_place=False)

    compressed, _ = compress_context(
        agent, messages, approx_tokens=100_000, system_message="sys"
    )

    child = agent.session_id
    assert child != parent
    parent_state = agent._turn_checkpoint_store.load(parent)
    child_state = agent._turn_checkpoint_store.load(child)
    assert parent_state["phase"] == "superseded"
    assert child_state["parent_session_id"] == parent
    assert child_state["compaction"]["state"] == "committed"
    assert child_state["transcript"]["current_hash"] == transcript_hash(
        db.get_messages_as_conversation(child)
    )
    assert transcript_hash(compressed) == child_state["transcript"]["current_hash"]


def test_rotation_checkpoint_failure_reopens_parent_and_closes_child(
    tmp_path, monkeypatch
):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    parent = "checkpoint-rotation-rollback"
    messages = _seed(db, parent)
    agent = _agent(db, parent, in_place=False)

    child_ids = []

    def fail_migration(self, old_session_id, new_session_id, active_messages):
        child_ids.append(new_session_id)
        raise CheckpointWriteError("migration ack failed")

    monkeypatch.setattr(TurnCheckpointStore, "migrate_session", fail_migration)
    with pytest.raises(CheckpointWriteError, match="migration ack failed"):
        compress_context(
            agent, messages, approx_tokens=100_000, system_message="sys"
        )

    assert agent.session_id == parent
    parent_row = db.get_session(parent)
    assert parent_row is not None
    assert parent_row.get("ended_at") is None
    assert len(child_ids) == 1
    child_row = db.get_session(child_ids[0])
    assert child_row is not None
    assert child_row.get("ended_at") is not None
