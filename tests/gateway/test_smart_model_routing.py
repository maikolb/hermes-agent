from types import SimpleNamespace

import pytest

from agent.smart_model_routing import (
    classify_task,
    resolve_claude_delegation_route,
    resolve_turn_route,
)
from gateway.run import GatewayRunner


TEST_BENCHMARK_HASH = "a" * 64
CFG = {
    "enabled": True,
    "platforms": ["telegram"],
    "quality_policy": "benchmarked",
    "benchmark_policy_sha256": TEST_BENCHMARK_HASH,
}
PRIMARY = {
    "model": "gpt-5.6-sol",
    "runtime": {
        "provider": "openai-codex",
        "api_key": "test-only",
        "base_url": "https://example.invalid",
        "api_mode": "codex_responses",
        "credential_pool": None,
    },
}
CLAUDE_CFG = {
    "enabled": True,
    "platforms": ["telegram"],
    "quality_policy": "benchmarked",
    "benchmark_policy_sha256": TEST_BENCHMARK_HASH,
    "claude_models": {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-5",
        "opus": "claude-opus-4-8",
        "fable": "claude-fable-5",
    },
}
CLAUDE_PRIMARY = {
    "model": "claude-fable-5",
    "runtime": {
        "provider": "anthropic",
        "api_key": "test-only",
        "base_url": "https://example.invalid",
        "api_mode": "anthropic_messages",
        "credential_pool": None,
    },
}


@pytest.fixture(autouse=True)
def approve_only_the_synthetic_test_benchmark(monkeypatch):
    monkeypatch.setattr(
        "agent.smart_model_routing._APPROVED_BENCHMARK_POLICY_SHA256",
        frozenset({TEST_BENCHMARK_HASH}),
    )


@pytest.mark.parametrize(
    ("prompt", "tier", "effort"),
    [
        ("Oi, qual é o status?", "luna", "low"),
        ("Explique este conceito de forma curta para a equipe.", "luna", "low"),
        ("Corrija o bug em app.py e rode os testes.", "terra", "medium"),
        (
            "Revise a arquitetura deste módulo e este traceback: ```ValueError```; "
            "proponha duas alternativas.",
            "terra",
            "high",
        ),
        (
            "Redesenhe a arquitetura e coordene múltiplos serviços e equipes com "
            "um plano de integração.",
            "sol",
            "high",
        ),
        (
            "Migre a autenticação de produção, altere permissões e valide rollback.",
            "sol",
            "xhigh",
        ),
        (
            "Delete production data and rotate credentials after validating rollback.",
            "sol",
            "xhigh",
        ),
    ],
)
def test_structural_routing_matrix_ptbr_and_english(prompt, tier, effort):
    decision = classify_task(prompt)
    assert (decision.tier, decision.effort) == (tier, effort)


def test_auto_routing_never_selects_max_or_ultra():
    prompts = [
        "Oi",
        "Corrija o bug em api.py e teste.",
        "Audite toda a arquitetura ponta a ponta, produção, auth e dados; não pare.",
    ]
    assert {classify_task(prompt).effort for prompt in prompts} <= {
        "low", "medium", "high", "xhigh"
    }


@pytest.mark.parametrize(
    ("primary", "expected_model"),
    [
        (PRIMARY, "gpt-5.6-sol"),
        (CLAUDE_PRIMARY, "claude-fable-5"),
    ],
)
def test_missing_benchmark_policy_fails_closed_to_strongest_lane(primary, expected_model):
    route = resolve_turn_route(
        "Oi, qual é o status?",
        {"enabled": True, "platforms": ["telegram"]},
        primary,
        reasoning_config={"enabled": True, "effort": "low"},
        context={"platform": "telegram"},
    )
    assert route["model"] == expected_model
    assert route["reasoning_config"]["effort"] == "xhigh"
    assert route["decision"]["source"] == "quality_fail_safe"
    assert "quality_benchmark_required" in route["decision"]["reasons"]


