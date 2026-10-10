"""x-opencode-session rides on every OpenCode request, on every transport."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import build_api_kwargs, handle_max_iterations
from run_agent import AIAgent

_MSGS = [{"role": "user", "content": "hi"}]


def _agent(provider, model, base_url, api_mode=None):
    agent = AIAgent(
        api_key="test-key",
        base_url=base_url,
        model=model,
        provider=provider,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="sess-affinity-1",
    )
    if api_mode:
        agent.api_mode = api_mode
        agent._transport = None
        agent._anthropic_base_url = base_url
    return agent


@pytest.mark.parametrize(
    "provider, model, base_url, api_mode",
    [
        ("opencode-go", "glm-5", "https://opencode.ai/zen/go/v1", None),  # chat_completions
        ("opencode-go", "deepseek-v4.1-flash", "https://opencode.ai/zen/go/v1", None),
        ("opencode-go", "gpt-5.6-luna", "https://opencode.ai/zen/go/v1", None),  # codex_responses
        ("opencode-go", "minimax-m2.7", "https://opencode.ai/zen/go/v1", "anthropic_messages"),
        ("opencode-free", "laguna-s-2.1-free", "https://opencode.ai/zen/v1", None),
        ("custom", "glm-5", "https://opencode.ai/zen/go/v1", None),  # URL-only detection
    ],
)
def test_main_turn_sends_stable_session_header_on_every_transport(provider, model, base_url, api_mode):
    agent = _agent(provider, model, base_url, api_mode)
    first = build_api_kwargs(agent, _MSGS)["extra_headers"]["x-opencode-session"]
    second = build_api_kwargs(agent, _MSGS)["extra_headers"]["x-opencode-session"]
    assert first == second == "sess-affinity-1"

    other = _agent("openrouter", "anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1")
    assert "x-opencode-session" not in (build_api_kwargs(other, _MSGS).get("extra_headers") or {})


def test_auxiliary_calls_share_the_main_turn_session_key():
    token = aux.set_runtime_main(
        "opencode-go", "glm-5", base_url="https://opencode.ai/zen/go/v1", session_id="sess-affinity-1"
    )
    try:
        kwargs = aux._build_call_kwargs("opencode-go", "glm-5", _MSGS, base_url="https://opencode.ai/zen/go/v1")
        assert kwargs["extra_headers"]["x-opencode-session"] == "sess-affinity-1"
        other = aux._build_call_kwargs("openrouter", "x", _MSGS, base_url="https://openrouter.ai/api/v1")
        assert "x-opencode-session" not in (other.get("extra_headers") or {})
    finally:
        aux._RUNTIME_MAIN_CONTEXT.reset(token)


def _iteration_limit_summary(agent, replies):
    """Run the forced summary of the iteration ceiling against a fake provider; return its text and every request sent."""
    sent = []
    answers = iter(replies)

    def send(**request):
        sent.append(request)
        return next(answers)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=send)))
    transport = types.SimpleNamespace(
        build_kwargs=lambda **kwargs: {"model": kwargs["model"], "messages": kwargs["messages"]},
        normalize_response=lambda raw, **_: types.SimpleNamespace(content=raw),
    )
    agent._cached_system_prompt = "SYS"
    with patch.object(agent, "_ensure_primary_openai_client", return_value=client),             patch.object(agent, "_get_transport", return_value=transport),             patch.object(agent, "_anthropic_messages_create", side_effect=lambda request: send(**request)):
        return handle_max_iterations(agent, [{"role": "user", "content": "q"}], 5), sent


@pytest.mark.parametrize(
    "model, api_mode",
    [
        ("deepseek-v4.1-flash", None),  # chat_completions, the route of the workers that ended with no summary
        ("minimax-m2.7", "anthropic_messages"),
    ],
)
def test_iteration_limit_summary_sends_the_session_header_on_the_first_try_and_on_the_retry(model, api_mode):
    agent = _agent("opencode-go", model, "https://opencode.ai/zen/go/v1", api_mode)
    text, sent = _iteration_limit_summary(agent, ["", "SUMMARY"])
    assert text == "SUMMARY" and len(sent) == 2
    assert [request["extra_headers"]["x-opencode-session"] for request in sent] == ["sess-affinity-1"] * 2


def test_iteration_limit_summary_of_another_provider_gets_no_session_header():
    agent = _agent("openrouter", "anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1")
    text, sent = _iteration_limit_summary(agent, ["SUMMARY"])
    assert text == "SUMMARY" and len(sent) == 1
    assert "x-opencode-session" not in (sent[0].get("extra_headers") or {})
