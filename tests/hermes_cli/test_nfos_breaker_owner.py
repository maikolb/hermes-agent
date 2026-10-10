"""BREAKER_OWNER_20261010: o disjuntor de falhas não deixa card do NFOS parado sem dono.

dovcrm t_4cb4df58 (06/10/2026): duas falhas de spawn com o lacre do worktree inválido, o disjuntor desistiu e o card ficou
blocked sem tipo, sem decisão e sem pausa por mais de 90 h. ccm t_b2c7c034 (22/09/2026): o mesmo depois de duas quedas do
worker, 17 dias. Ao desistir, a falha de workspace vira pausa de manutenção, que o runtime cobra do Principal, e a queda ou
o tempo esgotado viram decisão pendente do Principal com o erro. O card que já estava assim ganha o mesmo dono, uma vez.
"""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_workspace_repair as repair

WORKSPACE_ERROR = ("workspace: sealed worktree ownership is no longer valid: owned linked worktree no longer matches "
                   "creation receipt")
CRASH_ERROR = "pid 1586801 exited with code 1"
GONE_PID = 2147483647  # acima do pid_max do Linux: nenhum processo responde por ele


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)
    with kb.connect_closing() as connection:
        delivery.init_schema(connection)
        yield connection


def _card(conn, n, pid=None):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": str(900 + n)},
        text=f"Erro no CRM: falha na qualificação de leads {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=8)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=pid or os.getpid())
    return kb.get_task(conn, task.id)


def _idle(conn, task):
    """O worker saiu: execução encerrada e card de volta a ready, como o chamador do disjuntor deixa na queda."""
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()


def _events(conn, task_id, kind):
    return [(row[0], json.loads(row[1] or "{}")) for row in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def _open_decisions(conn, task_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human') ORDER BY created_at, id", (task_id,))]


def test_workspace_failure_gives_up_into_a_maintenance_pause_the_principal_owns(conn):
    task = _card(conn, 1, pid=GONE_PID)
    _idle(conn, task)  # o primeiro worker saiu; daqui em diante o dispatcher reivindica e falha ao preparar o workspace
    for expected in (False, True):
        refusal = []
        assert kb.claim_task(conn, task.id, refusal=refusal) is not None, refusal
        assert kb._record_spawn_failure(conn, task.id, WORKSPACE_ERROR, failure_limit=2) is expected

    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ("blocked", "awaiting_principal")
    gave_up_id, gave_up = _events(conn, task.id, "gave_up")[-1]
    run_id = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (task.id,)).fetchone()[0]
    pause = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])["maintenance_pause"]
    assert pause["kind"] == "failure_breaker" and pause["actor"] == "runtime"
    assert pause["breaker"] == {"outcome": "spawn_failed", "failures": 2, "limit": 2, "gave_up_event_id": gave_up_id,
                                "error": WORKSPACE_ERROR, "at": pause["breaker"]["at"]}
    assert repair.maintenance_pause_pending(conn, task.id)

    # O tick cobra a pausa do Principal e o card não volta sozinho à fila.
    assert delivery.sweep_awaiting_principal(conn) == []
    [obligation] = _open_decisions(conn, task.id)
    recovery = json.loads(obligation["context"])["maintenance_recovery"]
    assert recovery["pause_run_id"] == run_id and recovery["resume_route"] == "resume-after-repair"
    assert WORKSPACE_ERROR in obligation["question"]
    assert obligation["id"] in [payload.get("decision_id") for _, payload in _events(conn, task.id, "nfos_principal_requested")]
    assert kb.get_task(conn, task.id).status == "blocked"


@pytest.mark.parametrize("outcome,error", [("crashed", CRASH_ERROR), ("timed_out", "elapsed 7300s > limit 7200s"),
                                           ("spawn_failed", "spawn: profile executable not found")])
def test_other_failures_give_up_into_a_pending_decision_of_the_principal(conn, outcome, error):
    task = _card(conn, 2)
    _idle(conn, task)
    for expected in (False, True):
        assert kb._record_task_failure(conn, task.id, error, outcome=outcome, failure_limit=2,
                                       release_claim=False, end_run=False) is expected

    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ("blocked", "awaiting_principal")
    assert not repair.maintenance_pause_pending(conn, task.id)
    gave_up_id, _ = _events(conn, task.id, "gave_up")[-1]
    [decision] = _open_decisions(conn, task.id)
    assert decision["kind"] == "impediment" and decision["status"] == "pending" and error in decision["question"]
    breaker = json.loads(decision["context"])["breaker"]
    assert breaker == {"outcome": outcome, "failures": 2, "limit": 2, "gave_up_event_id": gave_up_id, "error": error,
                       "at": breaker["at"]}
    assert decision["id"] in [d["id"] for d in delivery.pending_decisions(conn)], "a decisão está na lista do Principal"
    assert delivery.sweep_awaiting_principal(conn) == [], "com a decisão aberta o card fica"

    # A resposta do Principal devolve o card à fila.
    delivery.resolve_decision(conn, decision["id"], action="continue", answer="CONTINUE: causa tratada.", author="Principal")
    assert delivery.sweep_awaiting_principal(conn) == [task.id]
    assert kb.get_task(conn, task.id).status == "ready"


