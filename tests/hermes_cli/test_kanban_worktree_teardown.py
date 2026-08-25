"""Tests for worktree workspace teardown at task completion/archive.

Covers the fail-closed ownership boundary for legacy worktrees. Hermes-owned
branches are preserved without a sealed delivery receipt; non-owned clean,
pushed worktrees retain the legacy safe-removal behavior. Verified delivery
cleanup and its retry state are covered by ``test_git_delivery.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kb._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_task_owned_worktree_without_receipt_is_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    removed, reason = kb._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert removed is False
    assert reason == "owned branch cleanup requires a sealed delivery receipt"
    assert wt.is_dir()
    assert _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kb._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kb._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


def test_custom_branch_and_manual_worktree_are_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    removed, reason = kb._cleanup_worktree_workspace(
        "t_dddd4444", str(wt), "feature/custom"
    )
    assert removed is False
    assert reason == "worktree branch is not owned by this task"
    assert wt.is_dir()
    assert _branch_exists(repo, "feature/custom")


def test_main_checkout_never_removed(repo: Path) -> None:
    kb._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kb._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree dirtied after quarantine is restored, never deleted."""
    task_id = "t_gggg7777"
    branch = f"wt/{task_id}"
    wt = _make_worktree(repo, task_id, branch=branch)
    expected_common = _git(
        "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    expected_git_dir = _git(
        "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-dir"
    ).strip()
    expected_head = _git("-C", str(wt), "rev-parse", "HEAD^{commit}").strip()
    quarantine = (
        repo / ".worktrees" / ".hermes-cleanup" / f"{task_id}-{'c' * 32}"
    )
    original_cleanup_git = kb._cleanup_git
    dirtied = {"once": False}

    def _dirty_after_quarantine(repo_path, *args, **kwargs):
        result = original_cleanup_git(repo_path, *args, **kwargs)
        if (
            args[:2] == ("worktree", "move")
            and result.returncode == 0
            and not dirtied["once"]
        ):
            dirtied["once"] = True
            (quarantine / "late-wip.txt").write_text(
                "dirtied after quarantine\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(kb, "_cleanup_git", _dirty_after_quarantine)
    removed, reason = kb._cleanup_worktree_workspace(
        task_id,
        str(wt),
        branch,
        expected_head_sha=expected_head,
        repo_path=str(repo),
        expected_common_dir=expected_common,
        expected_git_dir=expected_git_dir,
        quarantine_path=str(quarantine),
    )

    assert removed is False
    assert reason == "worktree is dirty or its status could not be proven; checkout restored without deletion"
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()
    assert not quarantine.exists()
    assert _branch_exists(repo, branch)


def test_cleanup_refuses_foreign_checkout_swapped_into_owned_path(repo: Path) -> None:
    task_id = "t_a1b2c3d4"
    branch = f"wt/{task_id}"
    owned = _make_worktree(repo, task_id, branch=branch)
    expected_common = _git(
        "-C", str(owned), "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    expected_git_dir = _git(
        "-C", str(owned), "rev-parse", "--path-format=absolute", "--git-dir"
    ).strip()
    expected_head = _git("-C", str(owned), "rev-parse", "HEAD^{commit}").strip()

    preserved_owned = repo / ".worktrees" / "preserved-owned"
    _git("-C", str(repo), "worktree", "move", str(owned), str(preserved_owned))
    _git("-C", str(repo), "branch", "feature/foreign", "HEAD")
    _git("-C", str(repo), "worktree", "add", str(owned), "feature/foreign")
    foreign_git_dir = _git(
        "-C", str(owned), "rev-parse", "--path-format=absolute", "--git-dir"
    ).strip()
    assert Path(foreign_git_dir).resolve() != Path(expected_git_dir).resolve()

    removed, reason = kb._cleanup_worktree_workspace(
        task_id,
        str(owned),
        branch,
        expected_head_sha=expected_head,
        repo_path=str(repo),
        expected_common_dir=expected_common,
        expected_git_dir=expected_git_dir,
        quarantine_path=str(
            repo / ".worktrees" / ".hermes-cleanup" / f"{task_id}-{'a' * 32}"
        ),
    )

    assert removed is False
    assert reason == "cleanup candidate differs from sealed worktree identity"
    assert owned.is_dir()
    assert preserved_owned.is_dir()
    assert _branch_exists(repo, branch)


def test_cleanup_retry_recovers_quarantined_worktree_after_crash(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "t_b2c3d4e5"
    branch = f"wt/{task_id}"
    owned = _make_worktree(repo, task_id, branch=branch)
    expected_common = _git(
        "-C", str(owned), "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    expected_git_dir = _git(
        "-C", str(owned), "rev-parse", "--path-format=absolute", "--git-dir"
    ).strip()
    expected_head = _git("-C", str(owned), "rev-parse", "HEAD^{commit}").strip()
    quarantine = (
        repo
        / ".worktrees"
        / ".hermes-cleanup"
        / f"{task_id}-{'b' * 32}"
    )
    original_cleanup_git = kb._cleanup_git
    crashed = {"once": False}

    def _crash_after_quarantine(repo_path, *args, **kwargs):
        if (
            args[:2] == ("worktree", "remove")
            and not crashed["once"]
        ):
            crashed["once"] = True
            raise RuntimeError("simulated crash after quarantine move")
        return original_cleanup_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(kb, "_cleanup_git", _crash_after_quarantine)
    removed, reason = kb._cleanup_worktree_workspace(
        task_id,
        str(owned),
        branch,
        expected_head_sha=expected_head,
        repo_path=str(repo),
        expected_common_dir=expected_common,
        expected_git_dir=expected_git_dir,
        quarantine_path=str(quarantine),
    )
    assert removed is False
    assert "simulated crash" in reason
    assert not owned.exists()
    assert quarantine.is_dir()
    assert _branch_exists(repo, branch)

    monkeypatch.setattr(kb, "_cleanup_git", original_cleanup_git)
    removed, reason = kb._cleanup_worktree_workspace(
        task_id,
        str(owned),
        branch,
        expected_head_sha=expected_head,
        repo_path=str(repo),
        expected_common_dir=expected_common,
        expected_git_dir=expected_git_dir,
        quarantine_path=str(quarantine),
    )
    assert removed is True
    assert reason is None
    assert not quarantine.exists()
    assert not _branch_exists(repo, branch)


def test_resolve_never_adopts_manual_task_shaped_worktree(
    kanban_home: Path,
    repo: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="Manual checkout must stay foreign",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        branch = f"wt/{task_id}"
        target = _make_worktree(repo, task_id, branch=branch)
        task = kb.get_task(conn, task_id)

        with pytest.raises(RuntimeError, match="no Hermes creation receipt"):
            kb.resolve_workspace(task, conn=conn)

        assert target.is_dir()
        assert _branch_exists(repo, branch)


def test_worktree_git_runner_is_hidden_and_noninteractive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def _fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(kb.subprocess, "run", _fake_run)
    result = kb._cleanup_git(tmp_path, "status", "--porcelain=v2")

    assert result.returncode == 0
    argv, kwargs = calls[0]
    assert argv[:3] == ["git", "-C", str(tmp_path)]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    if kb._IS_WINDOWS:
        assert kwargs.get("creationflags", 0) != 0


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_without_delivery_obligation_is_blocked(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done") is False
    assert wt.is_dir()
    assert _branch_exists(repo, f"wt/{tid}")


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done") is False
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_preserves_unverified_worktree(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert wt.is_dir()


def test_archived_parent_worktree_remains_preserved_after_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        assert kb.archive_task(conn, parent)
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # A terminal child cannot turn an unverified parent into cleanup authority.
    assert parent_wt.is_dir()
