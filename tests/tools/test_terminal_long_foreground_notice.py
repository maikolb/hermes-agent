"""Long-foreground policy notice on terminal results (28/08 DOVCRM).

A principal turn sat ~1h serially blocked on local transcription before
fanning out fronts that never depended on it. SOUL only suggests; the tool
result is what the model always reads — the notice fires there, only in
kanban/board-bound contexts, with an env threshold and kill switch.
"""

from __future__ import annotations

import pytest

from tools.terminal_tool import _long_foreground_notice


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "HERMES_LONG_FOREGROUND_NOTICE_SECONDS",
        "HERMES_KANBAN_TASK",
        "HERMES_PROJECT_BOARD",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)


def test_fires_in_kanban_context_over_threshold(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    notice = _long_foreground_notice(400.0)
    assert notice is not None
    assert "400s" in notice
    assert "NÃO bloqueia a entrega" in notice


def test_silent_under_threshold(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert _long_foreground_notice(299.0) is None


def test_silent_outside_board_context():
    assert _long_foreground_notice(4000.0) is None


def test_threshold_env_and_kill_switch(monkeypatch):
    monkeypatch.setenv("HERMES_PROJECT_BOARD", "dovcrm")
    monkeypatch.setenv("HERMES_LONG_FOREGROUND_NOTICE_SECONDS", "60")
    assert _long_foreground_notice(61.0) is not None
    monkeypatch.setenv("HERMES_LONG_FOREGROUND_NOTICE_SECONDS", "0")
    assert _long_foreground_notice(9999.0) is None
