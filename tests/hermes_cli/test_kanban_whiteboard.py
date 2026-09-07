"""Real SQLite contracts for the user-visible whiteboard workflow."""
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  default_assignee: default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli import kanban_db as kb
    token = set_hermes_home_override(home)
    try:
        with kb.connect_closing() as conn:
            yield kb, conn
    finally:
        reset_hermes_home_override(token)


def test_report_is_durable_and_requires_result_not_pr(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Count active users", requires_repo=False)
    task = kb.get_task(conn, tid)
    assert task.requires_repo is False and task.delivery_type == "report"
    assert not kb.complete_task(conn, tid)
    assert kb.complete_task(conn, tid, result="Count and query attached; target checked.")
    assert kb.get_task(conn, tid).status == "done"
    assert conn.execute("SELECT * FROM task_git_delivery WHERE task_id=?", (tid,)).fetchone() is None


def test_activity_never_enters_worker_queue(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Conversation activity", task_role="activity", requires_repo=False)
    assert kb.claim_task(conn, tid) is None
    assert not kb.has_spawnable_ready(conn)
    task = kb.claim_task(conn, tid, allow_activity=True)
    assert task.task_role == "activity"
    assert kb.complete_task(conn, tid, result="Conversation ended", expected_run_id=task.current_run_id)


def test_two_creators_share_one_postit(board, monkeypatch):
    kb, conn = board
    db = kb.kanban_db_path()
    original = kb._new_task_id
    barrier = threading.Barrier(2)

    def rendezvous():
        barrier.wait(timeout=10)
        return original()

    monkeypatch.setattr(kb, "_new_task_id", rendezvous)

    def create(_):
        with kb.connect_closing(db_path=db) as other:
            return kb.create_task(other, title="Same request", assignee="default", idempotency_key="telegram:41:123")

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_repeated_impediment_stays_one_task(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Prepare migration", requires_repo=False)
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="One credential is invalid", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        if task.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT:
            assert kb.unblock_task(conn, tid)
    from hermes_cli import kanban_decompose as decomp
    assert tid not in decomp.list_triage_ids()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_report_children_keep_delivery_contract(board):
    kb, conn = board
    root = kb.create_task(conn, title="Two independent reports", triage=True, requires_repo=False)
    children = kb.decompose_triage_task(conn, root, root_assignee="default", children=[
        {"title": "Count users", "assignee": "default"},
        {"title": "Count subscriptions", "assignee": "default"},
    ])
    for tid in children:
        task = kb.get_task(conn, tid)
        assert task.requires_repo is False and task.delivery_type == "report"
        assert task.workspace_kind == "scratch" and task.assignee == "default"
        assert kb.complete_task(conn, tid, result="Verified report with source.")
    assert kb.get_task(conn, root).status == "done"
    assert kb.get_task(conn, root).task_role == "aggregate"
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND claim_lock IS NOT NULL", (root,)).fetchone()[0] == 0


def test_code_cannot_use_report_completion(board):
    kb, conn = board
    with pytest.raises(ValueError, match="worktree"):
        kb.create_task(conn, title="Change code", delivery_type="code")
    tid = kb.create_task(conn, title="Change code", workspace_kind="worktree", delivery_type="code")
    task = kb.get_task(conn, tid)
    assert task.requires_repo is True
    assert not kb.complete_task(conn, tid, result="Changed code, without Git receipt")
    assert kb.get_task(conn, tid).status != "done"


def test_waiting_work_is_not_an_executing_person(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Waiting for credential", requires_repo=False)
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="Need credential for destination", kind="needs_input")
    view = kb.task_presentation(conn, kb.get_task(conn, tid))
    assert view["board_column"] == "running"
    assert view["work_in_progress"] is True and view["is_executing"] is False
    assert "credential" in view["block_reason"]


def test_recovery_closes_exact_attempt_and_ignores_old_completion(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Resume existing work", requires_repo=False)
    old = kb.claim_task(conn, tid, claimer=socket.gethostname() + ":2147483647")
    assert kb.recover_interrupted_task(conn, tid, expected_run_id=old.current_run_id,
                                       expected_claim=old.claim_lock, expected_heartbeat=None)
    task = kb.get_task(conn, tid)
    assert task.current_run_id is None and task.status == "ready"
    row = conn.execute("SELECT * FROM task_runs WHERE id=?", (old.current_run_id,)).fetchone()
    assert row["status"] == "reclaimed" and row["ended_at"] is not None
    new = kb.claim_task(conn, tid)
    assert not kb.recover_interrupted_task(conn, tid, expected_run_id=old.current_run_id,
                                           expected_claim=old.claim_lock, expected_heartbeat=None)
    assert not kb.complete_task(conn, tid, result="Late reply", expected_run_id=old.current_run_id)
    assert kb.get_task(conn, tid).current_run_id == new.current_run_id


def test_recovery_preserves_live_owner(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Live work", requires_repo=False)
    task = kb.claim_task(conn, tid)
    assert not kb.recover_interrupted_task(conn, tid, expected_run_id=task.current_run_id,
                                           expected_claim=task.claim_lock, expected_heartbeat=None)
    assert kb.get_task(conn, tid).status == "running"


def test_instruction_update_preserves_card_and_records_old_scope(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Migration", body="Wait for people", requires_repo=False)
    kb.update_task_instruction(conn, tid, body="Prepare before asking people", author="operator", expected_revision=0)
    task = kb.get_task(conn, tid)
    assert task.instruction_revision == 1 and task.body == "Prepare before asking people"
    with pytest.raises(ValueError, match="instruction changed"):
        kb.update_task_instruction(conn, tid, body="Old concurrent edit", author="operator", expected_revision=0)
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='instruction_updated'", (tid,)).fetchone()
    assert json.loads(event[0])["previous_body"] == "Wait for people"


def test_archived_request_identity_is_not_recreated(board):
    kb, conn = board
    tid = kb.create_task(conn, title="One request", requires_repo=False, idempotency_key="message:1")
    kb.archive_task(conn, tid)
    assert kb.create_task(conn, title="Retry same message", requires_repo=False, idempotency_key="message:1") == tid


def test_recovery_preserves_partial_attempt_evidence(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Partial work", requires_repo=False)
    task = kb.claim_task(conn, tid, claimer=socket.gethostname() + ":2147483647")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET summary=?, metadata=? WHERE id=?", ("Already counted users", '{"artifact":"report.md"}', task.current_run_id))
    assert kb.recover_interrupted_task(conn, tid, expected_run_id=task.current_run_id, expected_claim=task.claim_lock, expected_heartbeat=None)
    row = conn.execute("SELECT summary,metadata FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
    assert row["summary"] == "Already counted users" and json.loads(row["metadata"])["artifact"] == "report.md"


def test_decomposition_rejects_changed_then_restored_instruction(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Two reports", body="Original", requires_repo=False, triage=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body='Changed' WHERE id=?", (tid,))
        conn.execute("UPDATE tasks SET body='Original' WHERE id=?", (tid,))
    assert kb.decompose_triage_task(conn, tid, root_assignee="default", children=[{"title":"obsolete"}], expected_instruction=("Two reports", "Original"), expected_revision=0) == []
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_activity_does_not_consume_task_capacity(board):
    kb, conn = board
    tid = kb.create_task(conn, title="Helper record", task_role="activity", requires_repo=False)
    assert kb.claim_task(conn, tid, allow_activity=True)
    assert kb.count_running_tasks(conn) == 0
    work = kb.create_task(conn, title="Actual task", requires_repo=False)
    assert kb.claim_task(conn, work)
    assert kb.count_running_tasks(conn) == 1
