"""Durable-transcript dedupe on append_message (28/08 replay incident).

Resume/steer replay persisted the same user steer up to 10x and re-wrote
whole blocks of earlier assistant replies on every recovery pass. The
writer now recognises a twin row and returns its id instead of inserting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB

LONG = "resposta longa e substantiva do assistente " * 8  # > 200 chars


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "state.db")
    d.create_session("sess1", source="test")
    return d


def _count(db: SessionDB, session_id: str, role: str) -> int:
    return sum(
        1 for m in db.get_messages(session_id)
        if (m.get("role") if isinstance(m, dict) else getattr(m, "role", ""))
        == role
    )


def test_user_steer_with_platform_id_is_deduped(db):
    first = db.append_message(
        "sess1", role="user", content="pode implementar a correção",
        platform_message_id="tg-123",
    )
    for _ in range(9):
        dup = db.append_message(
            "sess1", role="user", content="pode implementar a correção",
            platform_message_id="tg-123",
        )
        assert dup == first
    assert _count(db, "sess1", "user") == 1


def test_observed_and_real_steer_both_persist(db):
    observed = db.append_message(
        "sess1", role="user", content="msg do grupo",
        platform_message_id="tg-9", observed=True,
    )
    steer = db.append_message(
        "sess1", role="user", content="msg do grupo",
        platform_message_id="tg-9", observed=False,
    )
    assert observed != steer


def test_long_assistant_replay_is_deduped(db):
    first = db.append_message("sess1", role="assistant", content=LONG)
    dup = db.append_message("sess1", role="assistant", content=LONG)
    assert dup == first
    assert _count(db, "sess1", "assistant") == 1


def test_short_assistant_repeat_is_legitimate(db):
    a = db.append_message("sess1", role="assistant", content="Ok.")
    b = db.append_message("sess1", role="assistant", content="Ok.")
    assert a != b


def test_short_system_note_burst_collapses_within_window(db):
    note = "[System note: gateway shutdown]"
    a = db.append_message("sess1", role="user", content=note)
    b = db.append_message("sess1", role="user", content=note)
    assert b == a
    # A later, distinct event (outside the 10-minute window) persists.
    c = db.append_message(
        "sess1", role="user", content=note,
        timestamp=__import__("time").time() + 3600,
    )
    assert c != a


def test_tool_and_summary_rows_never_deduped(db):
    a = db.append_message(
        "sess1", role="tool", content="x" * 300, tool_name="terminal",
        tool_call_id="c1",
    )
    b = db.append_message(
        "sess1", role="tool", content="x" * 300, tool_name="terminal",
        tool_call_id="c1",
    )
    assert a != b


def test_kill_switch(db, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_DEDUPE", "off")
    a = db.append_message("sess1", role="assistant", content=LONG)
    b = db.append_message("sess1", role="assistant", content=LONG)
    assert a != b
