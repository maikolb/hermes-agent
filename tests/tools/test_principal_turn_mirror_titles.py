"""Principal-turn mirror titles and traces (operator, 28/08 concursa-ai).

Mirror cards were titled with the raw turn prompt — sender tags, system
notes, forwarded-message timestamps, async-batch markers — and the
closeout trace announced them in the topic verbatim as "Worker concluído:
Principal: [Jhonatan|7550030839] ...". The title must be the distilled
request; the trace must name the lane correctly.
"""

from __future__ import annotations

from gateway.kanban_watchers import _render_worker_trace_content
from tools.principal_turn_mirror import _clean_title_hint


def test_clean_hint_drops_sender_tag():
    raw = "[Jhonatan|7550030839]\nTambém tem que verificar esse edital: [11:54, 27/08/2026] +55 3"
    assert _clean_title_hint(raw) == "Também tem que verificar esse edital"


def test_clean_hint_drops_stacked_metadata_blocks():
    raw = (
        "[System note: The previous turn was interrupted by a gateway shutdown]"
        " [Maikol|996979567] | A lei aparece errado no aplicativo"
    )
    assert _clean_title_hint(raw) == "A lei aparece errado no aplicativo"


def test_clean_hint_async_batch_marker_keeps_meaningful_rest():
    raw = (
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_e8c8ed12]\n"
        "A background fan-out of 2 subagents finished."
    )
    assert _clean_title_hint(raw) == "A background fan-out of 2 subagents finished."


def test_clean_hint_empty_and_caps_length():
    assert _clean_title_hint("") == ""
    assert _clean_title_hint("[só metadata sem fim") == ""
    assert len(_clean_title_hint("x" * 500)) == 90


def test_last_assistant_message_reads_profile_state(tmp_path, monkeypatch):
    """Spec: 'Fim do trabalho principal: closeout AOF com o trabalho feito.'
    The mirror's close pulls the turn's own final assistant message as the
    card result; empty/missing anything falls back to ''."""
    import sqlite3

    from tools import principal_turn_mirror as ptm

    state = tmp_path / "state.db"
    conn = sqlite3.connect(state)
    conn.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL)"
    )
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp) "
        "VALUES (?,?,?,?)",
        [
            ("s1", "user", "pedido", 1.0),
            ("s1", "assistant", "Closeout AOF: entreguei X, evidência Y.", 2.0),
            ("s1", "assistant", "", 3.0),
            ("s2", "assistant", "outra sessão", 4.0),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    assert ptm._last_assistant_message("s1") == (
        "Closeout AOF: entreguei X, evidência Y."
    )
    assert ptm._last_assistant_message("sem-sessao") == ""
    assert ptm._last_assistant_message(None) == ""


def test_trace_names_principal_lane():
    text = _render_worker_trace_content(
        kind="completed",
        title="Principal: Corrigir erro ao abrir Disciplinas",
        board="concursa-ai",
        task_id="t_45b61253",
        run_id=48,
        summary="Turno principal concluído em ~3 min.",
        trace_url_template="",
    )
    assert text.startswith(
        "✅ Turno principal concluído: Corrigir erro ao abrir Disciplinas"
    )
    assert "Worker concluído" not in text


def test_trace_keeps_worker_naming_for_workers():
    text = _render_worker_trace_content(
        kind="completed",
        title="Corrigir upload do edital",
        board="concursa-ai",
        task_id="t_cba35ccf",
        run_id=50,
        summary="closeout do worker",
        trace_url_template="",
    )
    assert text.startswith("✅ Worker concluído: Corrigir upload do edital")
