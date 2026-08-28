"""Per-worker closeout traces (TARGET_ARCHITECTURE gap 5).

Every worker that finishes terminally publishes ITS OWN closeout — per
worker, not per display lane. Two workers finishing together produce two
traces; a blocked worker reports its reason; a worker that never held the
focus bubble still gets a trace as its own message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import gateway.kanban_watchers as kw
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        from gateway.platforms.base import SendResult

        self.sent.append(
            {"chat_id": chat_id, "text": text, "metadata": metadata or {}}
        )
        return SendResult(success=True, message_id=str(len(self.sent)))


def _sub():
    return {
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "topic-7",
        "notifier_profile": "",
        "delivery_metadata": {"thread_id": "topic-7"},
    }


def _exit_info(kind, title="tarefa delegada", run_id=3):
    return {"kind": kind, "sub": _sub(), "title": title, "run_id": run_id}


def _runner(adapter, monkeypatch, *, rotation=True):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._active_profile_name = lambda: "default"
    runner._kanban_worker_focus_active = {}
    runner._kanban_worker_focus_states = {}
    runner._kanban_worker_display_scopes = {}
    monkeypatch.setattr(
        kw,
        "_load_worker_focus_config",
        lambda profile, loader: {
            "display": {"worker_rotation": rotation}
        },
    )
    return runner


EXIT_KEY = ("boardx", "telegram", "chat-1", "topic-7", "default")


def test_two_unfocused_finishers_publish_two_closeouts(monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch)
    monkeypatch.setattr(
        kw,
        "_read_worker_trace_summary",
        lambda board, task_id, kind: f"Scope: closeout de {task_id}",
    )
    runner._kanban_worker_focus_exits = {
        EXIT_KEY: {
            "t_aaa": _exit_info("completed", title="worker A"),
            "t_bbb": _exit_info("completed", title="worker B"),
        }
    }

    asyncio.run(runner._kanban_refresh_worker_focus())

    assert len(adapter.sent) == 2
    texts = "\n---\n".join(m["text"] for m in adapter.sent)
    assert "✅ Worker concluído: worker A" in texts
    assert "✅ Worker concluído: worker B" in texts
    assert "closeout de t_aaa" in texts
    assert "closeout de t_bbb" in texts
    assert runner._kanban_worker_focus_exits == {}
    # Thread routing preserved so the trace lands in the right topic.
    assert all(
        m["metadata"].get("thread_id") == "topic-7" for m in adapter.sent
    )


def test_blocked_worker_publishes_blocked_closeout(monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch)
    monkeypatch.setattr(
        kw,
        "_read_worker_trace_summary",
        lambda board, task_id, kind: "bloqueado: falta credencial",
    )
    runner._kanban_worker_focus_exits = {
        EXIT_KEY: {"t_blk": _exit_info("blocked", title="worker travado")}
    }

    asyncio.run(runner._kanban_refresh_worker_focus())

    assert len(adapter.sent) == 1
    assert "⛔ Worker bloqueado: worker travado" in adapter.sent[0]["text"]
    assert "falta credencial" in adapter.sent[0]["text"]


def test_non_traceable_exits_stay_silent_and_drain(monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch)
    runner._kanban_worker_focus_exits = {
        EXIT_KEY: {
            "t_crash": _exit_info("crashed"),
            "t_stale": _exit_info("stale"),
            "t_recl": _exit_info("reclaimed"),
        }
    }

    asyncio.run(runner._kanban_refresh_worker_focus())

    assert adapter.sent == []
    assert runner._kanban_worker_focus_exits == {}


def test_rotation_disabled_suppresses_closeout_traces(monkeypatch):
    adapter = RecordingAdapter()
    runner = _runner(adapter, monkeypatch, rotation=False)
    runner._kanban_worker_focus_exits = {
        EXIT_KEY: {"t_off": _exit_info("completed")}
    }

    asyncio.run(runner._kanban_refresh_worker_focus())

    assert adapter.sent == []


def test_render_trace_blocked_uses_reason_and_link():
    content = kw._render_worker_trace_content(
        kind="blocked",
        title="auditar limites",
        board="dovcrm",
        task_id="t_1",
        run_id=7,
        summary="Limitations: aguardando decisão do operador",
        trace_url_template="https://vigilia/#{board}/{task_id}",
    )
    assert content.startswith("⛔ Worker bloqueado: auditar limites")
    assert "aguardando decisão do operador" in content
    assert "[dovcrm] Kanban t_1 · run 7" in content
    assert "https://vigilia/#dovcrm/t_1" in content


def test_read_summary_completed_result_and_blocked_comment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    real_connect = kb.connect

    # The reader passes board=...; pin any board name to the temp DB so the
    # test exercises the read logic, not board directory resolution.
    def _connect(db_path=None, *, board=None):
        return real_connect()

    monkeypatch.setattr(kb, "connect", _connect)

    conn = kb.connect()
    try:
        done_id = kb.create_task(conn, title="done worker", assignee="w1")
        kb.claim_task(conn, done_id, claimer="worker:w1")
        kb.complete_task(conn, done_id, result="Scope: X\nDone: Y")

        blocked_id = kb.create_task(conn, title="stuck worker", assignee="w2")
        kb.claim_task(conn, blocked_id, claimer="worker:w2")
        kb.block_task(conn, blocked_id, reason="sem acesso", kind="needs_input")
        kb.add_comment(conn, blocked_id, "worker", "Limitations: sem acesso ao S3")
    finally:
        conn.close()

    assert (
        kw._read_worker_trace_summary("b", done_id, "completed")
        == "Scope: X\nDone: Y"
    )
    assert (
        kw._read_worker_trace_summary("b", blocked_id, "blocked")
        == "Limitations: sem acesso ao S3"
    )


def test_live_transcript_converts_to_worker_log_dialect():
    """FNAT RTU (28/08): in-process workers stream to the live transcript;
    its lines must surface as Reasoning/tool items in the focus bubble."""
    live = "\n".join([
        "  Task 0: Executar TZ1",
        "12:00:01 start    | goal accepted",
        "12:00:02 think    | Bloco 1 revisado, seguindo pro 2",
        "12:00:03 tool     | -> write_file(notas/TZ1.md)",
        "12:00:04 result   | write_file ok 0.3s: 5 itens gravados",
        "12:00:05 assistant| parcial entregue",
    ])
    converted = kw._live_transcript_to_worker_log(live)
    assert "┌─ Reasoning" in converted
    assert "│ Bloco 1 revisado, seguindo pro 2" in converted
    assert "┊ Tool: write_file(notas/TZ1.md)" in converted
    assert "┊ write_file ok 0.3s: 5 itens gravados" in converted
    # Renders through the EXISTING bubble renderer:
    out = kw._render_kanban_worker_focus_output(
        converted, task_id="t_x", include_tool_progress=True,
        include_reasoning=True,
    )
    assert "Reasoning: Bloco 1 revisado" in out
    assert "┊ Tool: write_file" in out


def test_mirror_live_transcript_read_from_card_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    real_connect = kb.connect
    monkeypatch.setattr(kb, "connect", lambda db_path=None, board=None: real_connect())

    live_path = tmp_path / "task-0.log"
    live_path.write_text(
        "12:00:02 think    | validando bloco\n"
        "12:00:03 tool     | -> read_file(x)\n",
        encoding="utf-8",
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="mirror", assignee="w")
        kb.add_comment(
            conn, tid, "delegation",
            f"Mirror card for in-process delegation d1 task 0. "
            f"Live transcript: {live_path}",
        )
    finally:
        conn.close()

    out = kw._read_mirror_live_transcript("b", tid, 64 * 1024)
    assert "┌─ Reasoning" in out
    assert "┊ Tool: read_file(x)" in out
