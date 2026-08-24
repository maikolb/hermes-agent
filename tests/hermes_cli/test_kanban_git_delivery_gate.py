"""Focused SQLite contracts for the worktree Git-delivery completion gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _complete_policy() -> dict:
    return {
        "required": True,
        "head_remote": "origin",
        "expected_head_remote_url": "https://github.com/acme/widget.git",
        "base_remote": "upstream",
        "expected_base_remote_url": "https://github.com/acme/widget.git",
        "head_repository": "acme/widget",
        "base_repository": "acme/widget",
        "base_branch": "main",
        "required_checks": ["tests"],
    }


def _seal_policy(conn, task_id: str, policy: dict | None = None) -> dict:
    sealed = dict(policy or _complete_policy())
    policy_json, policy_fingerprint = kb._canonical_delivery_document(sealed)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_git_delivery SET policy_json = ?, "
            "policy_fingerprint = ? WHERE task_id = ?",
            (policy_json, policy_fingerprint, task_id),
        )
    return sealed


def test_manual_non_code_completion_remains_unchanged(tmp_path: Path) -> None:
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(conn, title="Manual research")

        assert kb.complete_task(conn, task_id, summary="done") is True
        assert kb.get_task(conn, task_id).status == "done"
        obligation = conn.execute(
            "SELECT 1 FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert obligation is None


def test_nonrequired_completion_paths_never_query_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import git_delivery

    def _unexpected_remote(*_args, **_kwargs):
        pytest.fail("non-required completion must not invoke Git delivery")

    monkeypatch.setattr(
        git_delivery,
        "verify_and_persist_git_delivery",
        _unexpected_remote,
    )
    with kb.connect_closing(tmp_path / "no-remote.db") as conn:
        manual = kb.create_task(conn, title="Manual completion")
        assert kb.complete_task(conn, manual, summary="done") is True

        legacy = kb.create_task(
            conn,
            title="Tampered legacy worktree",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / ".worktrees" / "legacy"),
            branch_name="wt/legacy",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET required = 0 WHERE task_id = ?",
                (legacy,),
            )
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?",
                (legacy,),
            )
        assert kb.complete_task(conn, legacy, summary="must block") is False
        assert kb.get_task(conn, legacy).status == "review"


def test_policy_absent_worktree_fails_closed_and_stays_retryable(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Code task",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="feature/code-task",
        )

        assert kb.complete_task(conn, task_id, summary="unproven delivery") is False
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        obligation = conn.execute(
            "SELECT required, candidate_digest, receipt_json, verified_at "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert obligation is not None
        assert tuple(obligation) == (1, None, None, None)
        blocked = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "completion_blocked_delivery"
        ]
        assert len(blocked) == 1
        assert workspace.is_dir()


def test_required_worktree_stays_retryable_without_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "required-worktree"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Required code task",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="feature/required-code-task",
        )
        policy_json, policy_fingerprint = kb._canonical_delivery_document(
            {"required": True}
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET required = 1, policy_json = ?, "
                "policy_fingerprint = ? WHERE task_id = ?",
                (policy_json, policy_fingerprint, task_id),
            )

        assert kb.complete_task(conn, task_id, summary="not delivered") is False
        assert kb.get_task(conn, task_id).status == "ready"
        blocked = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "completion_blocked_delivery"
        ]
        assert len(blocked) == 1


@pytest.mark.parametrize("tamper", ["delete", "disable"])
def test_worktree_completion_fails_if_obligation_row_is_missing_or_disabled(
    tmp_path: Path, tamper: str
) -> None:
    workspace = tmp_path / tamper
    workspace.mkdir()
    with kb.connect_closing(tmp_path / f"{tamper}.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Tamper obligation",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name=f"delivery/{tamper}",
        )
        with kb.write_txn(conn):
            if tamper == "delete":
                conn.execute(
                    "DELETE FROM task_git_delivery WHERE task_id = ?",
                    (task_id,),
                )
            else:
                conn.execute(
                    "UPDATE task_git_delivery SET required = 0 WHERE task_id = ?",
                    (task_id,),
                )

        assert kb.complete_task(conn, task_id, summary="must not pass") is False
        assert kb.get_task(conn, task_id).status == "ready"
        blocked = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "completion_blocked_delivery"
        ]
        assert blocked[-1].payload["code"] == "delivery_obligation_missing"


def test_incomplete_required_policy_fails_review_with_actionable_reason(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "incomplete-policy"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Incomplete policy",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/incomplete",
        )
        policy_json, policy_fingerprint = kb._canonical_delivery_document(
            {"required": True}
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET required = 1, policy_json = ?, "
                "policy_fingerprint = ? WHERE task_id = ?",
                (policy_json, policy_fingerprint, task_id),
            )

        ok, reason = kb.request_review(
            conn,
            task_id,
            git_delivery_request={
                "pull_request": 17,
                "declared_artifacts": ["app.txt"],
            },
            with_reason=True,
        )

        assert ok is False
        assert "policy is incomplete" in reason
        assert kb.get_task(conn, task_id).status == "ready"


def test_worker_cannot_supply_missing_policy_at_review(tmp_path: Path) -> None:
    workspace = tmp_path / "policy-fill"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Fill policy",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/fill-policy",
        )

        ok, reason = kb.request_review(
            conn,
            task_id,
            git_delivery_request={
                "pull_request": 17,
                "declared_artifacts": ["app.txt"],
            },
            with_reason=True,
        )

        assert ok is False
        assert "policy is missing" in reason
        contract = kb.get_git_delivery_contract(conn, task_id)
        assert contract is not None
        assert contract["policy"] is None
        assert contract["policy_fingerprint"] is None
        assert kb.get_task(conn, task_id).status == "ready"


def test_review_uses_only_policy_already_sealed_on_task(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "immutable-policy"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "immutable-policy.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Immutable policy",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/immutable-policy",
        )
        sealed = _seal_policy(conn, task_id)

        ok, reason = kb.request_review(
            conn,
            task_id,
            git_delivery_request={
                "pull_request": 17,
                "declared_artifacts": ["app.txt"],
            },
            with_reason=True,
        )

        assert ok is True, reason
        contract = kb.get_git_delivery_contract(conn, task_id)
        assert contract is not None
        assert contract["policy"] == sealed
        assert contract["policy"]["required_checks"] == ["tests"]


def test_changes_requested_invalidates_receipt_and_requires_fresh_review(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "changes"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "changes.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Rework candidate",
            assignee="implementer",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/rework",
        )
        _seal_policy(conn, task_id)
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            git_delivery_request={
                "pull_request": 17,
                "declared_artifacts": ["app.txt"],
            },
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET candidate_digest = ?, "
                "receipt_json = ?, receipt_fingerprint = ?, verified_at = ? "
                "WHERE task_id = ?",
                ("a" * 64, "{}", "c" * 64, 1, task_id),
            )
        claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
        assert claimed is not None
        ok, _implementer = kb.request_changes(
            conn,
            task_id,
            reason="change it",
            expected_run_id=claimed.current_run_id,
        )
        assert ok is True
        contract = kb.get_git_delivery_contract(conn, task_id)
        assert contract["request"] is None
        assert contract["candidate_digest"] is None
        assert contract["receipt_json"] is None
        assert kb.complete_task(conn, task_id, summary="reuse old proof") is False


def test_reopen_review_invalidates_prior_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "reopen"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "reopen.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Reopen candidate",
            assignee="implementer",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/reopen",
        )
        _seal_policy(conn, task_id)
        assert kb.request_review(
            conn,
            task_id,
            git_delivery_request={
                "pull_request": 17,
                "declared_artifacts": ["app.txt"],
            },
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET candidate_digest = ?, "
                "receipt_json = ?, receipt_fingerprint = ?, verified_at = ? "
                "WHERE task_id = ?",
                ("b" * 64, "{}", "d" * 64, 1, task_id),
            )

        assert kb.reopen_review_task(conn, task_id) is True
        contract = kb.get_git_delivery_contract(conn, task_id)
        assert contract["request"] is None
        assert contract["candidate_digest"] is None
        assert contract["receipt_json"] is None


def test_decompose_creates_fail_closed_obligation_for_worktree_child(
    tmp_path: Path,
) -> None:
    with kb.connect_closing(tmp_path / "decompose.db") as conn:
        root = kb.create_task(conn, title="Root", triage=True)
        children = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "Code child", "workspace_kind": "worktree"}],
            auto_promote=False,
        )
        assert children is not None
        row = conn.execute(
            "SELECT required FROM task_git_delivery WHERE task_id = ?",
            (children[0],),
        ).fetchone()
        assert row is not None and row["required"] == 1


def test_archive_preserves_unverified_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "archive-preserved"
    workspace.mkdir()
    with kb.connect_closing(tmp_path / "archive.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Archive unverified",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="delivery/archive",
        )
        assert kb.archive_task(conn, task_id) is True
        assert workspace.is_dir()
        contract = conn.execute(
            "SELECT cleanup_state FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert contract["cleanup_state"] == "not_requested"


def test_manual_task_shaped_checkout_cannot_receive_delivery_receipt(
    tmp_path: Path,
) -> None:
    with kb.connect_closing(tmp_path / "manual-shaped.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Manual task-shaped checkout",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "placeholder"),
        )
        checkout = tmp_path / ".worktrees" / task_id
        checkout.mkdir(parents=True)
        branch = f"wt/{task_id}"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, branch_name = ? WHERE id = ?",
                (str(checkout), branch, task_id),
            )

        accepted = kb._persist_verified_git_delivery_receipt(
            conn,
            task_id,
            {
                "schema_version": 1,
                "candidate_digest": "a" * 64,
                "canonical_worktree": str(checkout),
                "branch": branch,
                "head_sha": "b" * 40,
                "merge_sha": "c" * 40,
                "checks": [{"name": "tests", "state": "SUCCESS"}],
            },
        )

        assert accepted is False
        contract = kb.get_git_delivery_contract(conn, task_id)
        assert contract is not None
        assert contract["candidate_digest"] is None
        assert contract["receipt_json"] is None
        assert checkout.is_dir()


def _owned_worktree_task(conn, tmp_path: Path, title: str) -> str:
    task_id = kb.create_task(
        conn,
        title=title,
        workspace_kind="worktree",
        workspace_path=str(tmp_path / "placeholder"),
    )
    owned_path = (tmp_path / ".worktrees" / task_id).resolve()
    repo_root = tmp_path.resolve()
    ownership = {
        "schema_version": 1,
        "task_id": task_id,
        "repo_root": str(repo_root),
        "git_common_dir": str(repo_root / ".git"),
        "git_dir": str(repo_root / ".git" / "worktrees" / task_id),
        "canonical_worktree": str(owned_path),
        "branch": f"project/{task_id}-delivery",
        "creation_nonce": "a" * 32,
        "created_at": 1,
    }
    ownership_json = json.dumps(
        ownership, sort_keys=True, separators=(",", ":")
    )
    ownership_fingerprint = hashlib.sha256(
        ownership_json.encode("utf-8")
    ).hexdigest()
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ?, branch_name = ? WHERE id = ?",
            (str(owned_path), f"project/{task_id}-delivery", task_id),
        )
        conn.execute(
            "UPDATE task_git_delivery SET ownership_json = ?, "
            "ownership_fingerprint = ? WHERE task_id = ?",
            (ownership_json, ownership_fingerprint, task_id),
        )
    return task_id


def test_public_delete_refuses_active_worktree_even_if_cleanup_is_tampered_complete(
    tmp_path: Path,
) -> None:
    with kb.connect_closing(tmp_path / "active-delete.db") as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Active owned worktree")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET cleanup_state = 'complete' "
                "WHERE task_id = ?",
                (task_id,),
            )

        assert kb.delete_task(conn, task_id) is False
        assert kb.get_task(conn, task_id) is not None
        assert kb.get_git_delivery_contract(conn, task_id) is not None


@pytest.mark.parametrize("delete_name", ["delete_task", "delete_archived_task"])
@pytest.mark.parametrize("cleanup_state", ["not_requested", "pending"])
def test_owned_archived_worktree_cannot_lose_cleanup_obligation(
    tmp_path: Path,
    delete_name: str,
    cleanup_state: str,
) -> None:
    with kb.connect_closing(
        tmp_path / f"owned-{delete_name}-{cleanup_state}.db"
    ) as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Archived owned worktree")
        assert kb.archive_task(conn, task_id)
        if cleanup_state != "not_requested":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_git_delivery SET cleanup_state = ? WHERE task_id = ?",
                    (cleanup_state, task_id),
                )

        assert getattr(kb, delete_name)(conn, task_id) is False
        assert kb.get_task(conn, task_id) is not None
        assert kb.get_git_delivery_contract(conn, task_id) is not None


@pytest.mark.parametrize("delete_name", ["delete_task", "delete_archived_task"])
def test_owned_archived_worktree_is_deletable_only_after_cleanup_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_name: str,
) -> None:
    with kb.connect_closing(tmp_path / f"complete-{delete_name}.db") as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Reaped owned worktree")
        assert kb.archive_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?",
                (task_id,),
            )
        monkeypatch.setattr(
            kb,
            "_validate_cleanup_obligation",
            lambda *_args, **_kwargs: (True, {"task_id": task_id}, ""),
        )

        assert getattr(kb, delete_name)(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None
        assert kb.get_git_delivery_contract(conn, task_id) is None


@pytest.mark.parametrize("delete_name", ["delete_task", "delete_archived_task"])
def test_tampered_ownership_never_falls_back_to_foreign_delete(
    tmp_path: Path,
    delete_name: str,
) -> None:
    with kb.connect_closing(tmp_path / f"tampered-{delete_name}.db") as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Tampered ownership")
        assert kb.archive_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET ownership_fingerprint = ? "
                "WHERE task_id = ?",
                ("f" * 64, task_id),
            )

        assert getattr(kb, delete_name)(conn, task_id) is False
        assert kb.get_task(conn, task_id) is not None
        assert kb.get_git_delivery_contract(conn, task_id) is not None


def test_dashboard_reopen_waits_for_pending_owned_cleanup(
    tmp_path: Path,
) -> None:
    from plugins.kanban.dashboard import plugin_api

    with kb.connect_closing(tmp_path / "dashboard-reopen-pending.db") as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Pending reopen")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_git_delivery SET cleanup_state = 'pending' "
                "WHERE task_id = ?",
                (task_id,),
            )

        assert plugin_api._set_status_direct(conn, task_id, "ready") is False
        assert kb.get_task(conn, task_id).status == "done"
        row = conn.execute(
            "SELECT ownership_json, cleanup_state FROM task_git_delivery "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row["ownership_json"] is not None
        assert row["cleanup_state"] == "pending"


def test_dashboard_reopen_resets_reaped_delivery_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.kanban.dashboard import plugin_api

    with kb.connect_closing(tmp_path / "dashboard-reopen-reaped.db") as conn:
        task_id = _owned_worktree_task(conn, tmp_path, "Reaped reopen")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_git_delivery SET cleanup_state = 'complete' "
                "WHERE task_id = ?",
                (task_id,),
            )
        monkeypatch.setattr(
            kb,
            "_validate_cleanup_obligation",
            lambda *_args, **_kwargs: (True, {"task_id": task_id}, ""),
        )

        assert plugin_api._set_status_direct(conn, task_id, "ready") is True
        assert kb.get_task(conn, task_id).status == "ready"
        row = conn.execute(
            "SELECT ownership_json, candidate_digest, cleanup_state, cleanup_json "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == (None, None, "not_requested", None)


def test_descendant_reopen_rolls_back_when_owned_cleanup_is_pending(
    tmp_path: Path,
) -> None:
    with kb.connect_closing(tmp_path / "descendant-reopen.db") as conn:
        parent = kb.create_task(conn, title="Reopened parent")
        child = _owned_worktree_task(conn, tmp_path, "Pending child")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child,))
            conn.execute(
                "UPDATE task_git_delivery SET cleanup_state = 'pending' "
                "WHERE task_id = ?",
                (child,),
            )

        with pytest.raises(RuntimeError, match="cleanup is still pending"):
            kb.invalidate_descendants_for_parent_reopen(
                conn,
                parent,
                author="test",
            )

        assert kb.get_task(conn, child).status == "done"
        row = conn.execute(
            "SELECT ownership_json, cleanup_state FROM task_git_delivery "
            "WHERE task_id = ?",
            (child,),
        ).fetchone()
        assert row["ownership_json"] is not None
        assert row["cleanup_state"] == "pending"


@pytest.mark.parametrize("delete_name", ["delete_task", "delete_archived_task"])
def test_explicit_delete_forgets_archived_foreign_metadata_but_never_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_name: str,
) -> None:
    checkout = tmp_path / f"manual-{delete_name}"
    checkout.mkdir()
    with kb.connect_closing(tmp_path / f"foreign-{delete_name}.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Foreign manual checkout",
            workspace_kind="worktree",
            workspace_path=str(checkout),
            branch_name="feature/manual-checkout",
        )
        assert kb.archive_task(conn, task_id)
        monkeypatch.setattr(
            kb,
            "_cleanup_worktree_workspace",
            lambda *_args, **_kwargs: pytest.fail(
                "foreign checkout must never enter worktree cleanup"
            ),
        )
        monkeypatch.setattr(
            kb,
            "_cleanup_git",
            lambda *_args, **_kwargs: pytest.fail(
                "foreign checkout branch must never enter Git cleanup"
            ),
        )

        assert getattr(kb, delete_name)(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None
        assert checkout.is_dir()


def test_legacy_worktree_requirement_backfill_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="Legacy task")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind = 'worktree' WHERE id = ?",
                (task_id,),
            )

    kb.init_db(db_path)
    kb.init_db(db_path)
    raw = sqlite3.connect(db_path)
    try:
        row = raw.execute(
            "SELECT COUNT(*), MAX(required) FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        raw.close()
    assert row == (1, 1)
