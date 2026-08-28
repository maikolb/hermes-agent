"""Stdout tool-progress printer for kanban dispatcher workers.

A dispatcher worker's stdout is redirected to the board log
(``<board>/logs/<task>.log``), but in quiet mode nothing reached it until
the final response — so every live surface fed by that file (the
worker-focus bubble, the Vigília activity panel, ``hermes kanban log``)
showed a dead worker for the whole turn (28/08 Wave 4: 80+ minutes of
silence over a healthy run). This printer emits one line per tool event
through THE shared renderer, flushed, so the existing file becomes a live
transcript with the same dialect as every other surface.
"""

from __future__ import annotations

from typing import Any

__all__ = ["make_worker_progress_printer"]


def make_worker_progress_printer():
    """Callback for ``agent.tool_progress_callback`` that prints to stdout."""
    from agent.display import (
        format_tool_error_line,
        format_tool_progress_message,
    )

    state = {"last_terminal": False}

    def _cb(
        event_type: str,
        name: str = "",
        preview: Any = None,
        args: Any = None,
        **_kwargs: Any,
    ) -> None:
        try:
            if event_type == "tool.started":
                msg, is_terminal = format_tool_progress_message(
                    str(name or ""),
                    args if isinstance(args, dict) else None,
                    str(preview) if preview else None,
                    code_blocks=False,
                    last_was_terminal_block=state["last_terminal"],
                )
                state["last_terminal"] = is_terminal
                if msg:
                    print(msg, flush=True)
            elif event_type == "tool.failed":
                line = format_tool_error_line(str(name or ""), preview)
                if line:
                    state["last_terminal"] = False
                    print(line, flush=True)
        except Exception:  # noqa: BLE001 — display must never break the turn
            pass

    return _cb
