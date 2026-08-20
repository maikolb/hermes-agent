from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.project_router import (
    AccessDeniedError,
    BindingConflictError,
    ProjectRouter,
    UnknownUserError,
    build_team_resource_namespace,
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


def test_dynamic_provisioning_checks_acl_before_any_state_or_board(tmp_path: Path):
    board_calls = []
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.set_acl("chat", "denied", "deny")
        with pytest.raises(UnknownUserError):
            router.provision_topic_project(
                "Unknown", "Unknown", "telegram", "chat", "thread-unknown",
                sender_user_id="missing",
                board_creator=lambda *args, **kwargs: board_calls.append(args),
            )
        with pytest.raises(AccessDeniedError):
            router.provision_topic_project(
                "Denied", "Denied", "telegram", "chat", "thread-denied",
                sender_user_id="denied",
                board_creator=lambda *args, **kwargs: board_calls.append(args),
            )
        assert router._connection.execute(
            "SELECT COUNT(*) FROM projects WHERE profile=?", ("default",)
        ).fetchone()[0] == 0
        assert router._connection.execute(
            "SELECT COUNT(*) FROM topic_bindings WHERE profile=?", ("default",)
        ).fetchone()[0] == 0
    assert board_calls == []


def test_dynamic_provisioning_marks_verified_implicit_sender_as_member(tmp_path: Path):
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        provisioned = router.provision_topic_project(
            "Member Project",
            "Member Project",
            "telegram",
            "chat",
            "thread",
            sender_user_id="member",
            allow_implicit_member=True,
            verified_sender_user_id="member",
            board_creator=lambda *args, **kwargs: None,
        )
        resolved = router.resolve(
            "telegram",
            "chat",
            "thread",
            "member",
            allow_implicit_member=True,
            verified_sender_user_id="member",
        )

    assert provisioned.access == "member"
    assert resolved.access == "member"


def test_dynamic_provisioning_is_atomic_and_same_name_topics_get_stable_slugs(tmp_path: Path):
    boards = set()

    def create_board(slug, **kwargs):
        boards.add(slug)
        return {"slug": slug, **kwargs}

    db_path = tmp_path / "router.db"
    with ProjectRouter(db_path, "default") as router:
        router.set_acl("chat", "alice", "allow")
        first = router.provision_topic_project(
            "Atlas", "Atlas", "telegram", "chat", "81",
            sender_user_id="alice", board_creator=create_board,
        )
        retried = router.provision_topic_project(
            "Atlas renamed", "Atlas renamed", "telegram", "chat", "81",
            sender_user_id="alice", board_creator=create_board,
        )
        second = router.provision_topic_project(
            "Atlas", "Atlas", "telegram", "chat", "82",
            sender_user_id="alice", board_creator=create_board,
        )
        second_retry = router.provision_topic_project(
            "Atlas", "Atlas", "telegram", "chat", "82",
            sender_user_id="alice", board_creator=create_board,
        )
        rows = router._connection.execute(
            "SELECT project_id, slug FROM projects WHERE profile=? ORDER BY slug",
            ("default",),
        ).fetchall()
        bindings = router._connection.execute(
            "SELECT thread_id, project_id FROM topic_bindings WHERE profile=? ORDER BY thread_id",
            ("default",),
        ).fetchall()

    assert first == retried
    assert first.slug == "atlas"
    assert second == second_retry
    assert second.slug.startswith("atlas-") and second.slug != "atlas"
    assert len(second.slug) <= 64
    assert [(row["thread_id"], row["project_id"]) for row in bindings] == [
        ("81", first.project_id), ("82", second.project_id)
    ]
    assert {row["slug"] for row in rows} == {first.slug, second.slug}
    assert boards == {first.board_slug, second.board_slug}


def test_dynamic_project_rolls_back_when_binding_insert_fails(tmp_path: Path):
    db_path = tmp_path / "router.db"
    with ProjectRouter(db_path, "default") as router:
        router.set_acl("chat", "alice", "allow")
        router._connection.execute(
            """CREATE TRIGGER reject_topic_binding
               BEFORE INSERT ON topic_bindings
               BEGIN SELECT RAISE(ABORT, 'binding rejected'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="binding rejected"):
            router.provision_topic_project(
                "Atomic", "Atomic", "telegram", "chat", "91",
                sender_user_id="alice",
                board_creator=lambda *args, **kwargs: pytest.fail(
                    "board must not be created after transaction rollback"
                ),
            )
        assert router._connection.execute(
            "SELECT COUNT(*) FROM projects WHERE profile=?", ("default",)
        ).fetchone()[0] == 0
        assert router._connection.execute(
            "SELECT COUNT(*) FROM topic_bindings WHERE profile=?", ("default",)
        ).fetchone()[0] == 0


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


def test_workspace_root_reuses_unique_normalized_folder_and_creates_missing_one(
    tmp_path: Path,
):
    workspace_root = tmp_path / "projects"
    existing = workspace_root / "Concursa_ai"
    existing.mkdir(parents=True)
    board_calls = []

    def create_board(slug, **kwargs):
        board_calls.append((slug, kwargs))
        return {"slug": slug, **kwargs}

    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.set_acl("chat", "alice", "allow")
        concursa = router.provision_topic_project(
            "Concursa AI", "Concursa AI", "telegram", "chat", "41",
            sender_user_id="alice",
            workspace_root=workspace_root,
            board_creator=create_board,
        )
        created = router.provision_topic_project(
            "Novo Projeto", "Novo Projeto", "telegram", "chat", "42",
            sender_user_id="alice",
            workspace_root=workspace_root,
            board_creator=create_board,
        )

    assert concursa.workdir == existing.resolve()
    assert created.workdir == (workspace_root / created.slug).resolve()
    assert created.workdir.is_dir()
    assert board_calls == [
        (
            "concursa-ai",
            {"name": "Concursa AI", "default_workdir": str(existing.resolve())},
        ),
        (
            "novo-projeto",
            {
                "name": "Novo Projeto",
                "default_workdir": str((workspace_root / "novo-projeto").resolve()),
            },
        ),
    ]


def test_team_namespaces_separate_same_topic_board_and_workspace(tmp_path: Path):
    workspace_root = tmp_path / "projects"
    boards = set()
    results = []

    for profile, chat_id in (("team-one", "-1001"), ("team-two", "-2002")):
        namespace = build_team_resource_namespace(profile, chat_id)
        with ProjectRouter(tmp_path / "router.db", profile) as router:
            results.append((
                namespace,
                router.provision_topic_project(
                    "Alpha Project",
                    "Alpha Project",
                    "telegram",
                    chat_id,
                    "42",
                    workspace_root=workspace_root,
                    resource_namespace=namespace,
                    board_creator=lambda slug, **kwargs: boards.add(slug),
                ),
            ))

    (first_namespace, first), (second_namespace, second) = results
    assert first_namespace != second_namespace
    assert first.board_slug != second.board_slug
    assert first.board_slug.startswith(f"{first_namespace}--")
    assert second.board_slug.startswith(f"{second_namespace}--")
    assert first.workdir == (workspace_root / first_namespace / first.slug).resolve()
    assert second.workdir == (workspace_root / second_namespace / second.slug).resolve()
    assert first.workdir.is_dir() and second.workdir.is_dir()
    assert boards == {first.board_slug, second.board_slug}
    assert "1001" not in first_namespace
    assert "2002" not in second_namespace


def test_workspace_root_ambiguity_fails_closed_before_project_persistence(tmp_path: Path):
    workspace_root = tmp_path / "projects"
    (workspace_root / "Concursa_ai").mkdir(parents=True)
    (workspace_root / "Concursa AI").mkdir()

    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.set_acl("chat", "alice", "allow")
        with pytest.raises(BindingConflictError, match="multiple workspace directories"):
            router.provision_topic_project(
                "Concursa AI", "Concursa AI", "telegram", "chat", "41",
                sender_user_id="alice",
                workspace_root=workspace_root,
                board_creator=lambda *args, **kwargs: pytest.fail(
                    "ambiguous workspace must not create a board"
                ),
            )
        assert router._connection.execute(
            "SELECT COUNT(*) FROM projects WHERE profile=?", ("default",)
        ).fetchone()[0] == 0
        assert router._connection.execute(
            "SELECT COUNT(*) FROM topic_bindings WHERE profile=?", ("default",)
        ).fetchone()[0] == 0


def test_existing_null_workdir_is_repaired_idempotently(tmp_path: Path):
    workspace_root = tmp_path / "projects"
    existing = workspace_root / "Concursa_ai"
    existing.mkdir(parents=True)
    calls = []
    resource_namespace = build_team_resource_namespace("default", "chat")

    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("concursa-ai", "concursa-ai", "concursa-ai", None)
        router.bind_topic("telegram", "chat", "41", "concursa-ai")
        router.set_acl("chat", "alice", "allow")
        context = router.resolve("telegram", "chat", "41", "alice")
        repaired = router.ensure_bound_workspace(
            context,
            workspace_root,
            display_name="Concursa AI",
            resource_namespace=resource_namespace,
            board_creator=lambda slug, **kwargs: calls.append((slug, kwargs)),
        )
        retried = router.ensure_bound_workspace(
            repaired,
            workspace_root,
            display_name="Concursa AI",
            resource_namespace=resource_namespace,
            board_creator=lambda *args, **kwargs: pytest.fail(
                "a repaired workspace must be an idempotent no-op"
            ),
        )

    assert repaired == retried
    assert repaired.workdir == existing.resolve()
    assert calls == [
        (
            "concursa-ai",
            {"name": "Concursa AI", "default_workdir": str(existing.resolve())},
        )
    ]


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
