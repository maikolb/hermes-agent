from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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


def test_unknown_topic_fails_closed(tmp_path: Path) -> None:
    with configured_router(tmp_path / "router.db", tmp_path / "workspace") as router:
        router.set_acl(100, 7, "allow")
        with pytest.raises(UnknownBindingError):
            router.resolve("telegram", 100, 999, 7)


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
