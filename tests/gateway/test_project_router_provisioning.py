from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.project_router import (
    AccessDeniedError,
    ProjectRouter,
    normalize_project_slug,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("RecuperaCli", "recuperacli"),
        ("DOVCRM", "dovcrm"),
        ("Mulher +Segura", "mulher-segura"),
        ("Sommus — SaaS", "sommus-saas"),
        ("NovoProjeto", "novoprojeto"),
    ],
)
def test_normalize_project_slug_examples(name, expected):
    assert normalize_project_slug(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "🧭"])
def test_normalize_project_slug_rejects_empty_result(name):
    with pytest.raises(ValueError):
        normalize_project_slug(name)


def test_legacy_not_null_workdir_migrates_without_losing_rows(tmp_path: Path):
    db_path = tmp_path / "router.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE projects (
            profile TEXT NOT NULL,
            project_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            board_slug TEXT NOT NULL,
            workdir TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (profile, project_id),
            UNIQUE (profile, slug)
        );
        INSERT INTO projects VALUES (
            'default', 'old-project', 'old', 'old-board', 'C:/old', 'active'
        );
        CREATE TABLE topic_bindings (
            profile TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            is_management INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (profile, platform, chat_id, thread_id),
            FOREIGN KEY (profile, project_id)
                REFERENCES projects(profile, project_id) ON DELETE CASCADE
        );
        INSERT INTO topic_bindings VALUES (
            'default', 'telegram', 'chat', 'thread', 'old-project', 0
        );
        """
    )
    connection.commit()
    connection.close()

    with ProjectRouter(db_path, "default") as router:
        router.upsert_project("new-project", "new", "new-board", None)
        rows = router._connection.execute(
            "SELECT project_id, workdir FROM projects ORDER BY project_id"
        ).fetchall()
        workdir_column = next(
            row for row in router._connection.execute("PRAGMA table_info(projects)")
            if row["name"] == "workdir"
        )
        router.set_acl("chat", "alice", "allow")
        migrated = router.resolve("telegram", "chat", "thread", "alice")

    assert [(row["project_id"], row["workdir"]) for row in rows] == [
        ("new-project", None),
        ("old-project", "C:/old"),
    ]
    assert workdir_column["notnull"] == 0
    assert migrated.project_id == "old-project"


def test_provisioning_is_idempotent_seeds_acl_and_preserves_conflicting_binding(
    tmp_path: Path,
):
    logical_creations = []
    existing_boards = set()
    current_board = "keep-me"

    def create_board(slug, **kwargs):
        if slug not in existing_boards:
            existing_boards.add(slug)
            logical_creations.append((slug, kwargs))
        return {"slug": slug}

    with ProjectRouter(tmp_path / "router.db", "default") as router:
        first = router.provision_topic_project(
            "Sommus — SaaS",
            "Sommus — SaaS",
            "telegram",
            "chat",
            "thread",
            allowed_users={"alice": "allow", "bob": "deny"},
            board_creator=create_board,
        )
        retried = router.provision_topic_project(
            "Sommus — SaaS",
            "Sommus — SaaS",
            "telegram",
            "chat",
            "thread",
            allowed_users={"alice": "allow", "bob": "deny"},
            board_creator=create_board,
        )
        conflict = router.provision_topic_project(
            "Different",
            "Different",
            "telegram",
            "chat",
            "thread",
            board_creator=lambda *args, **kwargs: pytest.fail(
                "conflicting binding must not create another board"
            ),
        )

        assert router.resolve("telegram", "chat", "thread", "alice").slug == "sommus-saas"
        with pytest.raises(AccessDeniedError):
            router.resolve("telegram", "chat", "thread", "bob")

    assert first == retried == conflict
    assert logical_creations == [
        (
            "sommus-saas",
            {"name": "Sommus — SaaS", "default_workdir": None},
        )
    ]
    assert current_board == "keep-me"


def test_management_provisioning_persists_control_plane_without_creating_board(
    tmp_path: Path,
):
    workdir = tmp_path / "team-blue"

    def fail_board_creation(*args, **kwargs):
        pytest.fail("management provisioning must not create a physical board")

    with ProjectRouter(tmp_path / "router.db", "team-blue") as router:
        first = router.provision_topic_project(
            "Team Blue Management",
            "🧭 Gestão",
            "telegram",
            "chat",
            "management-thread",
            workdir=workdir,
            status="active",
            is_management=True,
            allowed_users={"alice": "allow", "bob": "deny"},
            board_creator=fail_board_creation,
        )
        retried = router.provision_topic_project(
            "Team Blue Management",
            "🧭 Gestão",
            "telegram",
            "chat",
            "management-thread",
            workdir=workdir,
            status="active",
            is_management=True,
            allowed_users={"alice": "allow", "bob": "deny"},
            board_creator=fail_board_creation,
        )
        resolved = router.resolve(
            "telegram", "chat", "management-thread", "alice"
        )
        with pytest.raises(AccessDeniedError):
            router.resolve("telegram", "chat", "management-thread", "bob")

    assert first == retried
    assert resolved.project_id == "team-blue-management"
    assert resolved.slug == "team-blue-management"
    assert resolved.board_slug == "team-blue-management"
    assert resolved.workdir == workdir
    assert resolved.status == "active"
    assert resolved.is_management is True


def test_retry_after_project_only_partial_state_creates_missing_board(tmp_path: Path):
    calls = []
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("alpha", "alpha", "alpha", None)
        router.provision_topic_project(
            "Alpha",
            "Alpha",
            "telegram",
            "chat",
            "thread",
            board_creator=lambda slug, **kwargs: calls.append(slug),
        )

    assert calls == ["alpha"]


def test_find_telegram_binding_is_profile_and_chat_scoped(tmp_path: Path):
    db_path = tmp_path / "router.db"
    with ProjectRouter(db_path, "one") as router:
        router.provision_topic_project(
            "Alpha", "Alpha", "telegram", "chat-a", "thread-a",
            board_creator=lambda *args, **kwargs: None,
        )
    with ProjectRouter(db_path, "two") as router:
        router.provision_topic_project(
            "Alpha", "Alpha", "telegram", "chat-a", "thread-b",
            board_creator=lambda *args, **kwargs: None,
        )

    with ProjectRouter(db_path, "one") as router:
        found = router.find_telegram_binding("chat-a", "alpha")
        assert found is not None and found.thread_id == "thread-a"
        assert router.find_telegram_binding("chat-b", "alpha") is None
    with ProjectRouter(db_path, "two") as router:
        found = router.find_telegram_binding("chat-a", "alpha")
        assert found is not None and found.thread_id == "thread-b"


def test_abandon_event_only_releases_matching_unfinalized_claim(tmp_path: Path):
    with ProjectRouter(tmp_path / "router.db", "one") as router:
        assert router.claim_event("telegram", "chat", "message", "operation").claimed
        assert not router.abandon_event("telegram", "other-chat", "message", "operation")
        assert router.abandon_event("telegram", "chat", "message", "operation")
        assert router.claim_event("telegram", "chat", "message", "operation").claimed
        assert router.finalize_event(
            "telegram", "chat", "message", "operation", "telegram-topic:77"
        )
        assert not router.abandon_event("telegram", "chat", "message", "operation")
        duplicate = router.claim_event("telegram", "chat", "message", "operation")
        assert not duplicate.claimed
        assert duplicate.result_ref == "telegram-topic:77"
