"""Processo do projeto (project_workflow) como autoridade do motor NFOS.

O motor consulta o processo do board a cada troca que ele mesmo faz: salvar a spec, avançar o estágio,
salvar o relatório, executar efeito externo e concluir o card. O modo vem da chave ``motor`` do
``policy.json`` ao lado do banco do project_workflow, relido a cada chamada (sem reinício):

- ``off`` (padrão): nada muda;
- ``shadow``: o motor julga, grava a posição do card no processo e deixa na auditoria o que recusaria;
- ``enforce``: o motor obedece. A troca fora do desenho é recusada dentro do próprio motor com o que
  falta, e falha ao consultar o processo também recusa, porque o processo não é opcional nesse board.

A regra (posição do card, etapas, portões, raias, tipos de entrega governados) é do módulo
project_workflow; aqui fica só a ligação com o motor: board, ator, estado do card e a gravação da etapa
na mesma transação da troca, depois do evento que a registra no Kanban.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hermes_cli.nfos_delivery import WorkflowError

DEFAULT_SRC = "/opt/nexa-factory-os/project_workflow/current/src"
DEFAULT_DB = "/srv/hermes/project_workflow/project_workflow.db"
MODES = ("off", "shadow", "enforce")


class ProcessRefusal(WorkflowError):
    """O processo do projeto não permite esta troca agora."""


def _database() -> Path:
    return Path(os.environ.get("PROJECT_WORKFLOW_DB") or DEFAULT_DB)


def _rules() -> dict:
    try:
        data = json.loads((_database().parent / "policy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def board_of(conn) -> str | None:
    """Board do banco do Kanban desta conexão (``.../kanban/boards/<board>/kanban.db``)."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            path = Path(row[2])
            if path.parent.parent.name == "boards":
                return path.parent.name
    return os.environ.get("HERMES_KANBAN_BOARD") or None


def mode(board: str | None, rules: dict | None = None) -> str:
    """Modo do motor no board. Lido aqui, sem o módulo, para a falha fechada valer mesmo sem ele."""
    motor = (rules if rules is not None else _rules()).get("motor") if board else None
    if not isinstance(motor, dict):
        return "off"
    value = motor.get(board, motor.get("*", "off"))
    return value if value in MODES else "off"


def _module():
    src = os.environ.get("PROJECT_WORKFLOW_SRC") or DEFAULT_SRC
    if src not in sys.path:
        sys.path.insert(0, src)
    from project_workflow import enforce
    from project_workflow.store import Store
    return enforce, Store


def actor() -> str:
    return "worker" if os.environ.get("HERMES_KANBAN_TASK") else "principal"


def _engine(conn, task_id, delivery_type=None, spec_revision=None) -> dict:
    row = conn.execute("SELECT t.delivery_type, t.status, w.stage, w.spec_revision FROM tasks t "
                       "LEFT JOIN nfos_workflows w ON w.task_id=t.id WHERE t.id=?", (task_id,)).fetchone()
    engine = {k: row[k] for k in ("delivery_type", "status", "stage", "spec_revision")} if row else {}
    if delivery_type:
        engine["delivery_type"] = delivery_type
    if spec_revision is not None:
        engine["spec_revision"] = spec_revision
    return engine


def evaluate(conn, task_id, *, stage=None, effect=None, delivery_type=None, spec_revision=None):
    """Julga a troca pelo processo do board: ``(decisão, recusa)``.

    A decisão é None quando o processo não governa o card. A recusa só vem em ``enforce``: a mensagem
    do processo, ou a falha ao consultá-lo. Em ``shadow`` a decisão volta mesmo recusada, para gravar
    a posição, e a recusa fica só na auditoria.
    """
    board = board_of(conn)
    rules = _rules()
    current = mode(board, rules)
    if current == "off":
        return None, None
    try:
        enforce, Store = _module()
        decision = enforce.decide(Store(_database()), board, task_id,
                                  _engine(conn, task_id, delivery_type, spec_revision),
                                  stage=stage, effect=effect, actor=actor(), rules=rules)
    except Exception as exc:  # noqa: BLE001 - qualquer falha da consulta vale como processo indisponível
        if current != "enforce":
            return None, None
        return None, (f"PROCESSO do board {board}: o motor não conseguiu consultar o processo do projeto "
                      f"({type(exc).__name__}: {str(exc)[:300]}). Com o processo obrigatório neste board, a troca "
                      "não acontece sem ele. Avise o Principal.")
    if decision is None:
        return None, None
    if not decision["allowed"] and current == "enforce":
        return decision, decision["message"]
    return decision, None


