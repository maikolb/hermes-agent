"""COMPRESSION_LOOP_20261016 (caso 2): o hash do checkpoint tem de atravessar a leitura do SessionDB.

Run 1692 (t_448ed439, sessao 20261016_062028_62f9b3), 16/10 07:52 a 07:54Z: tres compressoes seguidas, todas
"checkpoint commit failed after transcript swap and compensating rollback also failed". O prompt do worker
terminava em "--candidata HEAD.\\n". O dict em memoria guarda o "\\n"; o SessionDB grava o mesmo texto, mas
``_rows_to_conversation`` devolve user/assistant com ``sanitize_context(content).strip()``. ``transcript_hash``
projetava o content cru, entao o transcript lido de volta nunca batia com o preparado (nem o compactado, nem o
original do rollback). Recalculado em producao: as linhas gravadas batem com after_hash/before_hash do checkpoint
pelo valor cru e deixam de bater com a leitura do hermes_state, que so muda as linhas terminadas em "HEAD.\\n".

A projecao do hash ja trata ``name``/``tool_name`` como apelidos porque o hash atravessa essa fronteira; o content
de user/assistant precisa da mesma normalizacao que a leitura aplica.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent.turn_checkpoint import transcript_hash

PROMPT = "work kanban task t_448ed439\n\n## Worker delivery workflow\n... --candidata HEAD.\n"


def _agent(db, session_id: str, compressed):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_in_place = True
    agent.context_compressor.compress = lambda *a, **k: [dict(m) for m in compressed]
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor.compression_count = 1
    return agent


def _fresh_session(db, sid: str) -> list[dict]:
    """A fresh worker session: the in-memory transcript keeps the prompt exactly as the CLI built it."""
    db.create_session(sid, "cli", model="test/model")
    messages = [{"role": "user", "content": PROMPT}]
    for index in range(7):
        role = "assistant" if index % 2 == 0 else "user"
        messages.append({"role": role, "content": f"message {index}"})
    for m in messages:
        db.append_message(sid, m["role"], m["content"])
    return messages


@pytest.mark.parametrize("content", [PROMPT, "  leading and trailing spaces  ", "line\r\n"])
def test_hash_of_a_message_equals_the_hash_of_its_sessiondb_readback(tmp_path, content):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "cli", model="test/model")
    written = [{"role": "user", "content": content}, {"role": "assistant", "content": content}]
    for m in written:
        db.append_message("s", m["role"], m["content"])
    assert transcript_hash(written) == transcript_hash(db.get_messages_as_conversation("s"))


def test_in_place_compaction_commits_when_the_prompt_ends_with_a_newline(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    sid = "checkpoint-trailing-newline"
    messages = _fresh_session(db, sid)
    # Production shape: protect_first_n keeps the prompt, the summary follows, the tail stays.
    compressed = [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": "[CONTEXT COMPACTION] durable summary"},
        {"role": "user", "content": "message 5"},
        {"role": "assistant", "content": "message 6"},
    ]
    agent = _agent(db, sid, compressed)

    # Before the fix: CheckpointWriteError("checkpoint commit failed after transcript swap and compensating
    # rollback also failed"), the production error of run 1692.
    result, _ = compress_context(agent, messages, approx_tokens=100_000, system_message="sys")

    live = db.get_messages_as_conversation(sid)
    state = agent._turn_checkpoint_store.load(sid)
    assert state["compaction"]["state"] == "committed"
    assert state["transcript"]["current_hash"] == transcript_hash(live)
    assert [m["content"] for m in live][1] == "[CONTEXT COMPACTION] durable summary"
    assert transcript_hash(result) == transcript_hash(live)