def test_benchmarked_label_without_revision_bound_hash_still_fails_closed():
    route = resolve_turn_route(
        "Oi",
        {"enabled": True, "quality_policy": "benchmarked", "platforms": ["telegram"]},
        PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == "gpt-5.6-sol"
    assert route["reasoning_config"]["effort"] == "xhigh"
    assert route["decision"]["source"] == "quality_fail_safe"


@pytest.mark.parametrize(
    ("primary", "expected_model"),
    [
        (PRIMARY, "gpt-5.6-sol"),
        (CLAUDE_PRIMARY, "claude-fable-5"),
    ],
)
def test_conservative_high_risk_route_uses_max(primary, expected_model):
    route = resolve_turn_route(
        "Migre a autenticação de produção, altere permissões e valide rollback.",
        {"enabled": True, "platforms": ["telegram"], "quality_policy": "conservative"},
        primary,
        reasoning_config={"enabled": True, "effort": "low"},
        context={"platform": "telegram"},
    )
    assert route["model"] == expected_model
    assert route["reasoning_config"]["effort"] == "max"
    assert route["decision"]["source"] == "quality_fail_safe"


def test_claude_delegation_high_risk_uses_fable_max_until_benchmarked():
    route = resolve_claude_delegation_route(
        "Migre a autenticação de produção, altere permissões e valide rollback."
    )
    assert (route["model"], route["effort"]) == ("fable", "max")


def test_resolver_keeps_provider_credentials_and_emits_prompt_free_telemetry():
    route = resolve_turn_route(
        "Corrija o bug em app.py e rode os testes.",
        CFG,
        PRIMARY,
        reasoning_config={"enabled": True, "effort": "xhigh"},
        context={"platform": "telegram"},
    )
    assert route["model"] == "gpt-5.6-terra"
    assert route["reasoning_config"]["effort"] == "medium"
    assert route["runtime"] == PRIMARY["runtime"]
    assert route["cache_model"] == PRIMARY["model"]
    assert "prompt" not in route["decision"]
    assert "message" not in route["decision"]


def test_session_model_override_disables_auto_route():
    runner = SimpleNamespace(
        _service_tier=None,
        config=None,
        _session_model_overrides={"session": {"model": "gpt-5.6-sol"}},
        _session_reasoning_overrides={},
    )
    route = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Oi",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        user_config={"smart_model_routing": CFG},
        session_key="session",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    assert route["model"] == "gpt-5.6-sol"
    assert route["reasoning_config"]["effort"] == "high"
    assert route["decision"]["source"] == "session_override"


def test_session_reasoning_override_wins_without_disabling_model_route():
    runner = SimpleNamespace(
        _service_tier=None,
        config=None,
        _session_model_overrides={},
        _session_reasoning_overrides={"session": {"enabled": True, "effort": "xhigh"}},
    )
    route = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Oi",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        user_config={"smart_model_routing": CFG},
        session_key="session",
        reasoning_config={"enabled": True, "effort": "xhigh"},
    )
    assert route["model"] == "gpt-5.6-luna"
    assert route["reasoning_config"]["effort"] == "xhigh"
    assert "explicit_reasoning_override" in route["decision"]["reasons"]


def test_explicit_user_model_and_ultra_effort_are_preserved():
    route = resolve_turn_route(
        "Use gpt-5.6-sol em modo ultra para esta tarefa.",
        CFG,
        PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == "gpt-5.6-sol"
    assert route["reasoning_config"]["effort"] == "ultra"
    assert route["decision"]["source"] == "user_override"


def test_classifier_exception_preserves_primary_route(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr("agent.smart_model_routing.classify_task", fail)
    route = resolve_turn_route(
        "Faça isso",
        CFG,
        PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == "gpt-5.6-sol"
    assert route["reasoning_config"] is None
    assert route["decision"]["source"] == "fail_safe"


def test_isolated_risk_keyword_does_not_force_sol():
    decision = classify_task("deploy")
    assert decision.tier == "terra"
    assert decision.effort == "medium"


def test_two_architecture_keywords_do_not_force_sol_without_structure():
    decision = classify_task("arquitetura equipes")
    assert decision.tier != "sol"


def test_short_continuation_inherits_only_same_session_auto_route():
    runner = SimpleNamespace(
        _service_tier=None,
        config=None,
        _session_model_overrides={},
        _session_reasoning_overrides={},
        _agent_cache_lock=None,
    )
    kwargs = {
        "user_config": {"smart_model_routing": CFG},
        "reasoning_config": {"enabled": True, "effort": "high"},
    }
    first = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Redesenhe a arquitetura e coordene múltiplas equipes e serviços com um plano.",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        session_key="topic-a",
        routing_context={"has_history": False},
        **kwargs,
    )
    continued = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Continue o trabalho.",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        session_key="topic-a",
        routing_context={"has_history": True},
        **kwargs,
    )
    other_topic = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Continue o trabalho.",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        session_key="topic-b",
        routing_context={"has_history": True},
        **kwargs,
    )
    new_simple_task = GatewayRunner._resolve_turn_agent_config(
        runner,
        "Nova tarefa: qual é o status?",
        "gpt-5.6-sol",
        PRIMARY["runtime"],
        session_key="topic-a",
        routing_context={"has_history": True},
        **kwargs,
    )

    assert (first["decision"]["tier"], first["decision"]["reasoning_effort"]) == (
        "sol", "high"
    )
    assert continued["decision"]["source"] == "auto_continuation"
    assert continued["model"] == first["model"]
    assert continued["reasoning_config"]["effort"] == "high"
    assert other_topic["model"] == "gpt-5.6-luna"
    assert new_simple_task["model"] == "gpt-5.6-luna"


