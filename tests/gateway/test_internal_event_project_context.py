"""Internal continuation events resolve their project context read-only.

TARGET_ARCHITECTURE gap 3 (27/08): auto-resumed turns ran without their
board — no mirror card, no dispatch routing, invisible work.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from gateway.run import GatewayRunner, SessionSource, MessageEvent
from gateway.config import Platform
from gateway.platforms.base import MessageType


def _make_router_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (
                profile TEXT NOT NULL, project_id TEXT NOT NULL,
                slug TEXT NOT NULL, board_slug TEXT NOT NULL,
                workdir TEXT, status TEXT NOT NULL,
                PRIMARY KEY (profile, project_id)
            );
            CREATE TABLE topic_bindings (
                profile TEXT NOT NULL, platform TEXT NOT NULL,
                chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                is_management INTEGER NOT NULL DEFAULT 0,
                is_closed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (profile, platform, chat_id, thread_id)
            );
            CREATE TABLE acl_entries (
                profile TEXT NOT NULL, chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
                role TEXT NOT NULL DEFAULT 'admin',
                PRIMARY KEY (profile, chat_id, user_id)
            );
            CREATE TABLE processed_events (
                profile TEXT NOT NULL, platform TEXT NOT NULL,
                chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
                operation TEXT NOT NULL, result_ref TEXT,
                claimed_at INTEGER NOT NULL,
                PRIMARY KEY (profile, platform, chat_id, message_id, operation)
            );
            CREATE TABLE workspace_leases (
                workdir TEXT PRIMARY KEY, holder TEXT, expires_at INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO projects VALUES ('factory','dovcrm','dovcrm','dovcrm',NULL,'active')"
        )
        conn.execute(
            "INSERT INTO topic_bindings VALUES ('factory','telegram','-1001','4','dovcrm',0,0)"
        )
        conn.execute(
            "INSERT INTO acl_entries VALUES ('factory','-1001','996979567','allow','admin')"
        )
        conn.commit()
    finally:
        conn.close()


def _runner(tmp_path: Path) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        project_router=SimpleNamespace(
            enabled=True,
            db_path=tmp_path / "router.db",
            managed_chat_ids=["-1001"],
            implicit_managed_chat_members=False,
        )
    )
    runner._effective_project_router_profile = lambda source: "factory"
    runner._project_router_db_path = lambda source: tmp_path / "router.db"
    return runner


def _source(user_id="996979567", thread="4"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="supergroup",
        thread_id=thread,
        user_id=user_id,
    )


def test_internal_event_resolves_bound_project_readonly(tmp_path):
    _make_router_db(tmp_path / "router.db")
    runner = _runner(tmp_path)
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=_source(), internal=True)

    context, denial = runner._resolve_project_context_for_message(event, _source())

    assert denial is None
    assert context is not None
    assert context.board_slug == "dovcrm"
    assert context.project_id == "dovcrm"
    assert context.is_management is False


def test_internal_event_without_binding_or_identity_stays_unbound(tmp_path):
    _make_router_db(tmp_path / "router.db")
    runner = _runner(tmp_path)

    no_binding = _source(thread="999")
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=no_binding, internal=True)
    context, denial = runner._resolve_project_context_for_message(event, no_binding)
    assert (context, denial) == (None, None)

    no_identity = _source(user_id=None)
    event2 = MessageEvent(text="", message_type=MessageType.TEXT, source=no_identity, internal=True)
    context2, denial2 = runner._resolve_project_context_for_message(event2, no_identity)
    assert (context2, denial2) == (None, None)


def _router_data_snapshot(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("projects", "topic_bindings", "acl_entries")
        }
    finally:
        conn.close()


def test_internal_resolution_never_mutates_router_data(tmp_path):
    _make_router_db(tmp_path / "router.db")
    before = _router_data_snapshot(tmp_path / "router.db")
    runner = _runner(tmp_path)
    for thread in ("4", "777"):
        source = _source(thread=thread)
        event = MessageEvent(
            text="", message_type=MessageType.TEXT, source=source, internal=True
        )
        runner._resolve_project_context_for_message(event, source)

    assert _router_data_snapshot(tmp_path / "router.db") == before
