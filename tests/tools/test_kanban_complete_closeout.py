"""Closeout persistence + AOF enforcement on kanban_complete (28/08).

Run 57 delivered a full closeout in ``summary`` and the board recorded
``result_len: 0`` — every surface that reads ``task.result`` went out
empty. The tool now persists a summary-only handoff as the result, and
refuses a worker's own completion when the closeout is not substantive
(enforcement in the tool, not in the prompt).
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from tools.kanban_tools import _handle_complete

CLOSEOUT = (
    "Scope: corrigir tutor. Done: fix publicado. "
    "Evidence: staging validado, PR #300. Limitations: nenhuma."
)


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", raising=False)
    kb._INITIALIZED_PATHS = set()
    kb.init_db()
    return tmp_path


def _make_claimed_task():
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="w", assignee="hermes")
        kb.claim_task(conn, task_id, ttl_seconds=3600)
    finally:
        conn.close()
    return task_id


def _result_of(task_id):
    conn = kb.connect()
    try:
        return kb.get_task(conn, task_id).result
    finally:
        conn.close()


def test_summary_only_persists_as_result(board):
    task_id = _make_claimed_task()
    out = _handle_complete({"task_id": task_id, "summary": CLOSEOUT})
    assert "error" not in json.loads(out) or json.loads(out).get("ok")
    assert _result_of(task_id) == CLOSEOUT


def test_worker_short_closeout_is_refused(board, monkeypatch):
    task_id = _make_claimed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    out = _handle_complete({"task_id": task_id, "summary": "ok"})
    assert "closeout too short" in out
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status != "done"
    finally:
        conn.close()

    out2 = _handle_complete({"task_id": task_id, "summary": CLOSEOUT})
    assert "closeout too short" not in out2
    assert _result_of(task_id) == CLOSEOUT


def test_escape_hatch_disables_enforcement(board, monkeypatch):
    task_id = _make_claimed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", "off")
    out = _handle_complete({"task_id": task_id, "summary": "ok"})
    assert "closeout too short" not in out


def test_non_worker_caller_not_enforced(board):
    """A principal/human completing a card (no worker env) keeps the old
    contract: any non-empty summary is accepted."""
    task_id = _make_claimed_task()
    out = _handle_complete({"task_id": task_id, "summary": "triagem ok"})
    assert "closeout too short" not in out


STATUS_CLOSEOUT = (
    "**Em execução.** Estou preparando o repositório privado e o contrato "
    "local; em seguida farei o push inicial e a configuração do deploy."
)


def test_worker_status_closeout_is_refused(board, monkeypatch):
    """A done record must state what WAS done (28/08 Central_DEC: a card
    closed done in 40s with a status line and the operator read the
    system as stalled)."""
    task_id = _make_claimed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    out = _handle_complete({"task_id": task_id, "summary": STATUS_CLOSEOUT})
    assert "closeout rejected" in out
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status != "done"
    finally:
        conn.close()

    out2 = _handle_complete({"task_id": task_id, "summary": CLOSEOUT})
    assert "closeout rejected" not in out2
    assert _result_of(task_id) == CLOSEOUT


def test_status_mention_mid_text_still_passes(board, monkeypatch):
    """Only the OPENING is judged — a delivered closeout that mentions a
    pending item mid-text is legitimate."""
    task_id = _make_claimed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    closeout = (
        "Done: repo criado e push feito, PR #12 aberto. Evidence: CI verde. "
        "Limitations: aguardando aprovação do deploy pelo operador."
    )
    out = _handle_complete({"task_id": task_id, "summary": closeout})
    assert "closeout rejected" not in out
    assert _result_of(task_id) == closeout


def test_status_closeout_escape_hatch(board, monkeypatch):
    task_id = _make_claimed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", "off")
    out = _handle_complete({"task_id": task_id, "summary": STATUS_CLOSEOUT})
    assert "closeout rejected" not in out
