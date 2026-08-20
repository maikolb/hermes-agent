"""Quality-gated, prompt-free routing for GPT-5.6 and Claude families.

The classifier is deliberately local and bounded: it never calls a model,
network service, or reads conversation content outside the current request.
Lexical hints are only one input; routing also uses structural task signals
(size, steps, artifacts, code, risk, coordination, and expected work).  A
classification is never sufficient to authorize a lower-capability route:
until a revision-bound benchmark policy is present, automatic routing fails
closed to the strongest configured lane.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_OPENAI_MODEL_RE = re.compile(
    r"^(?P<prefix>openai/)?gpt-5\.6-(?P<tier>luna|terra|sol)(?:-pro)?$",
    re.IGNORECASE,
)
_CLAUDE_MODEL_RE = re.compile(
    r"^(?P<prefix>anthropic/)?claude-(?P<tier>haiku|sonnet|opus|fable)"
    r"(?:[-.][a-z0-9.:-]+)*$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_CODE_RE = re.compile(r"```|`[^`]+`|\b(?:traceback|stack\s*trace|exception)\b", re.IGNORECASE)
_STEP_RE = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+")
_ARTIFACT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?:^|\s)[./][\w./\\-]+|\b[\w.-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|md|sql|ps1|sh|toml|pdf|docx)\b)",
    re.IGNORECASE,
)

_MUTATION_RE = re.compile(
    r"\b(?:crie|criar|create|implemente|implement|corrija|corrigir|fix|edite|editar|edit|"
    r"altere|alterar|change|migre|migrar|migrate|delete|apague|remova|remove|deploy|"
    r"publique|publish|release|envie|send|execute|rode|run|instale|install)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(?:pesquise|pesquisar|research|browse|procure|buscar|compare|comparar|benchmark|"
    r"fontes?|sources?|atual|latest|recent)\b",
    re.IGNORECASE,
)
_DIAGNOSE_RE = re.compile(
    r"\b(?:analise|analisar|analyze|investigue|investigar|investigate|diagnostique|"
    r"diagnose|debug|revise|revisar|review|audite|auditar|audit|aut[oó]psia)\b",
    re.IGNORECASE,
)
_ARCHITECTURE_RE = re.compile(
    r"\b(?:arquitetura|architecture|design\s+system|sistema|system|integra(?:ção|cao)|"
    r"integration|schema|migra(?:ção|cao)|migration|refactor|reestruture|redesign)\b",
    re.IGNORECASE,
)
_COORDINATION_RE = re.compile(
    r"\b(?:equipes?|teams?|agentes?|agents?|subagents?|projetos?|projects?|servi(?:ço|co)s?|"
    r"services?|reposit[oó]rios?|repositories|m[uú]ltipl[oa]s?|parallel|coordene|orquestre)\b",
    re.IGNORECASE,
)
_RISK_DOMAIN_RE = re.compile(
    r"\b(?:produ(?:ção|cao)|production|deploy|release|migra(?:ção|cao)|migration|auth|oauth|"
    r"credencia(?:l|is)|credentials?|secrets?|tokens?|security|seguran(?:ça|ca)|permiss(?:ão|ao)|"
    r"permissions?|billing|pagamento|financeiro|financial|legal|contrato|contract|dados?|data)\b",
    re.IGNORECASE,
)
_DESTRUCTIVE_RE = re.compile(
    r"\b(?:delete|drop|truncate|wipe|reset\s+--hard|force\s+push|apague|exclua|remova|"
    r"sobrescreva|overwrite|revogue|revoke|rotate\s+(?:key|token)|gire\s+(?:chave|token))\b",
    re.IGNORECASE,
)
_LONG_HORIZON_RE = re.compile(
    r"\b(?:end[- ]to[- ]end|ponta\s+a\s+ponta|todos?\s+os\s+arquivos|whole\s+(?:repo|project)|"
    r"do\s+not\s+stop|n[aã]o\s+pare|at[eé]\s+(?:terminar|ficar\s+pronto)|completamente|"
    r"comprehensive|profundamente|deeply)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_RE = re.compile(
    r"\b(?:isso|isto|aquilo|it|that|arrume|conserte|fa(?:ça|ca)|resolve|fix\s+it)\b",
    re.IGNORECASE,
)
_EXPLICIT_OPENAI_MODEL_RE = re.compile(
    r"\b(?:use|usar|usa|rode|rodar|execute|executar|modelo|model)\b.{0,40}?"
    r"\b(?:openai/)?gpt-5\.6-(luna|terra|sol)(?:-pro)?\b",
    re.IGNORECASE,
)
_EXPLICIT_CLAUDE_MODEL_RE = re.compile(
    r"\b(?:use|usar|usa|rode|rodar|execute|executar|modelo|model)\b.{0,40}?"
    r"\b(?:claude[- ]?)?(haiku|sonnet|opus|fable)(?:[- .]?\d[\w.:-]*)?\b",
    re.IGNORECASE,
)
_EXPLICIT_EFFORT_RE = re.compile(
    r"\b(?:reasoning|effort|esfor(?:ço|co)|modo|mode)\s*(?:=|:|-)?\s*"
    r"(low|medium|high|xhigh|max|ultra)\b",
    re.IGNORECASE,
)
_CONTINUATION_RE = re.compile(
    r"^\s*(?:continue|continue\s+o\s+trabalho|continue\s+da[ií]|prossiga|siga|segue|"
    r"pode\s+seguir|fa(?:ça|ca)\s+isso|retome|keep\s+going|go\s+on|proceed|resume)\b",
    re.IGNORECASE,
)

_AUTO_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_OPENAI_PROVIDERS = frozenset({"openai", "openai-api", "openai-codex", "openrouter"})
_CLAUDE_PROVIDERS = frozenset({"anthropic", "claude", "claude-code", "openrouter"})
_CLAUDE_DEFAULT_MODELS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
}
_CLAUDE_CLI_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable"})
# Empty by design after the 2026-08-18 quality audit.  A lower route becomes
# possible only through a source revision that pins the exact consolidated
# benchmark artifact after deterministic and blind gates pass.  A config label
# alone can never authorize a downgrade.
_APPROVED_BENCHMARK_POLICY_SHA256: frozenset[str] = frozenset()
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_DECISION_RANK = {
    ("luna", "low"): 0,
    ("luna", "medium"): 1,
    ("terra", "medium"): 2,
    ("terra", "high"): 3,
    ("sol", "high"): 4,
    ("sol", "xhigh"): 5,
}


@dataclass(frozen=True)
class RoutingDecision:
    tier: str
    effort: str
    score: int
    risk: str
    action: str
    reasons: tuple[str, ...]
    source: str = "auto"

    def telemetry(self, *, model: str) -> dict[str, Any]:
        """Return bounded telemetry. The user prompt is intentionally absent."""
        return {
            "tier": self.tier,
            "model": model,
            "reasoning_effort": self.effort,
            "score": self.score,
            "risk": self.risk,
            "action": self.action,
            "reasons": list(self.reasons),
            "source": self.source,
        }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _reasoning_effort(config: Mapping[str, Any] | None) -> str:
    if not isinstance(config, Mapping):
        return ""
    return str(config.get("effort") or "").strip().lower()


def _lower_routes_are_approved(config: Mapping[str, Any]) -> bool:
    if str(config.get("quality_policy") or "").strip().lower() != "benchmarked":
        return False
    evidence_hash = str(config.get("benchmark_policy_sha256") or "").strip().lower()
    return bool(
        _SHA256_RE.fullmatch(evidence_hash)
        and evidence_hash in _APPROVED_BENCHMARK_POLICY_SHA256
    )


def _append(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def classify_task(
    user_message: str,
    context: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    """Classify one task using bounded structural and semantic signals."""
    text = str(user_message or "").strip()[:32_000]
    ctx = context if isinstance(context, Mapping) else {}
    words = re.findall(r"\b\w+\b", text, flags=re.UNICODE)
    word_count = len(words)
    line_count = text.count("\n") + (1 if text else 0)
    step_count = len(_STEP_RE.findall(text))
    artifact_count = len(_ARTIFACT_RE.findall(text)) + len(_URL_RE.findall(text))
    try:
        attachment_count = max(0, min(8, int(ctx.get("attachment_count") or 0)))
        expected_steps = max(0, min(12, int(ctx.get("expected_steps") or 0)))
        expected_artifacts = max(0, min(12, int(ctx.get("expected_artifacts") or 0)))
    except (TypeError, ValueError):
        attachment_count = expected_steps = expected_artifacts = 0

    mutation = bool(_MUTATION_RE.search(text))
    research = bool(_RESEARCH_RE.search(text))
    diagnose = bool(_DIAGNOSE_RE.search(text))
    architecture = bool(_ARCHITECTURE_RE.search(text))
    coordination = bool(_COORDINATION_RE.search(text))
    risk_matches = _RISK_DOMAIN_RE.findall(text)
    risk_domain = bool(risk_matches)
    destructive = bool(_DESTRUCTIVE_RE.search(text))
    long_horizon = bool(_LONG_HORIZON_RE.search(text)) or step_count >= 6 or word_count >= 600
    ambiguous = (
        bool(_AMBIGUOUS_RE.search(text))
        and word_count <= 10
        and artifact_count == 0
        and attachment_count == 0
    )

    reasons: list[str] = []
    score = 0
    action = "answer"

    if word_count > 45:
        score += 1
        _append(reasons, "context_medium")
    if word_count > 180:
        score += 2
        _append(reasons, "context_large")
    if line_count >= 4:
        score += 1
        _append(reasons, "multiline")
    if step_count + expected_steps >= 2:
        score += 2
        _append(reasons, "multi_step")
    if step_count + expected_steps >= 5:
        score += 2
        _append(reasons, "many_steps")
    if artifact_count + expected_artifacts + attachment_count:
        score += 1
        _append(reasons, "artifacts")
    if artifact_count + expected_artifacts + attachment_count >= 4:
        score += 2
        _append(reasons, "many_artifacts")
    if _CODE_RE.search(text):
        score += 2
        action = "code_or_debug"
        _append(reasons, "code_or_trace")
    if mutation:
        score += 2
        action = "change"
        _append(reasons, "state_change")
    if diagnose:
        score += 1
        action = "diagnose"
        _append(reasons, "diagnosis")
    if research:
        score += 2
        action = "research"
        _append(reasons, "research")
    if architecture:
        score += 3
        action = "architecture"
        _append(reasons, "architecture")
    if coordination:
        score += 2
        action = "coordination" if action == "answer" else action
        _append(reasons, "coordination")
    if ambiguous:
        score += 2
        _append(reasons, "strong_ambiguity")
    if long_horizon:
        score += 3
        _append(reasons, "long_horizon")

    concrete_operation = bool(
        artifact_count
        or attachment_count
        or step_count
        or expected_steps
        or word_count >= 5
        or len(risk_matches) >= 2
        or architecture
        or coordination
        or diagnose
    )
    if concrete_operation and (destructive or (risk_domain and mutation)):
        risk = "high"
        score += 3
        _append(reasons, "high_risk")
    elif risk_domain:
        risk = "moderate"
        score += 1
        _append(reasons, "risk_domain")
    else:
        risk = "low"

    if risk == "high" or (long_horizon and score >= 10):
        return RoutingDecision("sol", "xhigh", score, risk, action, tuple(reasons))
    if score >= 10 or (
        architecture
        and coordination
        and (word_count >= 8 or artifact_count or step_count or long_horizon)
    ):
        return RoutingDecision("sol", "high", score, risk, action, tuple(reasons))
    if score >= 6:
        return RoutingDecision("terra", "high", score, risk, action, tuple(reasons))
    if score >= 3 or mutation or research or diagnose:
        return RoutingDecision("terra", "medium", score, risk, action, tuple(reasons))
    if word_count <= 32 and line_count <= 2 and not artifact_count and not attachment_count:
        return RoutingDecision("luna", "low", score, risk, action, tuple(reasons or ["short_simple"]))
    return RoutingDecision("luna", "medium", score, risk, action, tuple(reasons or ["bounded_explanation"]))


def _primary_route(primary: Mapping[str, Any], reasoning_config: Mapping[str, Any] | None) -> dict[str, Any]:
    runtime = dict(primary.get("runtime") or {})
    model = str(primary.get("model") or "")
    return {
        "model": model,
        "cache_model": model,
        "runtime": runtime,
        "reasoning_config": dict(reasoning_config) if isinstance(reasoning_config, Mapping) else reasoning_config,
        "signature": (
            model,
            runtime.get("provider"),
            runtime.get("requested_provider"),
            runtime.get("base_url"),
            runtime.get("api_mode"),
            runtime.get("command"),
            tuple(runtime.get("args") or ()),
        ),
    }


def _model_family(primary_model: str, provider: str) -> tuple[str | None, re.Match[str] | None]:
    openai_match = _OPENAI_MODEL_RE.fullmatch(primary_model)
    if openai_match and provider in _OPENAI_PROVIDERS:
        return "openai", openai_match
    claude_match = _CLAUDE_MODEL_RE.fullmatch(primary_model)
    if claude_match and provider in _CLAUDE_PROVIDERS:
        return "claude", claude_match
    return None, None


def _claude_model_key(decision: RoutingDecision) -> str:
    """Map structural complexity to a Claude capability tier.

    Haiku owns short/simple turns, Sonnet owns bounded and ordinary work,
    Opus owns complex multi-step/architecture work, and Fable is reserved for
    concrete high-risk or long-horizon xhigh work.
    """
    if decision.effort in {"xhigh", "max", "ultra"}:
        return "fable"
    if decision.effort == "high":
        return "opus"
    if decision.tier == "luna" and decision.effort == "low":
        return "haiku"
    return "sonnet"


def _target_model(
    primary_model: str,
    decision: RoutingDecision,
    config: Mapping[str, Any],
    family: str,
    explicit_model_key: str | None = None,
) -> str:
    if family == "openai":
        tier = explicit_model_key if explicit_model_key in {"luna", "terra", "sol"} else decision.tier
        models = config.get("models")
        configured = models.get(tier) if isinstance(models, Mapping) else None
        candidate = str(configured or "").strip()
        matched = _OPENAI_MODEL_RE.fullmatch(candidate) if candidate else None
        if matched and matched.group("tier").lower() == tier:
            return candidate
        prefix = "openai/" if primary_model.lower().startswith("openai/") else ""
        return f"{prefix}gpt-5.6-{tier}"

    model_key = (
        explicit_model_key
        if explicit_model_key in _CLAUDE_CLI_ALIASES
        else _claude_model_key(decision)
    )
    models = config.get("claude_models")
    configured = models.get(model_key) if isinstance(models, Mapping) else None
    candidate = str(configured or "").strip()
    matched = _CLAUDE_MODEL_RE.fullmatch(candidate) if candidate else None
    if matched and matched.group("tier").lower() == model_key:
        wants_prefix = primary_model.lower().startswith("anthropic/")
        has_prefix = candidate.lower().startswith("anthropic/")
        if wants_prefix and not has_prefix:
            return "anthropic/" + candidate
        if not wants_prefix and has_prefix:
            return candidate.split("/", 1)[1]
        return candidate
    prefix = "anthropic/" if primary_model.lower().startswith("anthropic/") else ""
    return prefix + _CLAUDE_DEFAULT_MODELS[model_key]


def _explicit_user_choice(text: str) -> tuple[str | None, str | None]:
    openai_match = _EXPLICIT_OPENAI_MODEL_RE.search(text or "")
    claude_match = _EXPLICIT_CLAUDE_MODEL_RE.search(text or "")
    effort_match = _EXPLICIT_EFFORT_RE.search(text or "")
    return (
        (
            openai_match.group(1).lower()
            if openai_match
            else claude_match.group(1).lower() if claude_match else None
        ),
        effort_match.group(1).lower() if effort_match else None,
    )


def resolve_claude_delegation_route(
    user_message: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a Claude Code CLI model alias without invoking Claude.

    This is the deterministic bridge used by Hermes before a real Claude Code
    delegation. It deliberately returns bounded telemetry and never returns
    the request text.
    """
    explicit_model, explicit_effort = _explicit_user_choice(user_message)
    decision = classify_task(user_message, context)
    if explicit_model in _CLAUDE_CLI_ALIASES:
        model_key = explicit_model
        source = "user_override"
        reasons = tuple((*decision.reasons, "explicit_user_model"))
    else:
        # Delegations do not receive a profile config/benchmark binding.  They
        # therefore fail closed to the strongest known Claude lane.  A future
        # benchmark-bound caller may use resolve_turn_route with an explicit
        # quality policy instead of weakening this standalone bridge.
        model_key = "fable"
        source = "quality_fail_safe"
        reasons = tuple((*decision.reasons, "quality_benchmark_required"))
    fail_safe_effort = (
        "max" if decision.risk == "high" or "long_horizon" in decision.reasons else "xhigh"
    )
    effort = explicit_effort or (decision.effort if explicit_model else fail_safe_effort)
    if not explicit_effort and source != "quality_fail_safe" and effort not in _AUTO_EFFORTS:
        effort = "xhigh"
    return {
        "provider": "claude-code",
        "model": model_key,
        "effort": effort,
        "tier": decision.tier,
        "score": decision.score,
        "risk": decision.risk,
        "action": decision.action,
        "reasons": list(reasons),
        "source": source,
    }