def guard(conn, task_id, **what):
    """Como ``evaluate``, mas recusa com ``ProcessRefusal`` (dentro da transação, desfaz a troca)."""
    decision, refusal = evaluate(conn, task_id, **what)
    if refusal:
        raise ProcessRefusal(refusal)
    return decision


def last_event_id(conn) -> int:
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def summary(decision) -> dict | None:
    """Resumo da decisão para o evento do Kanban da troca."""
    if decision is None:
        return None
    return {"step": decision.get("target") if decision["move"] in ("forward", "back", "jump") else decision.get("current"),
            "move": decision["move"], "entry": decision.get("entry"), "allowed": decision["allowed"],
            "mode": decision["mode"], "workflow_id": decision["workflow_id"], "version": decision["version"]}


def record(decision, *, engine_stage=None, event_id=None) -> None:
    """Grava a entrada e a troca de etapa no andamento do card, depois do evento do Kanban da mesma troca.

    Em ``enforce``, falha ao gravar recusa a troca (a transação do motor é desfeita): o motor não avança
    sem registrar onde o card está no processo.
    """
    if decision is None:
        return
    try:
        enforce, Store = _module()
        enforce.record(Store(_database()), decision, engine_stage=engine_stage, event_id=event_id)
    except Exception as exc:  # noqa: BLE001
        if decision.get("mode") == "enforce":
            raise ProcessRefusal(f"PROCESSO {decision.get('title')}: o motor não conseguiu gravar a etapa do card "
                                 f"({type(exc).__name__}: {str(exc)[:300]}); a troca foi desfeita.") from exc


def completion_blocked(conn, task_id, run_id, refusal, *, append_event) -> None:
    """Registra a recusa de conclusão pelo processo uma vez por motivo (o despachante tenta a cada ciclo)."""
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completion_blocked_process' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        previous = json.loads(last["payload"]).get("reason") if last and last["payload"] else None
    except (TypeError, ValueError):
        previous = None
    if previous == refusal[:2000]:
        return
    append_event(conn, task_id, "completion_blocked_process", {"reason": refusal[:2000]}, run_id=run_id)
    conn.execute("UPDATE nfos_workflows SET next_action=? WHERE task_id=?", (refusal[:400], task_id))


def position(conn, task_id) -> dict | None:
    """Onde o card está no processo do board, para o ``show`` do motor (só leitura)."""
    board = board_of(conn)
    rules = _rules()
    current = mode(board, rules)
    if current == "off":
        return None
    try:
        enforce, Store = _module()
        store = Store(_database(), readonly=True)
        card = store.card(board, task_id)
        definition = store.get(card["workflow_id"]) if card else store.active(board, "entrega", include_base=False)
        if definition is None or definition["project"] == "*":
            return None
        from project_workflow import spec as specmod
        spec = definition["spec"]
        engine = _engine(conn, task_id)
        history = store.history(board, task_id) if card else []
        judged = enforce.judge(definition, history, engine, stage=engine.get("stage") or "analysis", actor=actor())
        steps = specmod.steps_by_key(spec)
        here = judged["current"]
        return {"mode": current, "governed": specmod.governs(spec, engine.get("delivery_type")),
                "title": definition["title"], "version": definition["version"], "current": here,
                "current_name": steps.get(here, {}).get("name"),
                "permits": sorted(specmod.permits(spec, here)),
                "next": [{"step": t["to"], "name": steps[t["to"]]["name"], "when": t.get("when")}
                         for t in specmod.outgoing(spec, here)]}
    except Exception as exc:  # noqa: BLE001
        return {"mode": current, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
