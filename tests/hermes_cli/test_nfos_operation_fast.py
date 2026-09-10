"""OPERATION_FAST_20260910: operation cards skip the code preflight, but cannot be used to skip the code path."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d
from hermes_cli import nfos_runtime as rt

SHA = "a" * 40


@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid = d.receive_request(
            conn, source={"platform": "telegram", "chat_id": "1", "thread_id": "2", "message_id": "3"},
            text="Suba o plano do tenant X",
            project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path)},
        )
        req = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid())
    return tmp_path / "kanban.db", task


def _spec(**extra):
    spec = {"goal": "Plan scale", "criteria": [{"id": "C1", "text": "plan=scale"}], "steps": ["read", "apply", "readback"]}
    spec.update(extra)
    return spec


def _save(conn, task, spec):
    return d.save_spec(conn, task.id, task.current_run_id, spec, author="worker", evidence={"source": "worker"})


def _task_row(conn, tid):
    return conn.execute("SELECT delivery_type, requires_repo FROM tasks WHERE id=?", (tid,)).fetchone()


def test_operation_spec_needs_target_mutation_and_no_code_reason(card):
    db, task = card
    with kb.connect_closing() as conn:
        with pytest.raises(d.WorkflowError, match="operation.target"):
            _save(conn, task, _spec(delivery_type="operation"))
        _save(conn, task, _spec(delivery_type="operation", operation={"target": "prod tenant X", "mutation": "plan starter -> scale"},
                                no_code_reason="platform store call, no repository change"))
        assert _task_row(conn, task.id)["delivery_type"] == "operation"


def test_operation_card_cannot_publish_code(card):
    db, task = card
    with kb.connect_closing() as conn:
        _save(conn, task, _spec(delivery_type="operation", operation={"target": "prod", "mutation": "plan"}, no_code_reason="no code"))
        for op in ("pr", "merge", "deploy", "homolog"):
            with pytest.raises(d.WorkflowError, match="publishes no code"):
                d.begin_effect(conn, task.id, task.current_run_id, operation=op, target="https://github.com/o/r", candidate=SHA)


def test_reclassify_back_to_code_needs_worktree_and_restores_git_delivery(card):
    db, task = card
    with kb.connect_closing() as conn:
        _save(conn, task, _spec(delivery_type="operation", operation={"target": "prod", "mutation": "plan"}, no_code_reason="no code"))
        conn.execute("UPDATE tasks SET workspace_kind='scratch' WHERE id=?", (task.id,))
        conn.commit()
        with pytest.raises(d.WorkflowError, match="worktree"):
            _save(conn, task, _spec(delivery_type="code"))
        conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (task.id,))
        conn.commit()
        _save(conn, task, _spec(delivery_type="code"))
        row = _task_row(conn, task.id)
        assert row["delivery_type"] == "code" and int(row["requires_repo"]) == 1
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? AND kind='nfos_delivery_classified'", (task.id,))]
        assert len(kinds) == 2


def test_worker_text_limits_preflight_to_code():
    text = rt.worker_instructions()
    assert "For a code task, check existing Git changes/PRs" in text
    assert "For every task, check existing Git changes/PRs" not in text
    assert "skip HML, repository and PR reconciliation" in text
