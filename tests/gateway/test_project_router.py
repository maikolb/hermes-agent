from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from gateway.project_router import (
    AccessDeniedError,
    BindingConflictError,
    LeaseNotOwnedError,
    ProjectRouter,
    UnknownBindingError,
    UnknownUserError,
)


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def configured_router(db_path: Path, workdir: Path, *, profile: str = "default"):
    router = ProjectRouter(db_path, profile)
    router.upsert_project("project-1", "alpha", "alpha-board", workdir)
    router.bind_topic("telegram", 100, 200, "project-1", is_management=True)
    return router


def test_acl_is_fail_closed_and_deny_replaces_prior_allow(tmp_path: Path) -> None:
    with configured_router(tmp_path / "router.db", tmp_path / "workspace") as router:
        router.set_acl(100, 7, "allow")
        assert router.resolve("telegram", 100, 200, 7).access == "allow"

        with pytest.raises(UnknownUserError):
            router.resolve("telegram", 100, 200, 8)

        router.set_acl(100, 7, "deny")
        with pytest.raises(AccessDeniedError):
            router.resolve("telegram", 100, 200, 7)


def test_implicit_member_requires_matching_verified_sender_and_deny_wins(
    tmp_path: Path,
) -> None:
    with configured_router(tmp_path / "router.db", tmp_path / "workspace") as router:
        with pytest.raises(UnknownUserError):
            router.resolve("telegram", 100, 200, 8)
        with pytest.raises(UnknownUserError):
            router.resolve(
                "telegram",
                100,
                200,
                8,
                allow_implicit_member=True,
                verified_sender_user_id=9,
            )

        implicit = router.resolve(
            "telegram",
            100,
            200,
            8,
            allow_implicit_member=True,
            verified_sender_user_id=8,
        )
        assert implicit.sender_user_id == "8"
        assert implicit.access == "member"

        router.set_acl(100, 8, "deny")
        with pytest.raises(AccessDeniedError):
            router.resolve(
                "telegram",
                100,
                200,
                8,
                allow_implicit_member=True,
                verified_sender_user_id=8,
            )


def test_authorize_sender_exposes_capability_without_topic_binding(tmp_path: Path) -> None:
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        assert router.authorize_sender(
            "team",
            "member",
            allow_implicit_member=True,
            verified_sender_user_id="member",
        ) == "member"

        router.set_acl("team", "admin", "allow")
        assert router.authorize_sender(
            "team",
            "admin",
            allow_implicit_member=True,
            verified_sender_user_id="admin",
        ) == "allow"

        router.set_acl("team", "blocked", "deny")
        with pytest.raises(AccessDeniedError):
            router.authorize_sender(
                "team",
                "blocked",
                allow_implicit_member=True,
                verified_sender_user_id="blocked",
            )


def test_explicit_acl_separates_member_from_admin_capability(tmp_path: Path) -> None:
    with ProjectRouter(tmp_path / "router.db", "pf") as router:
        router.set_acl("team", "member", "allow", role="member")
        router.set_acl("team", "admin", "allow")

        assert router.authorize_sender("team", "member") == "member"
        assert router.authorize_sender("team", "admin") == "allow"


def test_unknown_topic_fails_closed(tmp_path: Path) -> None:
    with configured_router(tmp_path / "router.db", tmp_path / "workspace") as router:
        router.set_acl(100, 7, "allow")
        with pytest.raises(UnknownBindingError):
            router.resolve("telegram", 100, 999, 7)


def test_bind_existing_topic_propagates_implicit_member_capability(tmp_path: Path) -> None:
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "alpha-board", tmp_path / "alpha")
        context = router.bind_existing_topic(
            "telegram",
            "chat",
            "thread",
            "alpha",
            "member",
            allow_implicit_member=True,
            verified_sender_user_id="member",
        )

    assert context.project_id == "project-1"
    assert context.access == "member"


def test_resolution_values_persist_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    workdir = tmp_path / "folder" / ".." / "workspace"
    router = configured_router(db_path, workdir)
    router.set_acl("100", "7", "allow")
    first = router.resolve("telegram", "100", "200", "7")
    router.close()

    with ProjectRouter(db_path, "default") as reopened:
        second = reopened.resolve("telegram", 100, 200, 7)

    assert second == first
    assert second.project_id == "project-1"
    assert second.slug == "alpha"
    assert second.board_slug == "alpha-board"
    assert second.workdir == workdir.resolve(strict=False)
    assert second.status == "active"
    assert second.is_management is True


def test_upserts_and_duplicate_bindings_are_idempotent_but_rebind_is_explicit(
    tmp_path: Path,
) -> None:
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "board-a", tmp_path / "one")
        router.upsert_project("project-1", "alpha", "board-a", tmp_path / "one")
        router.bind_topic("telegram", "chat", "thread", "project-1")
        router.bind_topic("telegram", "chat", "thread", "project-1")

        router.upsert_project("project-2", "beta", "board-b", tmp_path / "two")
        with pytest.raises(BindingConflictError):
            router.bind_topic("telegram", "chat", "thread", "project-2")

        router.bind_topic(
            "telegram", "chat", "thread", "project-2", replace=True
        )
        router.set_acl("chat", "user", "allow")
        assert router.resolve("telegram", "chat", "thread", "user").project_id == "project-2"