def _inherit_continuation_decision(
    user_message: str,
    current: RoutingDecision,
    previous: Any,
) -> RoutingDecision:
    """Apply per-session hysteresis only to a short anaphoric continuation."""
    if not isinstance(previous, Mapping):
        return current
    text = str(user_message or "").strip()
    if len(text.split()) > 14 or not _CONTINUATION_RE.search(text):
        return current
    tier = str(previous.get("tier") or "").lower()
    effort = str(previous.get("reasoning_effort") or "").lower()
    if (tier, effort) not in _DECISION_RANK:
        return current
    if _DECISION_RANK[(tier, effort)] < _DECISION_RANK.get(
        (current.tier, current.effort), -1
    ):
        return current
    prior_reasons = previous.get("reasons")
    bounded_reasons = [
        str(item) for item in prior_reasons
        if isinstance(item, str)
    ][:12] if isinstance(prior_reasons, list) else []
    _append(bounded_reasons, "session_continuation")
    return RoutingDecision(
        tier=tier,
        effort=effort,
        score=max(current.score, int(previous.get("score") or 0)),
        risk=str(previous.get("risk") or current.risk),
        action=str(previous.get("action") or current.action),
        reasons=tuple(bounded_reasons),
        source="auto_continuation",
    )


def resolve_turn_route(
    user_message: str,
    routing_config: Mapping[str, Any] | None,
    primary: Mapping[str, Any],
    *,
    reasoning_config: Mapping[str, Any] | None = None,
    explicit_model_override: bool = False,
    explicit_reasoning_override: bool = False,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve model and effort without changing provider or credentials."""
    route = _primary_route(primary, reasoning_config)
    cfg = routing_config if isinstance(routing_config, Mapping) else {}
    primary_model = route["model"]
    provider = str(route["runtime"].get("provider") or "").strip().lower()
    family, model_match = _model_family(primary_model, provider)

    def finish_baseline(source: str, reason: str) -> dict[str, Any]:
        tier = model_match.group("tier").lower() if model_match else "baseline"
        decision = RoutingDecision(
            tier=tier,
            effort=_reasoning_effort(reasoning_config),
            score=0,
            risk="unknown",
            action="preserve",
            reasons=(reason,),
            source=source,
        )
        route["decision"] = decision.telemetry(model=primary_model)
        return route

    if not _truthy(cfg.get("enabled")):
        return finish_baseline("baseline", "routing_disabled")
    if explicit_model_override:
        return finish_baseline("session_override", "explicit_model_override")
    allowed_platforms = cfg.get("platforms", ("telegram",))
    platform = str((context or {}).get("platform") or "").strip().lower()
    if platform and isinstance(allowed_platforms, (list, tuple, set)):
        normalized_platforms = {str(item).strip().lower() for item in allowed_platforms}
        if platform not in normalized_platforms:
            return finish_baseline("baseline", "platform_not_enabled")
    if not family or not model_match:
        return finish_baseline("baseline", "incompatible_primary_route")

    explicit_tier, explicit_effort = _explicit_user_choice(user_message)
    try:
        decision = classify_task(user_message, context)
    except Exception:
        # A classifier failure must not silently change a user's model or
        # reasoning choice. Preserve the already-valid primary route.
        return finish_baseline("fail_safe", "classifier_error_primary_preserved")

    if not explicit_tier and not explicit_effort:
        decision = _inherit_continuation_decision(
            user_message,
            decision,
            (context or {}).get("previous_auto_decision"),
        )

    lower_routes_approved = _lower_routes_are_approved(cfg)
    if not explicit_tier and not explicit_effort and not lower_routes_approved:
        # Quality first, speed second.  Structural classification may describe
        # the task, but it cannot authorize a downgrade without a benchmark
        # artifact bound to this router revision.  Preserve the known strongest
        # lane and make the missing gate observable in telemetry.
        conservative_effort = (
            "max" if decision.risk == "high" or "long_horizon" in decision.reasons else "xhigh"
        )
        decision = RoutingDecision(
            "sol",
            conservative_effort,
            decision.score,
            decision.risk,
            decision.action,
            tuple((*decision.reasons, "quality_benchmark_required")),
            "quality_fail_safe",
        )

    family_model_keys = (
        {"luna", "terra", "sol"}
        if family == "openai"
        else _CLAUDE_CLI_ALIASES
    )
    explicit_model_key = explicit_tier if explicit_tier in family_model_keys else None
    quality_fail_safe_model_key = (
        "sol" if family == "openai" else "fable"
    ) if decision.source == "quality_fail_safe" else None

    if explicit_model_key:
        decision = RoutingDecision(
            explicit_model_key if family == "openai" else decision.tier,
            explicit_effort or decision.effort,
            decision.score,
            decision.risk,
            decision.action,
            tuple((*decision.reasons, "explicit_user_model")),
            "user_override",
        )
    elif explicit_effort:
        decision = RoutingDecision(
            "sol" if explicit_effort in {"xhigh", "max", "ultra"} else decision.tier,
            explicit_effort,
            decision.score,
            decision.risk,
            decision.action,
            tuple((*decision.reasons, "explicit_user_effort")),
            "user_override",
        )

    if explicit_reasoning_override:
        effort = _reasoning_effort(reasoning_config)
        decision = RoutingDecision(
            decision.tier,
            effort,
            decision.score,
            decision.risk,
            decision.action,
            tuple((*decision.reasons, "explicit_reasoning_override")),
            "session_override",
        )
    elif decision.source == "auto" and decision.effort not in _AUTO_EFFORTS:
        decision = RoutingDecision(
            decision.tier, "xhigh", decision.score, decision.risk,
            decision.action, tuple((*decision.reasons, "auto_effort_clamped")), decision.source,
        )

    target_model = _target_model(
        primary_model,
        decision,
        cfg,
        family,
        explicit_model_key=explicit_model_key or quality_fail_safe_model_key,
    )
    route["model"] = target_model
    # Keep the cache signature anchored to the user's primary route. All three
    # GPT-5.6 tiers share provider/runtime/context; the model and effort are
    # safe per-message fields and do not rebuild the frozen prompt/tool schema.
    route["cache_model"] = primary_model if family == "openai" else target_model
    if explicit_reasoning_override:
        route["reasoning_config"] = (
            dict(reasoning_config) if isinstance(reasoning_config, Mapping) else reasoning_config
        )
    else:
        routed_reasoning = dict(reasoning_config) if isinstance(reasoning_config, Mapping) else {}
        routed_reasoning.update({"enabled": True, "effort": decision.effort})
        route["reasoning_config"] = routed_reasoning
    route["decision"] = decision.telemetry(model=target_model)
    return route
