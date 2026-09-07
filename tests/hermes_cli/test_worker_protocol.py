"""Dispatcher worker protocol + closeout fallback (28/08 audit findings).

The audit caught a dispatcher worker completing a card with an empty
``result``: no protocol was injected on that path and the completion
trace went out as a bare title + link. Two guarantees now: the spawn
prompt carries the current delivery workflow demanding evidence in ``result``,
and the trace reader falls back to the worker's last substantive comment
when the result is empty anyway.
"""

from __future__ import annotations

import pytest

from gateway.kanban_watchers import _read_worker_trace_summary
from hermes_cli import kanban_db as kb
from hermes_cli.worker_protocol import dispatcher_worker_protocol


def test_protocol_demands_verified_result_and_persisted_impediments():
    text = dispatcher_worker_protocol()
    assert "current card, original request, attachments, prior work" in text
    assert "Persist the versioned spec on the card before implementation" in text
    assert "Principal's persisted review queue" in text
    assert "persist the\nstate and exit with the card awaiting that answer" in text
    assert "kanban_complete result must describe the outcome, evidence and any limitations" in text
    assert "Worker Protocol (AOF)" not in text
    assert "PREFLIGHT" not in text


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    kb.init_db()
    return tmp_path


def test_completed_empty_result_falls_back_to_last_worker_comment(board):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="w", assignee="default")
        kb.add_comment(
            conn, task_id, "hermes-project-factory",
            "Closeout: Scope X / Done Y / Evidence Z.",
        )
        kb.add_comment(
            conn, task_id, "watchdog",
            "Alerta de abandono publicado no tópico.",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', result=NULL WHERE id=?",
                (task_id,),
            )
    finally:
        conn.close()

    summary = _read_worker_trace_summary("default", task_id, "completed")
    assert summary == "Closeout: Scope X / Done Y / Evidence Z."


def test_completed_with_result_keeps_result(board):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="w", assignee="default")
        kb.add_comment(conn, task_id, "default", "comentário antigo")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', result='closeout real' "
                "WHERE id=?",
                (task_id,),
            )
    finally:
        conn.close()

    assert _read_worker_trace_summary("default", task_id, "completed") == (
        "closeout real"
    )


def test_spawn_prompt_carries_protocol(board, monkeypatch, tmp_path):
    """The dispatcher spawn command must ship the protocol block."""
    captured = {}

    import hermes_cli.kanban_db as kdb

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(kdb.subprocess, "Popen", _fake_popen)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="w", assignee="default")
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    kdb._default_spawn(task, str(tmp_path))

    joined = " ".join(str(part) for part in captured["cmd"])
    assert dispatcher_worker_protocol() in joined
    assert "Worker Protocol (AOF)" not in joined


def test_protocol_requires_review_and_production_readback_for_code_delivery():
    """The authorized workflow includes review, delivery and target evidence;
    reports without code retain their applicable non-deploy completion path."""
    text = dispatcher_worker_protocol()
    stages = [
        "Persist the versioned spec on the card before implementation",
        "Use Codex implement/test/correct cycles with persisted progress",
        "Validate the candidate in the project's homolog environment",
        "Create or update the PR with spec and evidence, then request Principal review",
        "Confirm the deployed version and functional production readback",
        "spec, PR reference and evidence report on the card before kanban_complete",
    ]
    offsets = [text.index(stage) for stage in stages]
    assert offsets == sorted(offsets)
    assert "Principal can approve merge and deployment within the authorized scope" in text
    assert "Reports and audits without code changes do not require a PR or deployment" in text
