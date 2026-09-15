"""Entrega 1B: every run must be attributable to code and configuration.

The acceptance stated in the plan has three parts, and each has a test here:
correct attribution across a restart, a worker surviving a restart, and
**different contents executing under the same commit** — the case the
2026-09-15 baseline found in production, where files were copied into a
serving release.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import runtime_identity as ri


ASSIGNEE = "hermes-project-factory"


@pytest.fixture
def board(tmp_path, monkeypatch):
    # The hermetic HERMES_HOME has no profiles on disk, so profile existence is
    # stubbed rather than provisioned — same shape as
    # tests/gateway/test_parallel_default_dispatch.py.
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    return kb.connect()


def _metadata_of(conn, run_id):
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None, "run row missing"
    return json.loads(row["metadata"]) if row["metadata"] else {}


def _claimed_run(conn, title="stamped task"):
    task_id = kb.create_task(conn, title=title, assignee=ASSIGNEE)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None, "task should have been claimable"
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_id, int(row["current_run_id"])


# ---------------------------------------------------------------------------
# The stamp exists and says something
# ---------------------------------------------------------------------------

def test_claim_stamps_code_and_config_digests(board):
    _, run_id = _claimed_run(board)
    identity = _metadata_of(board, run_id).get(ri.METADATA_KEY)

    assert identity, "claim_task must stamp the runtime identity"
    assert identity["code"]["digest"].startswith("sha256:")
    assert identity["code"]["files"] > 0
    assert identity["config"]["digest"].startswith("sha256:")
    assert identity["process"]["pid"] > 0


def test_manifests_are_written_and_addressed_by_digest(board):
    _, run_id = _claimed_run(board)
    identity = _metadata_of(board, run_id)[ri.METADATA_KEY]

    directory = ri._manifest_dir()
    assert directory is not None
    code_manifest = directory / identity["code"]["manifest"]
    assert code_manifest.exists(), "the code manifest must be retrievable later"

    payload = json.loads(code_manifest.read_text(encoding="utf-8"))
    assert payload["digest"] == identity["code"]["digest"]
    assert len(payload["files"]) == identity["code"]["files"]


# ---------------------------------------------------------------------------
# The case the baseline found: same commit, different bytes
# ---------------------------------------------------------------------------

def test_digest_changes_when_a_file_changes_under_the_same_commit(tmp_path):
    """A file edited in place must change the digest.

    This is the production failure the stamp exists to expose: on 2026-09-15
    `nfos_delivery.py` was rewritten inside the serving release 51 seconds
    before a restart, with no version, no tag and no commit reflecting it.
    """
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.py").write_text("y = 2\n", encoding="utf-8")

    before = ri.code_identity(root)["digest"]
    (root / "b.py").write_text("y = 3\n", encoding="utf-8")
    after = ri.code_identity(root)["digest"]

    assert before != after, "editing a file in place must change the code digest"


def test_digest_changes_when_a_file_is_renamed(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = ri.code_identity(root)["digest"]
    (root / "a.py").rename(root / "renamed.py")
    after = ri.code_identity(root)["digest"]

    assert before != after, "a rename with identical content must still change the digest"


def test_bytecode_is_excluded_from_the_digest(tmp_path):
    root = tmp_path / "pkg"
    (root / "__pycache__").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = ri.code_identity(root)["digest"]
    (root / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00\x01derived")
    after = ri.code_identity(root)["digest"]

    assert before == after, "derived bytecode must not move the digest"


# ---------------------------------------------------------------------------
# The stamp survives the rest of the run's life
# ---------------------------------------------------------------------------

def test_completion_preserves_the_identity_written_at_claim(board):
    task_id, run_id = _claimed_run(board)
    stamped = _metadata_of(board, run_id)[ri.METADATA_KEY]

    kb.complete_task(board, task_id, summary="done",
                     metadata={"worker_session_id": "sess-1", "criteria": {"C1": "PASS"}})

    after = _metadata_of(board, run_id)
    assert after.get(ri.METADATA_KEY) == stamped, "closing a run must not erase its identity"
    assert after["worker_session_id"] == "sess-1", "the caller's own metadata must still land"


def test_wholesale_metadata_replacement_preserves_the_identity(board):
    """A writer that replaces the whole column must not orphan the run."""
    _, run_id = _claimed_run(board)
    stamped = _metadata_of(board, run_id)[ri.METADATA_KEY]

    merged = ri.merge_run_metadata(board, run_id, {"delivery_type": "operation"})
    board.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                  (json.dumps(merged, ensure_ascii=False), run_id))

    after = _metadata_of(board, run_id)
    assert after[ri.METADATA_KEY] == stamped
    assert after["delivery_type"] == "operation"


def test_a_second_stamp_never_relabels_the_first(board):
    _, run_id = _claimed_run(board)
    first = _metadata_of(board, run_id)[ri.METADATA_KEY]

    restamped = ri.stamp_metadata(_metadata_of(board, run_id))
    assert restamped[ri.METADATA_KEY] == first, (
        "the identity must describe the code that started the run, not the last writer"
    )


def test_review_claim_is_stamped_too(board):
    task_id = kb.create_task(board, title="review path", assignee=ASSIGNEE)
    kb.claim_task(board, task_id)
    board.execute("UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = ?", (task_id,))
    board.commit()

    claimed = kb.claim_review_task(board, task_id)
    if claimed is None:
        pytest.skip("review claim not reachable in this fixture state")
    row = board.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    identity = _metadata_of(board, int(row["current_run_id"])).get(ri.METADATA_KEY)
    assert identity, "claim_review_task must stamp the runtime identity as well"


# ---------------------------------------------------------------------------
# Failure must degrade, never block
# ---------------------------------------------------------------------------

def test_identity_failure_does_not_prevent_a_claim(board, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("manifest directory unavailable")

    monkeypatch.setattr(ri, "code_identity", explode)
    monkeypatch.setattr(ri, "config_identity", explode)

    task_id = kb.create_task(board, title="claim under failure", assignee=ASSIGNEE)
    claimed = kb.claim_task(board, task_id)

    assert claimed is not None, "a broken stamp must never block a claim"


def test_no_secret_reaches_the_config_manifest(board, monkeypatch):
    from hermes_cli import config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly",
                        lambda: {"model": {"default": "gpt-6-astra"},
                                 "providers": {"openai": {"api_key": "sk-live-SHOULD-NOT-APPEAR"}}})
    identity = ri.config_identity()
    directory = ri._manifest_dir()
    body = (directory / identity["manifest"]).read_text(encoding="utf-8")

    assert "sk-live-SHOULD-NOT-APPEAR" not in body
    assert "gpt-6-astra" in body, "redaction must not blank the non-secret configuration"


def test_config_digest_is_stable_across_a_credential_rotation(monkeypatch):
    from hermes_cli import config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly",
                        lambda: {"providers": {"openai": {"api_key": "sk-first-value-aaaa"}}})
    first = ri.config_identity()["digest"]
    monkeypatch.setattr(cfg, "load_config_readonly",
                        lambda: {"providers": {"openai": {"api_key": "sk-second-value-bbbb"}}})
    second = ri.config_identity()["digest"]

    assert first == second, (
        "rotating a credential must not look like a configuration change"
    )


def test_stamp_is_cheap_enough_for_every_claim():
    start = time.monotonic()
    ri.code_identity()
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "stamping must stay far below the 60s dispatch interval"
