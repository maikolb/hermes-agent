"""Retention policy: the compressor never consumes recent human messages.

TARGET_ARCHITECTURE gap 1 — 27/08 DOVCRM incident: batch compaction
archived observed human messages (including unprocessed attachments), so
the next mention saw only the summary and the agent lost what the
operators had just said.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.transcript_retention import (
    is_real_human_message,
    select_retained_middle_humans,
)


def test_real_human_message_detection():
    assert is_real_human_message({"role": "user", "content": "arruma o acesso da vitoria"})
    assert is_real_human_message(
        {"role": "user", "content": "[Maikol|996979567] [audio 'x.ogg' saved at: /tmp/x.ogg]"}
    )
    assert not is_real_human_message(
        {"role": "user", "content": "[System note: The previous turn was interrupted]"}
    )
    assert not is_real_human_message(
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns..."}
    )
    assert not is_real_human_message({"role": "assistant", "content": "oi"})
    assert not is_real_human_message({"role": "user", "content": "   "})


def test_select_rescues_only_middle_members_of_last_k():
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"humano {i}"})
        messages.append({"role": "assistant", "content": f"resposta {i}"})
    # Janela de compressão cobre os índices 10..30 (humanos 5..14).
    rescued = select_retained_middle_humans(messages, 10, 30, keep_last=10)

    indices = [index for index, _ in rescued]
    assert indices == sorted(indices)
    # Últimos 10 humanos: índices 20,22,...,38 → dentro de [10,30): 20..28.
    assert indices == [20, 22, 24, 26, 28]
    assert all(messages[i]["content"].startswith("humano") for i in indices)


def test_select_ignores_synthetic_users_and_respects_zero():
    messages = [
        {"role": "user", "content": "[System note: interrupted]"},
        {"role": "user", "content": "pedido real"},
        {"role": "assistant", "content": "ok"},
    ]
    assert select_retained_middle_humans(messages, 0, 3, keep_last=0) == []
    rescued = select_retained_middle_humans(messages, 0, 3, keep_last=5)
    assert [i for i, _ in rescued] == [1]


def test_constructor_default_is_neutral_and_config_default_protects():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert ContextCompressor(model="t", quiet_mode=True).keep_last_human_messages == 0
    assert DEFAULT_CONFIG["compression"]["keep_last_human_messages"] == 10


def test_compress_keeps_recent_humans_verbatim():
    compressor = ContextCompressor(
        model="test-model",
        quiet_mode=True,
        keep_last_human_messages=6,
    )
    compressor.tail_token_budget = 300
    compressor.protect_last_n = 2
    compressor.threshold_tokens = 1

    filler = "conteudo de trabalho repetitivo " * 30
    messages = [{"role": "system", "content": "system prompt"}]
    human_texts = []
    for i in range(14):
        human = f"pedido humano numero {i}: detalhe importante {i}"
        human_texts.append(human)
        messages.append({"role": "user", "content": human})
        messages.append({"role": "assistant", "content": f"{filler} resposta {i}"})

    with patch.object(
        ContextCompressor,
        "_generate_summary",
        return_value="## Goal\nResumo sintetico do meio.",
    ):
        result = compressor.compress(messages, current_tokens=999999, force=True)

    result_texts = "\n".join(
        str(m.get("content")) for m in result if m.get("role") == "user"
    )
    for human in human_texts[-6:]:
        assert human in result_texts, f"humano recente perdido: {human}"
    assert len(result) < len(messages), "compressao deve reduzir o transcript"
