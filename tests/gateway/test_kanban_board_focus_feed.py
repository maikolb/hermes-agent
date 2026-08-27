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