def _legacy_stop(conn, task, error, outcome):
    """O estado que o disjuntor deixava antes desta regra: blocked sem tipo, só com o evento gave_up."""
    _idle(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind=NULL, consecutive_failures=2, last_failure_error=? WHERE id=?",
                     (error, task.id))
        kb._append_event(conn, task.id, "gave_up", {"failures": 2, "effective_limit": 2, "limit_source": "dispatcher",
                                                     "error": error, "trigger_outcome": outcome, "retry_status": "ready"})
        kb._append_event(conn, task.id, "nfos_worker_exit_confirmed", {"status": "confirmed"})


@pytest.mark.parametrize("outcome,error,opens", [("spawn_failed", WORKSPACE_ERROR, "pause"), ("crashed", CRASH_ERROR, "decision")])
def test_card_already_parked_by_the_breaker_gets_the_same_owner_once(conn, outcome, error, opens):
    task = _card(conn, 3)
    _legacy_stop(conn, task, error, outcome)
    assert _open_decisions(conn, task.id) == [] and not repair.maintenance_pause_pending(conn, task.id)

    assert delivery.sweep_awaiting_principal(conn) == []
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ("blocked", "awaiting_principal")
    [decision] = _open_decisions(conn, task.id)
    context = json.loads(decision["context"])
    assert repair.maintenance_pause_pending(conn, task.id) is (opens == "pause")
    assert ("maintenance_recovery" in context) is (opens == "pause") and ("breaker" in context) is (opens == "decision")

    requests = len(_events(conn, task.id, "nfos_principal_requested"))
    assert delivery.sweep_awaiting_principal(conn) == []
    assert len(_open_decisions(conn, task.id)) == 1 and len(_events(conn, task.id, "nfos_principal_requested")) == requests


def test_block_that_is_not_the_breakers_is_left_alone(conn):
    task = _card(conn, 4)
    _legacy_stop(conn, task, CRASH_ERROR, "crashed")
    with kb.write_txn(conn):  # depois da desistência alguém bloqueou de novo: o último bloqueio já não é do disjuntor
        kb._append_event(conn, task.id, "blocked", {"reason": "Aguardando o cliente enviar o arquivo."})
    assert repair.own_breaker_stop(conn, task.id) is None
    assert delivery.sweep_awaiting_principal(conn) == []
    assert kb.get_task(conn, task.id).block_kind is None and _open_decisions(conn, task.id) == []


def test_owner_step_that_fails_does_not_undo_the_breaker_and_the_sweep_applies_it(conn, monkeypatch):
    task = _card(conn, 5)
    _idle(conn, task)

    def half_way(conn, task_id):
        with kb.write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET block_kind='awaiting_principal' WHERE id=?", (task_id,))
            raise RuntimeError("owner step failed half way")

    with monkeypatch.context() as patch:
        patch.setattr(repair, "own_breaker_stop", half_way)
        for expected in (False, True):
            assert kb._record_task_failure(conn, task.id, CRASH_ERROR, outcome="crashed", failure_limit=2,
                                           release_claim=False, end_run=False) is expected
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ("blocked", None), "o disjuntor parou o card e o dono pela metade saiu"
    assert len(_events(conn, task.id, "gave_up")) == 1 and _open_decisions(conn, task.id) == []

    assert delivery.sweep_awaiting_principal(conn) == []
    assert kb.get_task(conn, task.id).block_kind == "awaiting_principal" and len(_open_decisions(conn, task.id)) == 1


def test_card_outside_the_nfos_keeps_the_dispatcher_behaviour(conn):
    task_id = kb.create_task(conn, title="plain kanban card", assignee="worker")
    for expected in (False, True):
        assert kb._record_task_failure(conn, task_id, CRASH_ERROR, outcome="crashed", failure_limit=2,
                                       release_claim=False, end_run=False) is expected
    plain = kb.get_task(conn, task_id)
    assert (plain.status, plain.block_kind) == ("blocked", None)
