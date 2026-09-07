"""Real Git + SQLite recovery at the worktree-create / receipt-commit boundary."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery


def run(*args, env=None):
    return subprocess.run(
        args, env=env, text=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=45,
    )


def git(repo, *args):
    result = run("git", "-C", str(repo), "-c", "user.name=NFOS Test",
                 "-c", "user.email=nfos-test@example.invalid",
                 "-c", "commit.gpgsign=false", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def owned_request(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    db = home / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("original\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    with kb.connect_closing(db) as conn:
        rid = delivery.receive_request(conn,
            source={"platform": "telegram", "chat_id": "-10", "thread_id": "4", "message_id": "1"},
            text="Make the integration readable",
            project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(repo)},
            attachments=[])
        request = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    return db, repo, task.id


def crash_creator(db, tid, tmp_path, *, after_seal=False):
    script = tmp_path / "crash_creator.py"
    script.write_text('''import os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
original = kb._seal_materialized_worktree_ownership
def crash(*args, **kwargs):
    worktree = kwargs["worktree"]
    (worktree / "README.md").write_text("uncommitted progress\\n", encoding="utf-8")
    (worktree / "checkpoint.json").write_text('{"next_action":"test"}', encoding="utf-8")
    if sys.argv[3] == "after":
        original(*args, **kwargs)
    os._exit(73)
kb._seal_materialized_worktree_ownership = crash
with kb.connect_closing(Path(sys.argv[1])) as conn:
    kb.resolve_workspace(kb.get_task(conn, sys.argv[2]), conn=conn)
''', encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(kb.__file__).resolve().parents[1]))
    result = run(sys.executable, str(script), str(db), tid,
                 "after" if after_seal else "before", env=env)
    assert result.returncode == 73, result.stdout + result.stderr


def test_process_death_after_git_create_recovers_same_workspace_and_progress(owned_request, tmp_path):
    db, repo, tid = owned_request
    base = git(repo, "rev-parse", "HEAD")
    crash_creator(db, tid, tmp_path)
    target = repo / ".worktrees" / tid
    before_gitdir = kb._git_dir(target)
    with kb.connect_closing(db) as conn:
        workspace = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
        valid, receipt, reason = kb._validate_worktree_ownership(conn, tid, require_checkout=True)
        assert valid, reason
        events = conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,)).fetchall()
        assert sum(row["kind"] == "worktree_creation_recovered" for row in events) == 1
    assert workspace == target.resolve()
    assert kb._git_dir(target) == before_gitdir
    assert git(target, "rev-parse", "HEAD") == base
    assert (target / "README.md").read_text(encoding="utf-8") == "uncommitted progress\n"
    assert json.loads((target / "checkpoint.json").read_text()) == {"next_action": "test"}
    assert not (before_gitdir / "locked").exists()
    with kb.connect_closing(db) as conn:
        assert kb.resolve_workspace(kb.get_task(conn, tid), conn=conn) == workspace
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='worktree_owned'", (tid,)).fetchone()[0] == 1


def test_existing_foreign_worktree_is_preserved_without_intent(owned_request):
    db, repo, tid = owned_request
    target = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-b", f"wt/{tid}", str(target), "HEAD")
    (target / "foreign.txt").write_text("belongs to another creator", encoding="utf-8")
    with kb.connect_closing(db) as conn:
        with pytest.raises(RuntimeError, match="foreign"):
            kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
        assert conn.execute("SELECT ownership_json FROM task_git_delivery WHERE task_id=?", (tid,)).fetchone()[0] is None
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "belongs to another creator"


def test_process_death_after_receipt_commit_releases_only_creation_lock(owned_request, tmp_path):
    db, repo, tid = owned_request
    crash_creator(db, tid, tmp_path, after_seal=True)
    target = repo / ".worktrees" / tid
    with kb.connect_closing(db) as conn:
        assert kb.resolve_workspace(kb.get_task(conn, tid), conn=conn) == target.resolve()
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='worktree_owned'", (tid,)).fetchone()[0] == 1
    assert not (kb._git_dir(target) / "locked").exists()
    assert (target / "README.md").read_text(encoding="utf-8") == "uncommitted progress\n"


def interrupt_before_git_add(db, tid, monkeypatch):
    original = kb._cleanup_git
    def interrupt(repo, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            # A different connection sees the intent and event before Git can
            # create any files, so this is a committed boundary, not a savepoint.
            with kb.connect_closing(db) as reader:
                assert kb._read_worktree_creation_intent(reader, tid)
                assert reader.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='worktree_creation_requested'", (tid,)).fetchone()[0] == 1
            raise RuntimeError("interruption before Git add")
        return original(repo, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(kb, "_cleanup_git", interrupt)
        with kb.connect_closing(db) as conn:
            with pytest.raises(RuntimeError, match="interruption before Git add"):
                kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)


def test_prior_intent_does_not_adopt_a_later_foreign_creator(owned_request, monkeypatch):
    db, repo, tid = owned_request
    interrupt_before_git_add(db, tid, monkeypatch)
    target = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-b", f"wt/{tid}", str(target), "HEAD")
    (target / "foreign.txt").write_text("foreign progress", encoding="utf-8")
    with kb.connect_closing(db) as conn:
        with pytest.raises(RuntimeError, match="foreign"):
            kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
        assert conn.execute("SELECT ownership_json FROM task_git_delivery WHERE task_id=?", (tid,)).fetchone()[0] is None
    assert (target / "foreign.txt").read_text() == "foreign progress"


def test_interruption_before_git_add_uses_persisted_sha_not_later_head(owned_request, monkeypatch):
    db, repo, tid = owned_request
    base = git(repo, "rev-parse", "HEAD")
    interrupt_before_git_add(db, tid, monkeypatch)
    with kb.connect_closing(db) as conn:
        intent = kb._read_worktree_creation_intent(conn, tid)
        assert intent["owner_pid"] is None
        assert intent["owner_token"] is None
    (repo / "later.txt").write_text("concurrent unrelated change", encoding="utf-8")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-m", "unrelated new HEAD")
    assert git(repo, "rev-parse", "HEAD") != base
    with kb.connect_closing(db) as conn:
        target = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
    assert git(target, "rev-parse", "HEAD") == base
    assert not (target / "later.txt").exists()


@pytest.mark.parametrize("changed", ["nonce", "head", "branch", "active_git_index"])
def test_recovery_refuses_changed_or_unfinished_creation_without_modifying_files(owned_request, tmp_path, changed):
    db, repo, tid = owned_request
    crash_creator(db, tid, tmp_path)
    target = repo / ".worktrees" / tid
    git_dir = kb._git_dir(target)
    if changed == "nonce":
        (git_dir / "locked").write_text("another owner", encoding="utf-8")
    elif changed == "head":
        git(target, "add", "README.md")
        git(target, "commit", "-m", "unverified later commit")
    elif changed == "branch":
        git(target, "switch", "-c", "different-owner")
    else:
        (git_dir / "index.lock").write_text("Git is still writing", encoding="utf-8")
    before = {p: p.read_bytes() for p in (target / "README.md", target / "checkpoint.json", git_dir / "locked")}
    with kb.connect_closing(db) as conn:
        with pytest.raises(RuntimeError, match="foreign|still writing"):
            kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
        assert conn.execute("SELECT ownership_json FROM task_git_delivery WHERE task_id=?", (tid,)).fetchone()[0] is None
    for path, content in before.items():
        assert path.read_bytes() == content


def test_existing_receipt_never_releases_a_later_user_lock(owned_request):
    db, repo, tid = owned_request
    with kb.connect_closing(db) as conn:
        target = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
    git(repo, "worktree", "lock", "--reason", "user maintenance", str(target))
    with kb.connect_closing(db) as conn:
        assert kb.resolve_workspace(kb.get_task(conn, tid), conn=conn) == target
    assert (kb._git_dir(target) / "locked").read_text().strip() == "user maintenance"


def test_live_creator_cannot_be_replaced_by_a_second_attempt(owned_request, monkeypatch):
    db, repo, tid = owned_request
    original = kb._cleanup_git
    attempted = []
    def concurrent_attempt(repo, *args, **kwargs):
        if args[:2] == ("worktree", "add") and not attempted:
            attempted.append(True)
            with kb.connect_closing(db) as other:
                with pytest.raises(RuntimeError, match="creator is still alive"):
                    kb.resolve_workspace(kb.get_task(other, tid), conn=other)
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(kb, "_cleanup_git", concurrent_attempt)
    with kb.connect_closing(db) as conn:
        target = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
    assert attempted == [True]
    assert git(target, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("changed", ["repository", "path"])
def test_intent_cannot_be_reused_for_a_different_repository_or_path(owned_request, tmp_path, monkeypatch, changed):
    db, repo, tid = owned_request
    interrupt_before_git_add(db, tid, monkeypatch)
    target = (repo / ".worktrees" / tid).resolve()
    actual_repo = repo
    if changed == "repository":
        actual_repo = tmp_path / "other-repository"
        actual_repo.mkdir()
        git(actual_repo, "init", "-b", "main")
        git(actual_repo, "commit", "--allow-empty", "-m", "other initial")
    else:
        target = (tmp_path / ".worktrees" / tid).resolve()
    with kb.connect_closing(db) as conn:
        with pytest.raises(RuntimeError, match="changed repository, path or branch"):
            kb._materialize_task_owned_worktree(
                conn, kb.get_task(conn, tid), repo_root=actual_repo, target=target, branch=f"wt/{tid}")
    assert not target.exists()


def test_existing_task_branch_is_pinned_instead_of_checkout_head(owned_request):
    db, repo, tid = owned_request
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", f"wt/{tid}", candidate)
    (repo / "unrelated.txt").write_text("not in the candidate", encoding="utf-8")
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-m", "advance unrelated checkout")
    with kb.connect_closing(db) as conn:
        target = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
        assert kb._read_worktree_creation_intent(conn, tid)["base_sha"] == candidate
    assert git(target, "rev-parse", "HEAD") == candidate
    assert not (target / "unrelated.txt").exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_legacy_worktree_keeps_its_existing_ownership_contract(owned_request, tmp_path, interrupted):
    db, repo, _ = owned_request
    with kb.connect_closing(db) as conn:
        tid = kb.create_task(conn, title="Legacy task", assignee="default",
                             workspace_kind="worktree", workspace_path=str(repo))
    if interrupted:
        crash_creator(db, tid, tmp_path)
        with kb.connect_closing(db) as conn:
            with pytest.raises(RuntimeError, match="foreign"):
                kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
            assert kb._read_worktree_creation_intent(conn, tid) is None
        assert (repo / ".worktrees" / tid / "README.md").read_text() == "uncommitted progress\n"
    else:
        with kb.connect_closing(db) as conn:
            target = kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
            valid, _, reason = kb._validate_worktree_ownership(conn, tid, require_checkout=True)
            assert valid, reason
            assert kb._read_worktree_creation_intent(conn, tid) is None
        assert target == (repo / ".worktrees" / tid).resolve()


def test_git_identity_probes_do_not_hold_the_board_writer(owned_request, monkeypatch):
    db, repo, tid = owned_request
    original = kb._cleanup_git
    probes = []
    def probe(repo, *args, **kwargs):
        if args[:2] == ("show-ref", "--verify"):
            with sqlite3.connect(str(db), timeout=0, isolation_level=None) as other:
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    probes.append(False)
                else:
                    probes.append(True)
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(kb, "_cleanup_git", probe)
    with kb.connect_closing(db) as conn:
        kb.resolve_workspace(kb.get_task(conn, tid), conn=conn)
    assert probes and all(probes), "a Git identity probe held the shared board writer"
