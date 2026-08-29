"""G3 — brief vivo da origem no worker de dispatcher (spec T3, 29/08).

Convergência dos 2 revisores adversariais: o brief é capturado do
parent_agent VIVO no turno delegante (zero state.db), sanitizado
(observed/compaction/system wrappers nunca entram), com a mensagem mais
recente priorizada no orçamento; o card de dispatcher ganha o carimbo de
origem (session_id/project_id) e o contexto do worker aponta a sessão.
"""

from __future__ import annotations

import types

import pytest

import hermes_cli.kanban_db as kb
from tools.delegate_tool import (
    _collect_origin_brief,
    _collect_parent_parity_blocks,
    _PARITY_BRIEF_MAX_CHARS,
)
from tools.session_search_tool import _drop_observed_rows


def _agent(messages):
    return types.SimpleNamespace(messages=messages, _memory_manager=None)


def test_origin_brief_sanitizes_wrappers_and_keeps_latest():
    msgs = [
        {"role": "user", "content": "[Observed Telegram group context - x] fofoca de terceiro"},
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] resumo velho"},
        {"role": "system", "content": "nunca entra"},
        {"role": "user", "content": "corrige o filtro de revenue"},
        {"role": "assistant", "content": "vou corrigir e subir"},
        {"role": "user", "content": "[System note: gateway restarted] ..."},
        {"role": "user", "content": "e valida no alvo depois"},
    ]
    brief = _collect_origin_brief(_agent(msgs))
    assert "fofoca de terceiro" not in brief
    assert "resumo velho" not in brief
    assert "nunca entra" not in brief
    assert "corrige o filtro de revenue" in brief
    assert "e valida no alvo depois" in brief
    # mais recente por último (ordem cronológica preservada)
    assert brief.index("corrige o filtro") < brief.index("valida no alvo")


def test_origin_brief_budget_prefers_recent():
    old = [{"role": "user", "content": f"mensagem antiga {i} " + "x" * 400}
           for i in range(30)]
    msgs = old + [{"role": "user", "content": "A MAIS RECENTE de todas"}]
    brief = _collect_origin_brief(_agent(msgs))
    assert "A MAIS RECENTE de todas" in brief
    assert len(brief) <= 2_400  # cap + header


def test_origin_brief_empty_without_messages():
    assert _collect_origin_brief(_agent([])) == ""
    assert _collect_origin_brief(types.SimpleNamespace()) == ""


def test_parity_brief_summary_cannot_evict_recent_tail():
    """Reviewer defect (delegate_tool.py:1274 old behavior): a long
    compaction summary evicted exactly the freshest turns."""
    huge_summary = "[CONTEXT COMPACTION — REFERENCE ONLY] " + "s" * 12_000
    msgs = [
        {"role": "user", "content": huge_summary},
        {"role": "user", "content": "decisão fresquíssima do operador"},
    ]
    blocks = _collect_parent_parity_blocks(_agent(msgs), goal="g")
    brief = blocks["session_brief"]
    assert "decisão fresquíssima do operador" in brief
    assert len(brief) <= _PARITY_BRIEF_MAX_CHARS + 50


def test_drop_observed_rows_filters():
    rows = [
        {"id": 1, "role": "user", "content": "meu", "observed": 0},
        {"id": 2, "role": "user", "content": "de terceiro", "observed": 1},
        {"id": 3, "role": "assistant", "content": "resposta"},
    ]
    kept = _drop_observed_rows(rows)
    assert [r["id"] for r in kept] == [1, 3]


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    kb.init_db()
    return tmp_path


def test_dispatch_cards_carry_origin_stamp_and_brief(board, monkeypatch):
    import tools.delegation_kanban as dk

    monkeypatch.setattr(
        dk, "_origin_subscription_context",
        lambda: {"session_id": "20260829_ab", "project_id": "proj-x",
                 "platform": "telegram", "chat_id": "-100", "profile": "p"},
    )
    monkeypatch.setattr(dk, "_maybe_subscribe_origin", lambda *a, **k: None, raising=False)
    cards = dk.create_dispatch_cards(
        [{"goal": "corrigir filtro", "context": "contexto explícito"}],
        board=None or "default",
        brief="Brief da conversa de origem (snapshot na criação do card; mais recente por último):\nuser: corrige",
    )
    assert cards
    conn = kb.connect()
    try:
        task = kb.get_task(conn, cards[0])
        assert task.session_id == "20260829_ab"
        assert "contexto explícito" in (task.body or "")
        assert "Brief da conversa de origem" in (task.body or "")
        ctx = kb.build_worker_context(conn, task.id)
        assert "Origin session: 20260829_ab" in ctx
    finally:
        conn.close()


def test_dispatch_cards_explicit_context_never_sacrificed(board, monkeypatch):
    import tools.delegation_kanban as dk

    monkeypatch.setattr(dk, "_origin_subscription_context", lambda: {})
    big_context = "C" * (dk._BODY_MAX - 30)
    cards = dk.create_dispatch_cards(
        [{"goal": "g", "context": big_context}],
        board="default",
        brief="Brief da conversa de origem: não deve entrar",
    )
    conn = kb.connect()
    try:
        task = kb.get_task(conn, cards[0])
        assert "não deve entrar" not in (task.body or "")
        assert (task.body or "").count("C") >= dk._BODY_MAX - 40
    finally:
        conn.close()
