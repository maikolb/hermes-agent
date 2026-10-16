"""NFOS_API_CALL_GEN_UPSTREAM_20261016: id da geração e provedor na linha "API call #N".

A linha de log de cada chamada passa a terminar em ``gen=<id> upstream=<provedor>``
quando o OpenRouter devolve os dois dados. O começo da linha não muda, e sem o
dado (outro provedor, Codex, resposta sem os campos) a linha é a de antes.

Os testes passam pelo caminho real: ``run_conversation`` emite a linha; no
streaming, ``_interruptible_streaming_api_call`` monta a resposta a partir de
chunks e cabeçalhos falsos.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion

from run_agent import AIAgent

LOGGER = "agent.conversation_loop"

# O formato de antes, sem nada no fim: é o que os leitores da linha conhecem.
BASELINE = re.compile(
    r"^API call #1: model=\S+ provider=\S+ in=\d+ out=\d+ total=\d+ latency=\d+\.\ds$"
)
LINE_START = re.compile(
    r"^API call #1: model=\S+ provider=\S+ in=\d+ out=\d+ total=\d+ latency=\d+\.\ds"
)


def _tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "web_search tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_agent(*, base_url="https://openrouter.ai/api/v1", provider="openrouter"):
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url=base_url,
            provider=provider,
            model="anthropic/claude-test",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _completion(**extra) -> ChatCompletion:
    """Uma ChatCompletion real; ``provider`` (do OpenRouter) vira campo extra."""
    payload = {
        "id": "gen-1750000000-AbC123",
        "object": "chat.completion",
        "created": 1750000000,
        "model": "anthropic/claude-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
    }
    payload.update(extra)
    return ChatCompletion.model_validate(payload)


def _api_call_records(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("API call #")]


def _run_turn(agent, response, caplog):
    with (
        caplog.at_level(logging.INFO, logger=LOGGER),
        patch.object(agent, "_interruptible_api_call", return_value=response),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("oi")
    assert result["completed"] is True
    return _api_call_records(caplog)


# ── Streaming falso ────────────────────────────────────────────────────────


class _FakeHeaders:
    def __init__(self, values):
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, key, default=None):
        return self._values.get(key.lower(), default)


class _FakeStream:
    """Iterável de chunks com ``.response`` (cabeçalhos HTTP), como o SDK."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.response = SimpleNamespace(
            status_code=200, headers=_FakeHeaders(headers or {})
        )

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


def _chunk(*, content=None, finish_reason=None, usage=None, chunk_id=None, **extra):
    choices = []
    if content is not None or finish_reason is not None:
        delta = SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=None, reasoning=None
        )
        choices = [SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)]
    chunk = SimpleNamespace(choices=choices, model="anthropic/claude-test", usage=usage)
    if chunk_id is not None:
        chunk.id = chunk_id
    for key, value in extra.items():
        setattr(chunk, key, value)
    return chunk


_USAGE = SimpleNamespace(prompt_tokens=120, completion_tokens=7, total_tokens=127)


