"""Ready-abandonment watchdog (operator card t_7872edd5, 28/08).

A ready card nobody will ever claim must produce ONE alert in the
project topic with a detectable reason — silent-but-healthy-looking is
the worst failure mode. Dedupe is durable (a ``watchdog`` comment).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import gateway.kanban_watchers as kw
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        from gateway.platforms.base import SendResult

        self.sent.append(
            {"chat_id": chat_id, "text": text, "metadata": metadata or {}}
        )
        return SendResult(success=True, message_id=str(len(self.sent)))


def _runner(adapter, monkeypatch, *, settings=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._active_profile_name = lambda: "default"
    runner._authorization_adapter = lambda platform, profile: adapter
    runner._kanban_board_display_targets = lambda profiles: {
        "default": [
            {
                "platform": "telegram",
                "chat_id": "chat-1",
                "thread_id": "topic-7",
                "notifier_profile": "default",
            }
        ]
    }
    runner._ready_watchdog_cfg = settings or {
        "enabled": True,
        "threshold": 180.0,
        "default_assignee": "",
    }
    return runner


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    real_connect = kb.connect
    monkeypatch.setattr(
        kb, "connect", lambda db_path=None, board=None: real_connect()
    )
    kb.init_db()
    return tmp_path


def _make_ready(age_seconds, *, assignee=None, workspace_path=None):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="card esquecido", assignee=assignee
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', created_at=?, "
                "workspace_path=? WHERE id=?",
                (int(time.time() - age_seconds), workspace_path, task_id),
            )
    finally:
        conn.close()
    return task_id


def test_stale_unassigned_ready_alerts_once_with_reason(board, monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch)
    task_id = _make_ready(600)

    asyncio.run(runner._kanban_ready_watchdog())

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "Card pronto sem ninguém" in text
    assert task_id in text
    assert "sem assignee" in text
    assert adapter.sent[0]["metadata"].get("thread_id") == "topic-7"

    # durable dedupe: second pass is silent
    asyncio.run(runner._kanban_ready_watchdog())
    assert len(adapter.sent) == 1

    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, task_id)
    finally:
        conn.close()
    assert any(
        getattr(c, "author", "") == "watchdog" for c in comments
    )


def test_fresh_or_claimed_ready_stays_silent(board, monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch)
    _make_ready(30)  # fresh: below threshold
    claimed = _make_ready(600)
    conn = kb.connect()
    try:
        assert kb.claim_task(conn, claimed, claimer="worker:x") is not None
    finally:
        conn.close()

    asyncio.run(runner._kanban_ready_watchdog())

    assert adapter.sent == []


def test_invalid_workspace_reason_beats_generic(board, monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(
        adapter,
        monkeypatch,
        settings={
            "enabled": True,
            "threshold": 180.0,
            "default_assignee": "hermes",
        },
    )
    _make_ready(600, assignee="hermes", workspace_path="/nao/existe/aqui")

    asyncio.run(runner._kanban_ready_watchdog())

    assert len(adapter.sent) == 1
    assert "workspace declarado não existe" in adapter.sent[0]["text"]


def test_watchdog_gate_off_is_silent(board, monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(
        adapter,
        monkeypatch,
        settings={"enabled": False, "threshold": 180.0, "default_assignee": ""},
    )
    _make_ready(600)

    asyncio.run(runner._kanban_ready_watchdog())

    assert adapter.sent == []
