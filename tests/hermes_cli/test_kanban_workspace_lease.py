"""Cross-board/workspace exclusion at the real worker spawn boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    return home


def test_same_workspace_spawns_only_one_live_worker(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "shared-checkout"
    with kb.connect() as conn:
        task_ids = [
            kb.create_task(
                conn,
                title=f"shared {index}",
                assignee="worker",
                workspace_kind="dir",
                workspace_path=str(workspace),
            )
            for index in range(2)
        ]
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: os.getpid())

    assert len(result.spawned) == 1
    assert result.spawned[0][0] in task_ids
    assert len(result.skipped_workspace_leased) == 1
    skipped_id, owner_id, leased_path = result.skipped_workspace_leased[0]
    assert skipped_id in task_ids
    assert skipped_id != result.spawned[0][0]
    assert owner_id == result.spawned[0][0]
    assert Path(leased_path) == workspace.resolve()


def test_distinct_workspaces_can_spawn_in_same_tick(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    with kb.connect() as conn:
        for index in range(2):
            kb.create_task(
                conn,
                title=f"isolated {index}",
                assignee="worker",
                workspace_kind="dir",
                workspace_path=str(tmp_path / f"checkout-{index}"),
            )
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: os.getpid())

    assert len(result.spawned) == 2
    assert result.skipped_workspace_leased == []


def test_live_preupgrade_worker_is_adopted_before_new_spawn(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "existing-checkout"
    workspace.mkdir()
    with kb.connect() as conn:
        running_id = kb.create_task(
            conn,
            title="already running",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        claimed = kb.claim_task(conn, running_id)
        assert claimed is not None
        kb._set_worker_pid(conn, running_id, os.getpid())
        waiting_id = kb.create_task(
            conn,
            title="must wait",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 999999)

    assert result.spawned == []
    assert result.skipped_workspace_leased == [
        (waiting_id, running_id, str(workspace.resolve()))
    ]
    assert result.workspace_lease_conflicts == []


def test_live_preupgrade_worker_in_other_board_blocks_new_spawn(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """Activation must adopt old workers globally, not per current board."""
    workspace = tmp_path / "cross-board-checkout"
    workspace.mkdir()
    kb.create_board("old-worker")
    kb.create_board("new-work")

    with kb.connect(board="old-worker") as old_conn:
        running_id = kb.create_task(
            old_conn,
            title="pre-upgrade worker in another board",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        assert kb.claim_task(old_conn, running_id) is not None
        kb._set_worker_pid(old_conn, running_id, os.getpid())

    with kb.connect(board="new-work") as new_conn:
        waiting_id = kb.create_task(
            new_conn,
            title="must not overlap old board",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        result = kb.dispatch_once(
            new_conn,
            board="new-work",
            spawn_fn=lambda *_args, **_kwargs: 999999,
        )

    assert result.spawned == []
    assert result.skipped_workspace_leased == [
        (waiting_id, running_id, str(workspace.resolve()))
    ]
