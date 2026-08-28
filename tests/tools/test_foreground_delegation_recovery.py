"""Foreground fan-out crash markers (TARGET_ARCHITECTURE gap 7).

An in-process delegation dies with its hosting process. The durable marker
makes the loss recoverable: on the next start the goals re-enter the
conversation as an outcome-unknown turn instructing re-delegation. Normal
completion deletes the marker and nothing is replayed.
"""

from __future__ import annotations

import queue
import sqlite3

import pytest

from tools import async_delegation as ad
from tools.process_registry import format_process_notification


@pytest.fixture()
def durable_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db)
    return db


def _register(goals=("goal A", "goal B")):
    return ad.register_foreground_delegation(
        goals=list(goals),
        context="contexto do turno",
        role="leaf",
        model="test-model",
        session_key="agent:main:telegram:-1001:4",
        origin_ui_session_id="ui-1",
        origin_session_id="sess-1",
        parent_session_id="parent-42",
        delegation_id="deleg_fg_test",
    )


def _kill_owner(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE async_delegations SET owner_pid = 999999999, "
            "owner_started_at = NULL"
        )
        conn.commit()
    finally:
        conn.close()


def test_marker_survives_owner_death_and_reinjects_goals(durable_db, monkeypatch):
    import gateway.status as gs

    monkeypatch.setattr(gs, "_pid_exists", lambda pid: False)

    deleg_id = _register()
    _kill_owner(durable_db)

    q = queue.Queue()
    restored = ad.restore_undelivered_completions(q)
    assert restored == 1

    evt = q.get_nowait()
    assert evt["delegation_id"] == deleg_id
    assert evt["status"] == "unknown"
    assert evt["mode"] == "foreground"
    assert evt["goals"] == ["goal A", "goal B"]
    assert evt["session_key"] == "agent:main:telegram:-1001:4"
    assert evt["restored"] is True
    assert "Re-delegate the goals" in evt["error"]

    text = format_process_notification(evt)
    assert "goal A" in text
    assert "goal B" in text
    assert "Re-delegate the goals" in text


def test_completed_marker_is_deleted_and_never_replayed(durable_db, monkeypatch):
    import gateway.status as gs

    monkeypatch.setattr(gs, "_pid_exists", lambda pid: False)

    deleg_id = _register()
    ad.complete_foreground_delegation(deleg_id)
    _kill_owner(durable_db)

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 0
    assert q.empty()


def test_sync_fanout_registers_then_clears_crash_marker(durable_db, monkeypatch):
    """delegate_task's synchronous path is wired to the marker lifecycle."""
    import threading
    from unittest.mock import MagicMock, patch

    from tools.delegate_tool import delegate_task

    lifecycle = []
    real_register = ad.register_foreground_delegation
    real_complete = ad.complete_foreground_delegation

    def spy_register(**kwargs):
        lifecycle.append(("register", list(kwargs.get("goals") or [])))
        return real_register(**kwargs)

    def spy_complete(deleg_id):
        lifecycle.append(("complete", deleg_id))
        return real_complete(deleg_id)

    monkeypatch.setattr(ad, "register_foreground_delegation", spy_register)
    monkeypatch.setattr(ad, "complete_foreground_delegation", spy_complete)

    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "k"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "test-model"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file"]
    parent.session_id = "parent-session-1"

    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done", "completed": True,
        "api_calls": 1, "messages": [],
    }
    child._delegate_saved_tool_names = []
    child._credential_pool = None
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.model = "test"

    with patch(
        "tools.delegate_tool._resolve_delegation_credentials",
        return_value={
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        },
    ), patch("tools.delegate_tool._load_config", return_value={}), patch(
        "run_agent.AIAgent", return_value=child
    ):
        delegate_task(goal="tarefa síncrona", parent_agent=parent)

    assert [entry[0] for entry in lifecycle] == ["register", "complete"]
    assert lifecycle[0][1] == ["tarefa síncrona"]

    conn = sqlite3.connect(durable_db)
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM async_delegations"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0


def test_background_recovery_wording_unchanged(durable_db, monkeypatch):
    """A detached background unit keeps its own outcome-unknown wording."""
    import gateway.status as gs

    monkeypatch.setattr(gs, "_pid_exists", lambda pid: False)

    record = {
        "delegation_id": "deleg_bg_test",
        "goal": "background goal",
        "session_key": "k",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    _kill_owner(durable_db)

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    evt = q.get_nowait()
    assert evt.get("mode") is None
    assert "outcome unknown" in evt["error"]
    assert "Re-delegate" not in evt["error"]
