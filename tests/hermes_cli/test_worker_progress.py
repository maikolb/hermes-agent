"""Kanban worker stdout progress printer (28/08 Wave 4 dead-display fix)."""

from __future__ import annotations

from hermes_cli.kanban_db import Task
from hermes_cli.worker_progress import make_worker_progress_printer


def test_tool_started_prints_rendered_line(capsys):
    printer = make_worker_progress_printer()
    printer(
        "tool.started", "terminal", "ls -la", {"command": "ls -la"},
    )
    out = capsys.readouterr().out
    assert out.strip(), "tool.started must emit a line"
    assert "ls -la" in out


def test_tool_failed_prints_error_line(capsys):
    printer = make_worker_progress_printer()
    printer("tool.failed", "web_fetch", '{"error": "boom kaboom"}', None)
    out = capsys.readouterr().out
    assert "boom kaboom" in out


def test_unknown_event_and_bad_args_stay_silent(capsys):
    printer = make_worker_progress_printer()
    printer("reasoning.available", "x", None, None)
    printer("tool.started", None, object(), "not-a-dict")
    out = capsys.readouterr().out
    # The malformed started event may still render a generic line, but
    # nothing may raise; the unknown event must print nothing.
    assert "reasoning" not in out


def test_task_model_exposes_worker_started_at(tmp_path, monkeypatch):
    """The focus bubble derives elapsed from worker_started_at — the model
    must surface the column (before this, getattr silently fell back to the
    card's first-claim started_at and showed day-old elapsed)."""
    import sqlite3

    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="w", assignee="hermes")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at=1000, worker_started_at=2000.5 "
                "WHERE id=?",
                (task_id,),
            )
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    assert isinstance(task, Task)
    assert task.started_at == 1000
    assert task.worker_started_at == 2000.5
