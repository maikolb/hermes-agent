"""NO_PARK_20260910: em owner mode um card NFOS só entra em blocked como pergunta a humano nomeado; pausa técnica é
recusada na transição (block_task), com evento block_refused. Worker vivo continua escalando ao principal."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

PAUSE = "Pausa técnica confirmada: receipts kanban_complete recusam entrega parcial HML; não redisparar."
QUESTION = "PERGUNTA para Maikol: autoriza resolver o conflito do PR #56 e integrar em produção?"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return home


def _running_card(conn):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "12"},
        text="Criar produto abre diálogo nativo do navegador.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def _worker_gone(conn, task):
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
                 (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id)


def _last_event(conn, task_id):
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return row[0], json.loads(row[1] or "{}")


def test_technical_pause_is_refused_on_a_ready_card(board):
    with kb.connect_closing() as conn:
        task = _worker_gone(conn, _running_card(conn))
        assert kb.block_task(conn, task.id, reason=PAUSE, kind="capability") is False
        assert kb.get_task(conn, task.id).status == "ready"
        kind, payload = _last_event(conn, task.id)
        assert kind == "block_refused"
        assert "question to a named human" in payload["why"]


def test_question_to_a_named_human_blocks(board):
    with kb.connect_closing() as conn:
        task = _worker_gone(conn, _running_card(conn))
        assert kb.block_task(conn, task.id, reason=QUESTION, kind="needs_input") is True
        assert kb.get_task(conn, task.id).status == "blocked"


def test_pause_is_allowed_outside_owner_mode(board, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        task = _worker_gone(conn, _running_card(conn))
        assert kb.block_task(conn, task.id, reason=PAUSE, kind="capability") is True
        assert kb.get_task(conn, task.id).status == "blocked"


def test_live_worker_still_escalates_to_the_principal(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _running_card(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
        from tools import kanban_tools as kt
        out = kt._handle_block({"reason": PAUSE, "kind": "capability"})
        assert '"ok": true' in out
        assert kb.get_task(conn, task.id).status == "running"
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchone()[0] == 1


def test_tool_reports_the_refusal_on_a_ready_card(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _worker_gone(conn, _running_card(conn))
        monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        from tools import kanban_tools as kt
        out = kt._handle_block({"reason": PAUSE, "kind": "capability"})
        assert "kanban_block refused" in out
        assert kb.get_task(conn, task.id).status == "ready"
