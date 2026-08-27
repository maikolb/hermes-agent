"""Behavioral coverage for busy-topic parallel-by-default Kanban intake."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.kanban_watchers import (
    _classify_parallel_intake_message,
    _resolve_parallel_by_default,
)
from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


def _event(text: str, *, message_id: str = "message-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="77",
            user_id="owner",
        ),
        message_id=message_id,
    )


def _parallel_runner(config: dict):
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter._send_with_retry = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._adapter_for_source = lambda _source: adapter
    runner._active_profile_name = lambda: "default"
    runner._kanban_notifier_profile = None
    runner._kanban_parallel_dispatch_config = lambda _source: config
    runner._resolve_project_context_for_message = lambda _event, _source: (
        SimpleNamespace(
            board_slug="default",
            project_id="",
            is_management=False,
        ),
        None,
    )
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._thread_metadata_for_source = lambda source, anchor: {
        "thread_id": source.thread_id,
    }
    return runner, adapter


def _created_task_id(adapter) -> str:
    content = adapter._send_with_retry.call_args.kwargs["content"]
    match = re.search(r"\b(t_[0-9a-f]+)\b", content)
    assert match is not None, content
    return match.group(1)


def test_classifies_independent_task_and_contextual_correction():
    assert (
        _classify_parallel_intake_message("investigar bug X durante o reparo do edital")
        == "new_task"
    )
    assert (
        _classify_parallel_intake_message("reverte isso que você acabou de fazer")
        == "steer"
    )
    assert _classify_parallel_intake_message("qual é o status?") == "steer"
    assert _classify_parallel_intake_message("/stop") == "steer"


def test_parallel_gate_explicit_value_wins_over_worker_rotation_fallback():
    assert _resolve_parallel_by_default({}) is False
    assert _resolve_parallel_by_default({"display": {"worker_rotation": True}}) is True
    assert (
        _resolve_parallel_by_default({
            "dispatch": {"parallel_by_default": False},
            "display": {"worker_rotation": True},
        })
        is False
    )
    assert (
        _resolve_parallel_by_default({"dispatch": {"parallel_by_default": True}})
        is True
    )


@pytest.mark.asyncio
async def test_independent_busy_message_creates_card_and_spawns_within_limit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "parallel.db"))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    config = {
        "dispatch": {"parallel_by_default": True},
        "kanban": {"max_in_progress": 2},
    }
    runner, adapter = _parallel_runner(config)
    event = _event("investigar bug X durante o reparo do edital")
    event.source.profile = "hermes-project-factory"

    handled = await runner._kanban_parallel_dispatch_busy_message(
        event,
        "principal-session",
    )

    assert handled is True
    task_id = _created_task_id(adapter)
    content = adapter._send_with_retry.call_args.kwargs["content"]
    assert "next tick" in content
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.assignee == "hermes-project-factory"
        subscriptions = kb.list_notify_subs(conn, task_id=task_id)
        assert len(subscriptions) == 1
        assert subscriptions[0]["notifier_profile"] == "hermes-project-factory"

        spawned = []
        result = kb.dispatch_once(
            conn,
            board="default",
            max_in_progress=2,
            spawn_fn=lambda claimed, *_args, **_kwargs: (
                spawned.append(claimed.id) or 4242
            ),
        )
        assert len(result.spawned) == 1
        assert spawned == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_independent_busy_message_queues_above_worker_limit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "parallel-capped.db"))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    conn = kb.connect()
    try:
        active = kb.create_task(conn, title="already running", assignee="default")
        assert kb.claim_task(conn, active, claimer="worker:active") is not None
    finally:
        conn.close()

    config = {
        "dispatch": {"parallel_by_default": True},
        "kanban": {"max_in_progress": 1},
    }
    runner, adapter = _parallel_runner(config)
    handled = await runner._kanban_parallel_dispatch_busy_message(
        _event("implementar validação Y", message_id="message-2"),
        "principal-session",
    )

    assert handled is True
    task_id = _created_task_id(adapter)
    content = adapter._send_with_retry.call_args.kwargs["content"]
    assert "capacity is full" in content
    assert "position: 1" in content
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "ready"
        result = kb.dispatch_once(
            conn,
            board="default",
            max_in_progress=1,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail(
                "dispatcher must not spawn above the configured limit"
            ),
        )
        assert len(result.spawned) == 0
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "config"),
    (
        (
            "reverte isso que você acabou de fazer",
            {"dispatch": {"parallel_by_default": True}},
        ),
        (
            "investigar bug X durante o reparo do edital",
            {"dispatch": {"parallel_by_default": False}},
        ),
    ),
)
async def test_correction_or_disabled_gate_keeps_existing_steer_behavior(
    monkeypatch,
    message,
    config,
):
    import gateway.run as run_module

    monkeypatch.setattr(run_module, "_load_gateway_config", lambda: config)
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._busy_input_mode = "steer"
    runner._busy_text_mode = "interrupt"
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._is_user_authorized = lambda _source: True
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    event = _event(message)
    session_key = "principal-session"
    agent = MagicMock()
    agent.steer.return_value = True
    runner._running_agents[session_key] = agent

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    agent.steer.assert_called_once_with(event.text)
    agent.interrupt.assert_not_called()
    content = adapter._send_with_retry.call_args.kwargs["content"]
    assert "Steered" in content
