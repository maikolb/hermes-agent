"""Focus bubble passthrough for already-rendered worker log lines (28/08).

Dispatcher workers print shared-renderer lines to the board log
(worker_progress printer). The bubble's dialect parser expected raw log
shapes and silently dropped every rendered line — the operator saw a live
worker with an empty bubble minutes after the release shipped ("voltou,
mas não tá mostrando RTU").
"""

from __future__ import annotations

from gateway.kanban_watchers import _render_kanban_worker_focus_output

RENDERED_LOG = """Query: work kanban task t_ea06beaf

## Worker Protocol (AOF)
1. SCOPE - restate the card's requested outcome in one sentence.
- Scope: the one-sentence outcome you worked toward

\U0001f50e Searching files for Disciplinas|topics
\U0001f4d6 Reading topic-populator.ts L100-349
⚠ web_fetch: timeout after 20s
Closeout paragraph the worker printed at the end, plain ASCII text.
"""


def test_rendered_lines_pass_through_and_ascii_stays_filtered():
    out = _render_kanban_worker_focus_output(
        RENDERED_LOG, task_id="t_ea06beaf"
    )
    assert "Searching files for Disciplinas|topics" in out
    assert "Reading topic-populator.ts L100-349" in out
    # Error lines keep flowing through the existing warning path.
    assert "web_fetch" in out
    # Prompt/protocol/closeout ASCII lines never reach the bubble.
    assert "Worker Protocol" not in out
    assert "SCOPE" not in out
    assert "Closeout paragraph" not in out


def test_empty_and_dialect_logs_keep_previous_behaviour():
    assert _render_kanban_worker_focus_output("", task_id="t_x") == ""
    dialect = "┊ Tool: terminal\n"
    out = _render_kanban_worker_focus_output(dialect, task_id="t_x")
    assert "terminal" in out