def test_event_claim_is_idempotent_and_returns_final_result(tmp_path: Path) -> None:
    with ProjectRouter(tmp_path / "router.db", "default", now=lambda: 100) as router:
        first = router.claim_event("telegram", 1, 2, "dispatch")
        duplicate = router.claim_event("telegram", 1, 2, "dispatch")
        assert first.claimed is True
        assert duplicate.claimed is False
        assert duplicate.result_ref is None

        assert router.finalize_event("telegram", 1, 2, "dispatch", "run:42") is True
        assert router.finalize_event("telegram", 1, 2, "dispatch", "run:99") is False
        finalized = router.claim_event("telegram", 1, 2, "dispatch")
        assert finalized.claimed is False
        assert finalized.result_ref == "run:42"


def test_lease_lifecycle_contention_expiry_and_release(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    workspace = tmp_path / "workspace"
    clock = Clock(1_000)
    with ProjectRouter(db_path, "default", now=clock) as first, ProjectRouter(
        db_path, "default", now=clock
    ) as second:
        acquired = first.acquire_lease(workspace, "owner-a", "run-a", 10)
        assert acquired.acquired is True
        assert acquired.expires_at == 1_010

        blocked = second.acquire_lease(workspace, "owner-b", "run-b", 10)
        assert blocked.acquired is False
        assert (blocked.owner_id, blocked.run_id) == ("owner-a", "run-a")

        clock.value = 1_005
        renewed = first.renew_lease(workspace, "owner-a", "run-a", 20)
        assert renewed.expires_at == 1_025
        with pytest.raises(LeaseNotOwnedError):
            second.release_lease(workspace, "owner-b", "run-b")

        clock.value = 1_025
        takeover = second.acquire_lease(workspace, "owner-b", "run-b", 5)
        assert takeover.acquired is True
        assert takeover.expires_at == 1_030
        assert second.release_lease(workspace, "owner-b", "run-b") is True
        assert second.release_lease(workspace, "owner-b", "run-b") is False


def test_two_routers_contending_yield_one_event_and_lease_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    workspace = tmp_path / "workspace"
    routers = [
        ProjectRouter(db_path, "default", now=lambda: 500),
        ProjectRouter(db_path, "default", now=lambda: 500),
    ]
    event_barrier = Barrier(2)

    def claim(index: int) -> bool:
        event_barrier.wait()
        return routers[index].claim_event("discord", "c", "m", "route").claimed

    lease_barrier = Barrier(2)

    def lease(index: int) -> bool:
        lease_barrier.wait()
        return routers[index].acquire_lease(
            workspace, f"owner-{index}", f"run-{index}", 30
        ).acquired

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            event_results = list(pool.map(claim, range(2)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            lease_results = list(pool.map(lease, range(2)))
    finally:
        for router in routers:
            router.close()

    assert sorted(event_results) == [False, True]
    assert sorted(lease_results) == [False, True]


def test_profiles_are_isolated_in_one_database(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    with ProjectRouter(db_path, "profile-a") as first, ProjectRouter(
        db_path, "profile-b"
    ) as second:
        for router, board in ((first, "board-a"), (second, "board-b")):
            router.upsert_project("same-id", "same-slug", board, tmp_path / board)
            router.bind_topic("telegram", "same-chat", "same-thread", "same-id")
            router.set_acl("same-chat", "same-user", "allow")

        assert first.resolve(
            "telegram", "same-chat", "same-thread", "same-user"
        ).board_slug == "board-a"
        assert second.resolve(
            "telegram", "same-chat", "same-thread", "same-user"
        ).board_slug == "board-b"

        assert first.claim_event("telegram", "same-chat", "m", "op").claimed is True
        assert second.claim_event("telegram", "same-chat", "m", "op").claimed is True

        second.set_acl("same-chat", "same-user", "deny")
        assert first.resolve(
            "telegram", "same-chat", "same-thread", "same-user"
        ).access == "allow"
        with pytest.raises(AccessDeniedError):
            second.resolve("telegram", "same-chat", "same-thread", "same-user")


def test_topic_binding_migration_adds_closed_state_to_existing_database(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                profile TEXT NOT NULL,
                project_id TEXT NOT NULL,
                slug TEXT NOT NULL,
                board_slug TEXT NOT NULL,
                workdir TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY (profile, project_id),
                UNIQUE (profile, slug)
            );
            CREATE TABLE topic_bindings (
                profile TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                is_management INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (profile, platform, chat_id, thread_id)
            );
            """
        )

    with ProjectRouter(db_path, "default") as router:
        columns = {
            row["name"]: row for row in router._connection.execute(
                "PRAGMA table_info(topic_bindings)"
            )
        }

    assert "is_closed" in columns
    assert columns["is_closed"]["notnull"] == 1
    assert str(columns["is_closed"]["dflt_value"]) == "0"


def test_last_open_topic_archives_project_and_duplicate_close_is_idempotent(
    tmp_path: Path,
) -> None:
    board_states = []
    writer = lambda slug, *, archived: board_states.append((slug, archived))
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "alpha-board", tmp_path / "alpha")
        router.bind_topic("telegram", "chat", "thread", "project-1")
        router.set_acl("chat", "user", "allow")

        first = router.transition_topic_project(
            "telegram", "chat", "thread", "user",
            closed=True,
            board_state_writer=writer,
        )
        duplicate = router.transition_topic_project(
            "telegram", "chat", "thread", "user",
            closed=True,
            board_state_writer=writer,
        )
        binding = router._connection.execute(
            "SELECT is_closed FROM topic_bindings WHERE profile='default'"
        ).fetchone()

    assert first.status == duplicate.status == "archived"
    assert binding["is_closed"] == 1
    assert board_states == [("alpha-board", True), ("alpha-board", True)]


def test_reopening_topic_reactivates_same_project_and_board(tmp_path: Path) -> None:
    board_states = []
    writer = lambda slug, *, archived: board_states.append((slug, archived))
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "alpha-board", tmp_path / "alpha")
        router.bind_topic("telegram", "chat", "thread", "project-1")
        router.set_acl("chat", "user", "allow")
        router.transition_topic_project(
            "telegram", "chat", "thread", "user",
            closed=True,
            board_state_writer=writer,
        )

        reopened = router.transition_topic_project(
            "telegram", "chat", "thread", "user",
            closed=False,
            board_state_writer=writer,
        )
        binding = router._connection.execute(
            "SELECT is_closed FROM topic_bindings WHERE profile='default'"
        ).fetchone()

    assert reopened.status == "active"
    assert reopened.project_id == "project-1"
    assert reopened.board_slug == "alpha-board"
    assert binding["is_closed"] == 0
    assert board_states == [("alpha-board", True), ("alpha-board", False)]


def test_board_archives_only_after_all_project_topics_are_closed(tmp_path: Path) -> None:
    board_states = []
    writer = lambda slug, *, archived: board_states.append((slug, archived))
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "alpha-board", tmp_path / "alpha")
        router.bind_topic("telegram", "chat", "thread-1", "project-1")
        router.bind_topic("telegram", "chat", "thread-2", "project-1")
        router.set_acl("chat", "user", "allow")

        first = router.transition_topic_project(
            "telegram", "chat", "thread-1", "user",
            closed=True,
            board_state_writer=writer,
        )
        second = router.transition_topic_project(
            "telegram", "chat", "thread-2", "user",
            closed=True,
            board_state_writer=writer,
        )

    assert first.status == "active"
    assert second.status == "archived"
    assert board_states == [("alpha-board", True)]


def test_shared_board_stays_active_until_last_project_is_archived(tmp_path: Path) -> None:
    board_states = []
    writer = lambda slug, *, archived: board_states.append((slug, archived))
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        for project_id, thread_id in (("project-1", "thread-1"), ("project-2", "thread-2")):
            router.upsert_project(
                project_id, project_id, "shared-board", tmp_path / project_id
            )
            router.bind_topic("telegram", "chat", thread_id, project_id)
        router.set_acl("chat", "user", "allow")

        first = router.transition_topic_project(
            "telegram", "chat", "thread-1", "user",
            closed=True,
            board_state_writer=writer,
        )
        second = router.transition_topic_project(
            "telegram", "chat", "thread-2", "user",
            closed=True,
            board_state_writer=writer,
        )

    assert first.status == second.status == "archived"
    assert board_states == [("shared-board", True)]


def test_management_topic_never_archives_a_board(tmp_path: Path) -> None:
    board_states = []
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project(
            "management", "management", "management", None, status="active"
        )
        router.bind_topic(
            "telegram", "chat", "management-thread", "management",
            is_management=True,
        )
        router.set_acl("chat", "admin", "allow")

        result = router.transition_topic_project(
            "telegram", "chat", "management-thread", "admin",
            closed=True,
            board_state_writer=lambda slug, *, archived: board_states.append(
                (slug, archived)
            ),
        )
        binding = router._connection.execute(
            "SELECT is_closed FROM topic_bindings WHERE profile='default'"
        ).fetchone()

    assert result.status == "active"
    assert binding["is_closed"] == 1
    assert board_states == []


def test_board_state_failure_rolls_back_topic_and_project_status(tmp_path: Path) -> None:
    def fail_board_state(slug, *, archived):
        raise OSError("board metadata unavailable")

    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.upsert_project("project-1", "alpha", "alpha-board", tmp_path / "alpha")
        router.bind_topic("telegram", "chat", "thread", "project-1")
        router.set_acl("chat", "user", "allow")

        with pytest.raises(OSError, match="metadata unavailable"):
            router.transition_topic_project(
                "telegram", "chat", "thread", "user",
                closed=True,
                board_state_writer=fail_board_state,
            )
        binding = router._connection.execute(
            "SELECT is_closed FROM topic_bindings WHERE profile='default'"
        ).fetchone()
        project = router._connection.execute(
            "SELECT status FROM projects WHERE profile='default'"
        ).fetchone()

    assert binding["is_closed"] == 0
    assert project["status"] == "active"