def _streamed_response(agent, chunks, headers=None):
    stream = _FakeStream(chunks, headers)
    client = MagicMock()
    client.chat.completions.create.side_effect = lambda *a, **kw: stream
    with (
        patch("run_agent.AIAgent._create_request_openai_client", return_value=client),
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        agent._fire_stream_delta = lambda text: None
        return agent._interruptible_streaming_api_call({})


# ── (a) resposta não streaming do OpenRouter com provider ──────────────────


def test_a_non_streaming_openrouter_line_carries_gen_and_upstream(caplog):
    agent = _make_agent()
    response = _completion(provider="Anthropic")

    records = _run_turn(agent, response, caplog)

    assert len(records) == 1
    line = records[0].getMessage()
    assert LINE_START.match(line), line
    assert line.endswith(" gen=gen-1750000000-AbC123 upstream=Anthropic"), line


def test_a_provider_with_spaces_stays_one_key_value_token(caplog):
    agent = _make_agent()
    records = _run_turn(agent, _completion(provider="Google AI Studio"), caplog)

    assert records[0].getMessage().endswith(
        " gen=gen-1750000000-AbC123 upstream=Google_AI_Studio"
    )


@pytest.mark.parametrize("blank_provider", ["", " ", "   "])
def test_a_provider_empty_or_only_spaces_drops_the_upstream_field(caplog, blank_provider):
    # Vazio ou só espaço não é nome de provedor: o campo some (não vira
    # "upstream=_") e o gen= segue.
    agent = _make_agent()
    records = _run_turn(agent, _completion(provider=blank_provider), caplog)

    line = records[0].getMessage()
    assert "upstream=" not in line, line
    assert line.endswith(" gen=gen-1750000000-AbC123"), line


def test_a_provider_with_edge_and_repeated_spaces_is_normalized(caplog):
    agent = _make_agent()
    records = _run_turn(agent, _completion(provider="  Google   AI Studio "), caplog)

    assert records[0].getMessage().endswith(
        " gen=gen-1750000000-AbC123 upstream=Google_AI_Studio"
    )


def test_a_openrouter_base_url_with_another_provider_name_still_gets_the_fields(caplog):
    # O gate vale por provedor "openrouter" OU base URL do OpenRouter: aqui o
    # provedor é "custom" e só a URL identifica o OpenRouter.
    agent = _make_agent(base_url="https://openrouter.ai/api/v1", provider="custom")
    assert agent.provider == "custom"

    records = _run_turn(agent, _completion(provider="DeepInfra"), caplog)

    assert records[0].getMessage().endswith(" gen=gen-1750000000-AbC123 upstream=DeepInfra")


# ── (b) streaming: último chunk e cabeçalhos capturados ────────────────────


def test_b_streaming_uses_last_chunk_id_and_provider(caplog):
    agent = _make_agent()
    response = _streamed_response(
        agent,
        [
            _chunk(content="o", chunk_id="gen-first", provider="Early"),
            _chunk(content="k", finish_reason="stop", chunk_id="gen-9-Zz", provider="Fireworks"),
            # O último chunk traz só o usage, como o OpenRouter envia.
            _chunk(usage=_USAGE, chunk_id="gen-9-Zz", provider="Fireworks"),
        ],
    )
    # O id sintético do Hermes não é geração do provedor.
    assert response.id.startswith("stream-")

    records = _run_turn(agent, response, caplog)

    assert records[0].getMessage().endswith(" gen=gen-9-Zz upstream=Fireworks")


def test_b_streaming_falls_back_to_captured_headers(caplog):
    agent = _make_agent()
    response = _streamed_response(
        agent,
        [
            _chunk(content="ok", finish_reason="stop"),
            _chunk(usage=_USAGE),
        ],
        headers={
            "x-openrouter-id": "gen-hdr-77",
            "x-openrouter-provider": "DeepInfra",
        },
    )

    records = _run_turn(agent, response, caplog)

    line = records[0].getMessage()
    assert LINE_START.match(line), line
    assert line.endswith(" gen=gen-hdr-77 upstream=DeepInfra"), line


# ── (c) sem o dado, a linha é a de hoje ────────────────────────────────────


def test_c_response_without_the_data_keeps_todays_line(caplog):
    agent = _make_agent()
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(role="assistant", content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="anthropic/claude-test",
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=7, total_tokens=127),
    )

    records = _run_turn(agent, response, caplog)

    line = records[0].getMessage()
    assert BASELINE.match(line), line
    assert "gen=" not in line and "upstream=" not in line


def test_c_streaming_without_chunk_or_header_data_keeps_todays_line(caplog):
    agent = _make_agent()
    response = _streamed_response(
        agent,
        [_chunk(content="ok", finish_reason="stop"), _chunk(usage=_USAGE)],
    )

    records = _run_turn(agent, response, caplog)

    assert BASELINE.match(records[0].getMessage()), records[0].getMessage()


def test_c_other_provider_with_an_id_keeps_todays_line(caplog):
    # Codex e outros provedores devolvem um id próprio (resp_..., chatcmpl-...);
    # só o OpenRouter ganha os campos.
    agent = _make_agent(base_url="https://api.example.com/v1", provider="openai-codex")

    records = _run_turn(agent, _completion(id="resp_0123456789", provider="Anthropic"), caplog)

    assert BASELINE.match(records[0].getMessage()), records[0].getMessage()


# ── (d) id malicioso não abre outra linha no log ───────────────────────────


@pytest.mark.parametrize(
    "evil_id",
    [
        "gen-1\nWARNING forged: line injected",
        "gen-1\r\nERROR forged",
        "gen-1 injected=1",
        "gen-1\tx",
    ],
)
def test_d_malicious_id_cannot_inject_a_log_line(caplog, evil_id):
    agent = _make_agent()

    records = _run_turn(agent, _completion(id=evil_id, provider="Anthropic"), caplog)

    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line and "\r" not in line
    assert "forged" not in line and "injected" not in line
    assert "gen=" not in line
    # O provedor, que é válido, segue na linha.
    assert line.endswith(" upstream=Anthropic"), line
    # E nenhum outro registro apareceu com o texto injetado.
    assert not [r for r in caplog.records if "forged" in r.getMessage()]


@pytest.mark.parametrize("evil_provider", ["Anthropic\nWARNING forged", "A\r\nB", "x\ty"])
def test_d_malicious_provider_cannot_inject_a_log_line(caplog, evil_provider):
    agent = _make_agent()

    records = _run_turn(agent, _completion(provider=evil_provider), caplog)

    line = records[0].getMessage()
    assert "\n" not in line and "\r" not in line and "forged" not in line
    assert "upstream=" not in line
    assert line.endswith(" gen=gen-1750000000-AbC123"), line


def test_d_malicious_streamed_header_cannot_inject_a_log_line(caplog):
    agent = _make_agent()
    response = _streamed_response(
        agent,
        [_chunk(content="ok", finish_reason="stop"), _chunk(usage=_USAGE)],
        headers={"x-openrouter-id": "gen-1\nWARNING forged", "x-openrouter-provider": "Ok"},
    )

    records = _run_turn(agent, response, caplog)

    line = records[0].getMessage()
    assert "\n" not in line and "forged" not in line
    assert line.endswith(" upstream=Ok"), line
