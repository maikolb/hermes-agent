"""In-process workers tee into the board's NATIVE worker log.

Operator requirement (28/08): one mechanism, one rendering, no lost
visibility — the FNAT bubble and the Vigília activity panel read the same
board worker log that dispatcher workers write; in-process workers must
surface there too, in the same dialect.
"""

from __future__ import annotations

import pytest

from tools.delegation_live_log import LiveTranscriptWriter


@pytest.fixture()
def board_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(
        kb, "worker_logs_dir", lambda board=None: tmp_path / "logs"
    )
    return tmp_path


def _writer(tmp_path):
    return LiveTranscriptWriter(
        "deleg_x", 0, "Executar TV1", root=tmp_path / "live"
    )


def test_tee_writes_native_dialect(board_env, tmp_path):
    w = _writer(tmp_path)
    w.attach_board_log("default", "t_abc123", goal="Executar TV1")
    w.tool_start("terminal", "git status --short")
    w.tool_result("terminal", result="clean", duration=1.34)
    w.thinking("bloco 1 revisado, seguindo")
    w.tool_start("read_file", "notas/TV1.md")
    w.tool_result("read_file", result="ok", duration=0.2, is_error=True)
    w.marker("status=completed duration=88s")

    log = (board_env / "logs" / "t_abc123.log").read_text(encoding="utf-8")
    assert "Query: work kanban task t_abc123" in log
    assert "┊ 💻 terminal" in log and "git status --short" in log and "1.3s" in log
    assert "┌─ Reasoning" in log and "│ bloco 1 revisado, seguindo" in log
    assert "⚠ 🔎 read_file" in log
    assert "── status=completed" in log

    # The FNAT bubble renderer consumes this file as-is:
    from gateway.kanban_watchers import _render_kanban_worker_focus_output

    out = _render_kanban_worker_focus_output(
        log, task_id="t_abc123", include_tool_progress=True,
        include_reasoning=True,
    )
    # Re-rendered into the principal's surface language (28/08): friendly
    # verb + args, thought line for reasoning — not raw log dialect.
    assert "Running git status --short" in out
    assert "💭 bloco 1 revisado, seguindo" in out
    assert "┊" not in out


def test_no_attach_means_no_board_log(board_env, tmp_path):
    w = _writer(tmp_path)
    w.tool_start("terminal", "x")
    w.tool_result("terminal", duration=0.1)
    assert not (board_env / "logs").exists()


def test_attach_failure_is_silent(tmp_path, monkeypatch):
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(
        kb, "worker_log_path",
        lambda task_id, board=None: (_ for _ in ()).throw(RuntimeError("x")),
    )
    w = _writer(tmp_path)
    w.attach_board_log("default", "t_abc123")
    w.tool_result("terminal", duration=0.1)  # must not raise
    assert w._board_log_path is None


def test_rtu_sidecar_renders_at_event_via_shared_renderer(board_env, tmp_path):
    """RTU no log vivo do Vigília (28/08): o tee grava <task>.rtu.log com as
    MESMAS linhas do renderer único (verbo amigável, 💭, erro curto), no
    momento do evento — nenhum re-parse, nenhum segundo renderer."""
    w = _writer(tmp_path)
    w.attach_board_log("boardx", "t_rtu1", goal="TZ de teste")
    w.thinking("bloco 1 revisado, seguindo")
    w.tool_start("terminal", "git status --short")
    w.tool_result("terminal", result="ok", duration=1.3)
    w.tool_start("read_file", "notas/TV1.md")
    w.tool_result(
        "read_file",
        result='{"content": "", "error": "File not found: notas/TV1.md"}',
        duration=0.2,
        is_error=True,
    )
    w.marker("status=completed duration=88s")

    rtu = (board_env / "logs" / "t_rtu1.rtu.log").read_text(encoding="utf-8")
    assert "Query: work kanban task t_rtu1" in rtu
    assert "💭 bloco 1 revisado, seguindo" in rtu
    assert "Running git status --short" in rtu
    assert "⚠ read_file: File not found: notas/TV1.md" in rtu
    assert "── status=completed" in rtu
    # raw dialect stays raw; sidecar stays rendered — no cross-bleed
    assert "┊" not in rtu
    raw = (board_env / "logs" / "t_rtu1.log").read_text(encoding="utf-8")
    assert "┊" in raw
