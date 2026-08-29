"""Dispatcher worker protocol + closeout fallback (28/08 audit findings).

The audit caught a dispatcher worker completing a card with an empty
``result``: no protocol was injected on that path and the completion
trace went out as a bare title + link. Two guarantees now: the spawn
prompt carries the AOF protocol demanding the closeout in ``result``,
and the trace reader falls back to the worker's last substantive comment
when the result is empty anyway.
"""

from __future__ import annotations

import pytest

from gateway.kanban_watchers import _read_worker_trace_summary
from hermes_cli import kanban_db as kb
from hermes_cli.worker_protocol import dispatcher_worker_protocol


def test_protocol_demands_closeout_in_result():
    text = dispatcher_worker_protocol()
    assert "SCOPE" in text
    assert "PREFLIGHT" in text
    assert "kanban_complete" in text
    assert "`result`" in text
    assert "kanban_block" in text
    assert "Limitations" in text


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
        task_id = kb.create_task(conn, title="w", assignee="hermes")
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
        task_id = kb.create_task(conn, title="w", assignee="hermes")
        kb.add_comment(conn, task_id, "hermes", "comentário antigo")
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
    assert "Worker Protocol (AOF)" in joined
    assert "kanban_complete" in joined


def test_protocol_formalizes_deliver_phase():
    """T6/G6 (spec): the dev loop spec->build->review/evidence->delivery is
    a single contract in the protocol — delivery is a phase of the cycle,
    never a wait for a human (A_BASE.md; skill v3)."""
    text = dispatcher_worker_protocol()
    assert "4. DELIVER" in text
    assert "green PR merges" in text
    assert "never waiting for a human" in text
    assert "validate the live target" in text
    assert "5. CLOSEOUT" in text