def test_disabled_incompatible_and_nontelegram_routes_preserve_baseline():
    disabled = resolve_turn_route("Oi", {"enabled": False}, PRIMARY)
    incompatible = resolve_turn_route(
        "Oi", CFG,
        {"model": "gemini-pro", "runtime": {"provider": "google"}},
        context={"platform": "telegram"},
    )
    other_platform = resolve_turn_route(
        "Oi", CFG, PRIMARY, context={"platform": "discord"}
    )
    assert disabled["model"] == "gpt-5.6-sol"
    assert incompatible["model"] == "gemini-pro"
    assert other_platform["model"] == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("prompt", "model", "effort"),
    [
        ("Oi, qual é o status?", "claude-haiku-4-5", "low"),
        ("Explique este conceito para a equipe com dois exemplos.", "claude-haiku-4-5", "low"),
        (
            "Explique de forma executiva como este fluxo funciona, incluindo contexto, "
            "duas analogias, limitações, um exemplo concreto e a decisão recomendada para "
            "que a equipe consiga apresentar o tema sem recorrer a termos técnicos.",
            "claude-sonnet-5",
            "medium",
        ),
        ("Corrija o bug em app.py e rode os testes.", "claude-sonnet-5", "medium"),
        (
            "Revise a arquitetura deste módulo e este traceback: ```ValueError```; "
            "proponha duas alternativas.",
            "claude-opus-4-8",
            "high",
        ),
        (
            "Redesenhe a arquitetura e coordene múltiplos serviços e equipes com "
            "um plano de integração.",
            "claude-opus-4-8",
            "high",
        ),
        (
            "Migre a autenticação de produção, altere permissões e valide rollback.",
            "claude-fable-5",
            "xhigh",
        ),
    ],
)
def test_claude_provider_routes_by_complexity(prompt, model, effort):
    route = resolve_turn_route(
        prompt,
        CLAUDE_CFG,
        CLAUDE_PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == model
    assert route["reasoning_config"]["effort"] == effort
    assert route["runtime"] == CLAUDE_PRIMARY["runtime"]
    assert route["cache_model"] == model


def test_claude_explicit_model_and_effort_win():
    route = resolve_turn_route(
        "Use Claude Opus com effort max para revisar isto.",
        CLAUDE_CFG,
        CLAUDE_PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == "claude-opus-4-8"
    assert route["reasoning_config"]["effort"] == "max"
    assert route["decision"]["source"] == "user_override"

    max_only = resolve_turn_route(
        "Use esforço max para esta auditoria.",
        CLAUDE_CFG,
        CLAUDE_PRIMARY,
        context={"platform": "telegram"},
    )
    assert max_only["model"] == "claude-fable-5"
    assert max_only["reasoning_config"]["effort"] == "max"


def test_claude_openrouter_prefix_is_preserved():
    route = resolve_turn_route(
        "Oi",
        CLAUDE_CFG,
        {
            "model": "anthropic/claude-fable-5",
            "runtime": {"provider": "openrouter", "api_key": "test-only"},
        },
        context={"platform": "telegram"},
    )
    assert route["model"] == "anthropic/claude-haiku-4-5"


def test_claude_delegation_router_is_prompt_free_and_uses_cli_aliases():
    simple = resolve_claude_delegation_route("Oi, qual é o status?")
    risky = resolve_claude_delegation_route(
        "Migre a autenticação de produção, altere permissões e valide rollback."
    )
    explicit = resolve_claude_delegation_route(
        "Use Sonnet em modo high para revisar esta mudança."
    )
    assert (simple["model"], simple["effort"]) == ("fable", "xhigh")
    assert (risky["model"], risky["effort"]) == ("fable", "max")
    assert (explicit["model"], explicit["effort"]) == ("sonnet", "high")
    assert "prompt" not in simple
    assert "message" not in simple


def test_claude_classifier_failure_preserves_primary_route(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr("agent.smart_model_routing.classify_task", fail)
    route = resolve_turn_route(
        "Faça isso",
        CLAUDE_CFG,
        CLAUDE_PRIMARY,
        context={"platform": "telegram"},
    )
    assert route["model"] == "claude-fable-5"
    assert route["reasoning_config"] is None
    assert route["decision"]["source"] == "fail_safe"
