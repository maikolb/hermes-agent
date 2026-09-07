"""Real temporary Kanban startup identity, without constructing an LLM."""
import json

import pytest

from hermes_cli import kanban_db as kb
from tools import delegation_kanban as dk


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "root" / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    with kb.connect_closing(board="default") as conn:
        yield conn


def claim(conn, title="Work"):
    task_id = kb.create_task(conn, title=title, assignee="default")
    task = kb.claim_task(conn, task_id)
    assert task and task.current_run_id and task.claim_lock
    return task


def metadata(conn, run_id):
    raw = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    return json.loads(raw) if raw else {}


def bind(conn, task, session="worker-session", **changes):
    kwargs = dict(run_id=task.current_run_id, claim_lock=task.claim_lock, session_id=session)
    kwargs.update(changes)
    return kb.bind_worker_session(conn, task.id, **kwargs)


def test_binding_is_idempotent_preserves_metadata_and_emits_one_event(board):
    task = claim(board)
    with kb.write_txn(board):
        board.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                      (json.dumps({"artifact": "existing"}), task.current_run_id))
    assert bind(board, task)
    assert bind(board, task)
    assert metadata(board, task.current_run_id) == {"artifact": "existing", "worker_session_id": "worker-session"}
    rows = board.execute("SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = ?",
                         (task.id, "worker_session_linked")).fetchall()
    assert len(rows) == 1 and rows[0]["run_id"] == task.current_run_id
    assert json.loads(rows[0]["payload"])["worker_session_id"] == "worker-session"
    assert not bind(board, task, session="other-worker")


def test_binding_rejects_foreign_run_claim_and_task(board):
    task = claim(board)
    other = claim(board, "Other")
    assert not bind(board, task, run_id=other.current_run_id)
    assert not bind(board, task, claim_lock="wrong")
    assert not kb.bind_worker_session(board, "missing", run_id=task.current_run_id,
                                      claim_lock=task.claim_lock, session_id="worker")
    assert metadata(board, task.current_run_id) == {}


def test_retry_fence_rejects_old_attempt_and_terminal_keeps_binding(board):
    old = claim(board)
    assert bind(board, old)
    kb.block_task(board, old.id, reason="retry", kind="needs_input")
    assert metadata(board, old.current_run_id)["worker_session_id"] == "worker-session"
    assert not bind(board, old, session="late")
    kb.unblock_task(board, old.id)
    current = kb.claim_task(board, old.id)
    assert current and current.current_run_id != old.current_run_id
    assert not bind(board, old, session="stale-attempt")
    assert bind(board, current, session="new-attempt")
    assert metadata(board, current.current_run_id)["worker_session_id"] == "new-attempt"


def test_mirror_captures_claim_and_binds_constructed_child_before_execution(board):
    captured = {}
    cards = dk.create_delegation_cards([{"goal": "Live child"}], "deleg-live", "default",
                                       run_bindings=captured)
    assert 0 in cards and 0 in captured
    assert dk.bind_delegation_session("default", cards[0], captured[0], "child-session")
    row = board.execute("SELECT status,metadata FROM task_runs WHERE id = ?", (captured[0][0],)).fetchone()
    assert row["status"] == "running"
    assert json.loads(row["metadata"])["worker_session_id"] == "child-session"


def test_dispatcher_existing_env_handoff_and_delegated_context_isolation(board, monkeypatch):
    from agent.delegation_context import delegated_child_context

    task = claim(board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    with delegated_child_context("nested-child"):
        assert not kb.bind_dispatcher_session("nested-child")
    assert metadata(board, task.current_run_id) == {}
    assert kb.bind_dispatcher_session("dispatcher-session")
    assert metadata(board, task.current_run_id)["worker_session_id"] == "dispatcher-session"
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "expired")
    assert not kb.bind_dispatcher_session("late-session")
