"""AOF Discovery Promotions lookup — pull-based, fail-open.

Both adversarial reviewers (28/08) rejected preflight injection at spawn
(registry fail-closed becomes a single point of failure; lexical matching
at spawn selects by task OBJECTIVE while incidents are about runtime
OBSTACLES) and converged on this instead: a tool the agent calls WHEN it
hits an obstacle or repeated procedure. Read-only, never raises, and a
broken/absent registry degrades to an honest "unavailable" — it can never
block a turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error  # noqa: F401 (tool_error unused but conventional)

_REGISTRY_CANDIDATES = (
    "/srv/agents/codex/GLOBAL_DISCOVERY_PROMOTIONS.json",
    str(Path.home() / ".codex" / "GLOBAL_DISCOVERY_PROMOTIONS.json"),
)


def _registry_path() -> str | None:
    override = os.environ.get("AOF_PROMOTIONS_REGISTRY", "").strip()
    candidates = ([override] if override else []) + list(_REGISTRY_CANDIDATES)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _match_score(promo: dict, query: str, capability: str) -> int:
    score = 0
    caps = [str(c).casefold() for c in (promo.get("capabilities") or [])]
    if capability and capability.casefold() in caps:
        score += 6
    triggers = promo.get("triggers") or {}
    for phrase in triggers.get("anyPhrases") or []:
        if str(phrase).casefold() in query:
            score += 3
    all_terms = [str(t).casefold() for t in (triggers.get("allTerms") or [])]
    if all_terms and all(term in query for term in all_terms):
        score += 2
    return score


def _handle_aof_lookup(args: dict, **_kw: Any) -> str:
    query = str(args.get("query") or "").strip().casefold()
    capability = str(args.get("capability") or "").strip()
    if not query and not capability:
        return json.dumps({
            "registry": "ok", "matches": [],
            "note": "informe `query` (o obstáculo/necessidade) e/ou `capability`.",
        }, ensure_ascii=False)

    path = _registry_path()
    if path is None:
        return json.dumps({
            "registry": "unavailable",
            "matches": [],
            "note": (
                "registry de Discovery Promotions não encontrado neste host; "
                "siga sem ele e registre o obstáculo no closeout."
            ),
        }, ensure_ascii=False)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        promotions = data.get("promotions") or []
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        return json.dumps({
            "registry": "unavailable",
            "matches": [],
            "note": f"registry ilegível ({type(exc).__name__}); siga sem ele.",
        }, ensure_ascii=False)

    scored = []
    for promo in promotions:
        if str(promo.get("status") or "").lower() != "active":
            continue
        score = _match_score(promo, query, capability)
        if score > 0:
            scored.append((score, int(promo.get("priority") or 0), promo))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    matches = []
    for score, _prio, promo in scored[:3]:
        artifact = promo.get("artifact") or {}
        matches.append({
            "id": promo.get("id"),
            "score": score,
            "description": promo.get("description"),
            "capabilities": promo.get("capabilities"),
            "artifact": artifact.get("path") or artifact,
            "preconditions": promo.get("preconditions"),
        })
    note = (
        "Capability promovida encontrada: é script-first — use o artifact "
        "indicado em vez de redescobrir o procedimento; valide as "
        "preconditions antes."
        if matches else
        "Sem capability promovida para isto. Se o obstáculo for repetível, "
        "registre-o no closeout como candidato de aprendizado."
    )
    return json.dumps(
        {"registry": "ok", "matches": matches, "note": note},
        ensure_ascii=False,
    )


def _check_aof_lookup() -> bool:
    """Available in kanban/board-bound contexts (worker or bound principal)."""
    return bool(
        os.environ.get("HERMES_KANBAN_TASK")
        or os.environ.get("HERMES_PROJECT_BOARD")
        or os.environ.get("HERMES_KANBAN_BOARD")
    )


AOF_LOOKUP_SCHEMA = {
    "name": "aof_lookup",
    "description": (
        "Consulta o registry de Discovery Promotions do AOF quando você "
        "esbarra num obstáculo (ferramenta ausente, procedimento que parece "
        "já ter sido resolvido antes) ou antes de construir algo "
        "potencialmente já promovido. Retorna capabilities promovidas "
        "(script-first) com artifact e preconditions. Fail-open: registry "
        "indisponível nunca bloqueia — siga e registre o obstáculo no "
        "closeout."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "O obstáculo ou necessidade, em texto livre (ex.: "
                    "'transcrever áudio de vídeo', 'python sem Pillow')."
                ),
            },
            "capability": {
                "type": "string",
                "description": (
                    "Nome exato de capability, se conhecido (ex.: "
                    "'media.audio.transcribe')."
                ),
            },
        },
        "required": [],
    },
}

registry.register(
    name="aof_lookup",
    toolset="kanban",
    schema=AOF_LOOKUP_SCHEMA,
    handler=_handle_aof_lookup,
    check_fn=_check_aof_lookup,
    emoji="🧭",
)
