"""Board-level worker-focus feed (rotation without notify subscriptions).

27/08 incident: worker rotation was fed only by task-level notify
subscriptions, and the notifier's zero-subscription early exit skipped
subscription-less boards entirely, so their workers never rotated into the
topic display. The feed now resolves display targets from the project
router's persisted topic bindings, independent of subscriptions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _kanban_worker_focus_key,
)


def _make_router_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (
                profile TEXT NOT NULL,
                project_id TEXT NOT NULL,
                slug TEXT NOT NULL,
                board_slug TEXT NOT NULL,
                workdir TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY (profile, project_id)
            );
            CREATE TABLE topic_bindings (
                profile TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                is_management INTEGER NOT NULL DEFAULT 0,
                is_closed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (profile, platform, chat_id, thread_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO projects VALUES "
            "('factory', 'dovcrm', 'dovcrm', 'dovcrm', NULL, 'active')"
        )
        conn.execute(
            "INSERT INTO topic_bindings VALUES "
            "('factory', 'telegram', '-1001', '77', 'dovcrm', 0, 0)"
        )
        conn.execute(
            "INSERT INTO projects VALUES "
            "('factory', 'mgmt', 'mgmt', 'mgmt-board', NULL, 'active')"
        )
        conn.execute(
            "INSERT INTO topic_bindings VALUES "
            "('factory', 'telegram', '-1001', '99', 'mgmt', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()


def _fake_runner(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            project_router=SimpleNamespace(enabled=enabled, db_path=None)
        )
    )


def test_display_targets_come_from_router_bindings(tmp_path, monkeypatch):
    _make_router_db(tmp_path / "project_router.db")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: tmp_path
    )

    targets = GatewayKanbanWatchersMixin._kanban_board_display_targets(
        _fake_runner(), {"factory"}
    )

    assert list(targets) == ["dovcrm"], "management bindings must not feed focus"
    assert targets["dovcrm"] == [
        {
            "platform": "telegram",
            "chat_id": "-1001",
            "thread_id": "77",
            "notifier_profile": "factory",
        }
    ]


def test_display_targets_empty_when_router_disabled(tmp_path, monkeypatch):
    _make_router_db(tmp_path / "project_router.db")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: tmp_path
    )

    targets = GatewayKanbanWatchersMixin._kanban_board_display_targets(
        _fake_runner(enabled=False), {"factory"}
    )

    assert targets == {}


def test_focus_apply_accepts_synthetic_target_rows():
    """A worker on a subscription-less board must enter the rotation bucket."""
    runner = SimpleNamespace()
    target = {
        "platform": "telegram",
        "chat_id": "-1001",
        "thread_id": "77",
        "notifier_profile": "factory",
    }
    task = SimpleNamespace(status="running", current_run_id=42, id="t_wave4", started_at=1.0)

    GatewayKanbanWatchersMixin._kanban_focus_apply_rows(
        runner,
        [
            {
                "sub": dict(target),
                "task": task,
                "board": "dovcrm",
                "bootstrap": True,
                "events": [],
            }
        ],
    )

    active = runner._kanban_worker_focus_active
    key = _kanban_worker_focus_key("dovcrm", target)
    assert key in active
    assert "t_wave4" in active[key]

    GatewayKanbanWatchersMixin._kanban_focus_apply_rows(
        runner,
        [
            {
                "sub": dict(target),
                "task": task,
                "board": "dovcrm",
                "bootstrap": False,
                "events": [SimpleNamespace(kind="completed", id=9, created_at=0.0)],
            }
        ],
    )

    assert key not in runner._kanban_worker_focus_active


def test_rotation_renders_without_principal_via_synthetic_scope(tmp_path, monkeypatch):
    """Production case: gateway restarted, workers run, nobody spoke yet."""
    import asyncio
    import time as _time

    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "focus-synth.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "worker_rotation": True,
                "platforms": {"telegram": {"tool_progress": "all"}},
            },
        },
    )
    monkeypatch.setattr(
        "gateway.run._load_gateway_config", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        "gateway.kanban_watchers._load_worker_focus_config",
        lambda profile, load_default: {
            "display": {
                "worker_rotation": True,
                "platforms": {"telegram": {"tool_progress": "all"}},
            },
        },
    )
    monkeypatch.setattr(
        kb, "read_worker_log", lambda task_id, **_kw: f"Query: {task_id}\n"
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="restart survivor", assignee="worker", project_id="dovcrm"
        )
        assert kb.claim_task(conn, task_id, claimer="worker:x") is not None
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(_time.time()) - 120, task_id),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    class RecordingAdapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, metadata=None):
            self.sent.append({"chat_id": chat_id, "text": text})
            return SimpleNamespace(success=True, message_id="501")

        async def edit_message(self, chat_id, message_id, content, **kw):
            return SimpleNamespace(success=True, message_id=str(message_id))

        async def delete_message(self, chat_id, message_id, **kw):
            return True

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._authorization_adapter = lambda platform, profile=None: adapter
    runner._is_session_running = lambda _key: False
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "factory"
    )
    target = {
        "platform": "telegram",
        "chat_id": "-1001",
        "thread_id": "4",
        "notifier_profile": "factory",
    }
    monkeypatch.setattr(
        GatewayRunner,
        "_kanban_board_display_targets",
        lambda self, profiles: {"dovcrm": [dict(target)]},
    )

    GatewayRunner._kanban_focus_apply_rows(
        runner,
        [
            {
                "sub": dict(target),
                "task": task,
                "board": "dovcrm",
                "bootstrap": True,
                "events": [],
            }
        ],
    )
    asyncio.run(GatewayRunner._kanban_refresh_worker_focus(runner))

    assert any("Now following worker" in item["text"] for item in adapter.sent), (
        "worker must render with no principal claim (synthetic scope)"
    )
    lane = ("telegram", "-1001", "4", "factory")
    scope = runner._kanban_worker_display_scopes[lane]
    assert scope.get("synthetic") is True

    # A real principal claim replaces the synthetic scope and fences renders.
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="4",
        profile="factory",
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: "agent:factory:telegram:group:-1001:4"
    asyncio.run(
        GatewayRunner._kanban_claim_worker_display(
            runner, source, board="dovcrm", project_id="dovcrm"
        )
    )
    replaced = runner._kanban_worker_display_scopes[lane]
    assert not replaced.get("synthetic")
    assert replaced["claim_sequence"] > scope["claim_sequence"]
