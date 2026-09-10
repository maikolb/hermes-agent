"""SEAL_BRANCH_20260910: a worktree whose current branch is the card's own (name contains the task id) stays owned."""
import hashlib
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

TID = "t_seal01"


def _seal(conn, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    common = tmp_path / "repo" / ".git"
    gitdir = common / "worktrees" / "wt"
    payload = {
        "schema_version": 1, "task_id": TID, "canonical_worktree": str(wt), "repo_root": str(tmp_path / "repo"),
        "git_common_dir": str(common), "git_dir": str(gitdir), "branch": "concursa-ai/t_seal01-maikol",
        "creation_nonce": "0" * 32,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, task_role, workspace_kind, workspace_path, branch_name) "
        "VALUES (?, 'a', 'ready', ?, 'work', 'worktree', ?, 'concursa-ai/t_seal01-maikol')", (TID, now, str(wt)),
    )
    conn.execute(
        "INSERT INTO task_git_delivery (task_id, required_at, ownership_json, ownership_fingerprint) VALUES (?, ?, ?, ?)", (TID, now, raw, fp),
    )
    conn.commit()
    return common, gitdir


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    conn = kb.connect(tmp_path / "k.db")
    common, gitdir = _seal(conn, tmp_path)
    monkeypatch.setattr(kb, "_task_owns_worktree_identity", lambda *a, **k: True)
    monkeypatch.setattr(kb, "_is_linked_worktree_checkout", lambda p: True)
    monkeypatch.setattr(kb, "_git_common_dir", lambda p: Path(common))
    monkeypatch.setattr(kb, "_git_dir", lambda p: Path(gitdir))
    return conn, monkeypatch


def _valid(conn, monkeypatch, branch):
    monkeypatch.setattr(kb, "_git_current_branch", lambda p: branch)
    ok, payload, reason = kb._validate_worktree_ownership(conn, TID, require_checkout=True)
    return ok, reason


def test_creation_branch_is_valid(sealed):
    conn, mp = sealed
    ok, reason = _valid(conn, mp, "concursa-ai/t_seal01-maikol")
    assert ok, reason


def test_card_named_branch_is_valid(sealed):
    conn, mp = sealed
    ok, reason = _valid(conn, mp, "nfos/t_seal01-production")
    assert ok, reason


def test_foreign_branch_is_rejected(sealed):
    conn, mp = sealed
    ok, reason = _valid(conn, mp, "main")
    assert not ok and "creation receipt" in reason
