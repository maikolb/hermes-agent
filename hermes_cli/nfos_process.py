"""Processo do projeto (project_workflow) como autoridade do motor NFOS.

O motor consulta o processo do board a cada troca que ele mesmo faz: salvar a spec, avançar o estágio,
salvar o relatório, executar efeito externo e concluir o card. O processo do board é o próprio ou, no board
sem processo próprio, a esteira padrão do NFOS. O modo vem da chave ``motor`` do
``policy.json`` ao lado do banco do project_workflow, relido a cada chamada (sem reinício):

- ``off`` (padrão): nada muda;
- ``shadow``: o motor julga, grava a posição do card no processo e deixa na auditoria o que recusaria;
- ``enforce``: o motor obedece. A troca fora do desenho é recusada dentro do próprio motor com o que
  falta, e falha ao consultar o processo também recusa, porque o processo não é opcional nesse board.

A decisão do processo vai no evento do Kanban da troca (``process``), na mesma transação: é a fonte do
andamento do card. O andamento no banco do project_workflow é a projeção dela, gravada logo depois do
commit e refeita pelo observador a partir do evento se essa gravação falhar (idempotente pelo id do evento).
A regra (posição do card, etapas, portões, raias, tipos de entrega governados) é do módulo project_workflow;
aqui fica só a ligação com o motor: board, ator, estado do card, decisão no evento e projeção.
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
    """Política do project_workflow, com a mesma regra de ``project_workflow.policy.load``, lida sem o módulo.

    Arquivo ausente desliga (nunca foi ligado). Arquivo defeituoso trava: o motor obedece em todo board, cada
    um com o seu processo, porque erro de leitura não pode desligar a obediência.
    """
    path = _database().parent / "policy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        return {"enforce": True, "motor": {"*": "enforce"}, "broken": f"policy.json defeituosa: {type(exc).__name__}"}
    motor = data.get("motor", {}) if isinstance(data, dict) else None
    if not isinstance(motor, dict) or any(value not in MODES for value in motor.values()):
        return {"enforce": True, "motor": {"*": "enforce"}, "broken": "policy.json defeituosa: chave motor inválida"}
    return data


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
    row = conn.execute("SELECT t.delivery_type, t.status, t.instruction_revision, t.workspace_path, w.stage, w.spec_revision, "
                       "w.state_json FROM tasks t LEFT JOIN nfos_workflows w ON w.task_id=t.id WHERE t.id=?", (task_id,)).fetchone()
    engine = {k: row[k] for k in ("delivery_type", "status", "stage", "spec_revision")} if row else {}
    if row:
        engine["instruction_revision"] = row["instruction_revision"]
        state = json.loads(row["state_json"] or "{}")
        engine.update({k: state.get(k) for k in ("candidate_sha", "integrated_sha")})
        # Worktree e base do card: na rota curta, o processo lê o diff da candidata antes de publicar (ROTAS_20261009).
        engine["workspace_path"] = row["workspace_path"]
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worktree_creation_requested' "
                             "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        try:
            engine["base_sha"] = (json.loads(event[0] or "{}") or {}).get("base_sha") if event else None
        except ValueError:
            engine["base_sha"] = None
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
    note = (f" Política do project_workflow defeituosa ({rules['broken']}): o motor segue travado até ela ser "
            "corrigida.") if rules.get("broken") else ""
    try:
        enforce, Store = _module()
        # O andamento é projeção das decisões gravadas nos eventos do card: a que ficou só no evento entra antes.
        decision = enforce.decide(Store(_database()), board, task_id,
                                  _engine(conn, task_id, delivery_type, spec_revision),
                                  stage=stage, effect=effect, actor=actor(), rules=rules,
                                  events=lambda: enforce.motor_events_in(conn, task_id))
    except Exception as exc:  # noqa: BLE001 - qualquer falha da consulta vale como processo indisponível
        if current != "enforce":
            return None, None
        return None, (f"PROCESSO do board {board}: o motor não conseguiu consultar o processo do projeto "
                      f"({type(exc).__name__}: {str(exc)[:300]}). Com o processo obrigatório neste board, a troca "
                      "não acontece sem ele. Avise o Principal." + note)
    if decision is None:
        return None, None
    if not decision["allowed"] and current == "enforce":
        return decision, decision["message"] + note
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
    """A decisão como ela vai no evento do Kanban da troca (fonte do andamento do card governado)."""
    if decision is None:
        return None
    enforce, _ = _module()
    return enforce.summary(decision)


def project(conn, task_id, run_id, decision, *, event_id=None) -> bool:
    """Projeta no andamento do card a decisão já gravada no evento, depois do commit da troca.

    A troca já aconteceu com a permissão do processo; falha aqui não a desfaz. Fica um evento
    ``nfos_process_projection_failed``, e a projeção é refeita a partir do evento pelo próximo julgamento do
    card, pelo registro de etapa pela CLI ou pelo observador.
    """
    if decision is None:
        return True
    try:
        enforce, Store = _module()
        # Projeta todas as decisões do card que estão nos eventos, em ordem (a desta troca e qualquer anterior
        # que tenha ficado só no evento), sem repetir o que já está gravado.
        Store(_database()).replay_motor_events(decision["board"], task_id, enforce.motor_events_in(conn, task_id))
        return True
    except Exception as exc:  # noqa: BLE001
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "nfos_process_projection_failed",
                             {"engine_event": event_id, "reason": f"{type(exc).__name__}: {str(exc)[:500]}"},
                             run_id=run_id)
        return False


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
        # O processo do board: o próprio ou, sem ele, a esteira padrão (PROJECT_PROCESS_EFFECT_20261006).
        definition = store.get(card["workflow_id"]) if card else store.active(board, "entrega", include_base=True)
        if definition is None:
            return None
        from project_workflow import spec as specmod
        engine = _engine(conn, task_id)
        history = store.history(board, task_id) if card else []
        # Older installed modules retain their original projection until the
        # coordinated routing module is installed; never grant from this view.
        if hasattr(enforce, "live_definition"):
            definition = enforce.live_definition(store, definition, board, engine)
        spec = definition["spec"]
        if spec.get("validation_policy"):
            from project_workflow import validation
            try:
                spec, _ = validation.effective(spec, history, dict(engine, board=board, task_id=task_id))
            except (ValueError, TypeError, KeyError, OSError):
                pass  # judge reports the refusal; this remains a read-only view.
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
