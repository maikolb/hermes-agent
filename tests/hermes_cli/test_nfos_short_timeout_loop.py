"""NFOS_SHORT_TIMEOUT_LOOP_20261016: card do NFOS em laço de timed_out curtos volta ao Principal como impedimento.

t_3f155a07 (Concursa, 21-23/09/2026): 235 execuções de cerca de 1 min, todas timed_out com o limite já estourado, em pares
separados por gave_up e unblocked automáticos. O disjuntor de falhas contava, o laço o desfazia. Agora, quando as duas
últimas execuções encerradas do card são timed_out em menos de 3 min e sem entrega durável, o dispatcher não lança a
terceira: o disjuntor desiste pelo mesmo caminho do #315 e o Principal recebe a decisão. Medida em
output/nfos-disjuntor/portao: 1 card parado em 5 semanas e 0 em 72 h; 2 em 94 concluídos tiveram a regra aplicada à história.
"""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

LOOP_ERROR = "elapsed 7297s > limit 7200s"
GONE_PID = 2147483647  # acima do pid_max do Linux: nenhum processo responde por ele


@pytest.fixture
def conn(tmp_path, monkeypatch, all_assignees_spawnable):
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


def _card(conn):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "901"},
        text="Erro na plataforma: erro ao puxar disciplinas de um determinado cargo.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=8)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=GONE_PID)
    return kb.get_task(conn, task.id)


def _idle(conn, task_id, outcome="crashed"):
    """O worker saiu: execução encerrada e card de volta a ready."""
    now = int(time.time())
    conn.execute("UPDATE task_runs SET status=?, outcome=?, ended_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                 (outcome, outcome, now, task_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task_id,))
    conn.commit()


def _ended_run(conn, task_id, seconds, outcome="timed_out", kinds=(), error=LOOP_ERROR):
    """Uma execução já encerrada, `seconds` de duração, com os eventos dados."""
    end = int(time.time())
    run_id = conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,error) VALUES(?,?,?,?,?,?)",
                          (task_id, outcome, end - seconds, end, outcome, error)).lastrowid
    for kind in kinds:
        kb._append_event(conn, task_id, kind, {}, run_id=run_id)
    conn.commit()
    return run_id


def _dispatch(conn):
    spawned = []
    result = kb.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id), max_spawn=4)
    return spawned, result


def _events(conn, task_id, kind):
    return [json.loads(p or "{}") for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def test_two_short_timeouts_stop_the_card_and_hand_it_to_the_principal(conn):
    """O caso do t_3f155a07: a terceira execução não nasce; o card volta ao Principal com a causa."""
    task = _card(conn)
    _idle(conn, task.id)
    _ended_run(conn, task.id, 65)
    _ended_run(conn, task.id, 66)

    spawned, result = _dispatch(conn)
    assert spawned == [] and result.auto_blocked == [task.id]
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ("blocked", "awaiting_principal")
    [gave_up] = _events(conn, task.id, "gave_up")
    assert gave_up["trigger_outcome"] == "short_timeout_loop" and gave_up["failures"] == 2 and gave_up["error"] == LOOP_ERROR
    [decision] = [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,))]
    assert decision["kind"] == "impediment"
    assert "duas últimas execuções passaram do tempo limite em menos de 3 minutos" in decision["question"]
    assert conn.execute("SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0] == "gave_up"


def test_after_the_principal_continues_the_card_gets_two_more_runs_before_stopping_again(conn):
    task = _card(conn)
    _idle(conn, task.id)
    _ended_run(conn, task.id, 65)
    _ended_run(conn, task.id, 66)
    assert _dispatch(conn)[0] == []
    [decision] = [dict(r) for r in conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,))]
    delivery.resolve_decision(conn, decision["id"], action="continue", answer="CONTINUE: causa tratada.", author="Principal")
    assert delivery.sweep_awaiting_principal(conn) == [task.id]

    spawned, _ = _dispatch(conn)
    assert spawned == [task.id], "a última execução é a desistência, não um timed_out curto"
    _idle(conn, task.id, "timed_out")
    conn.execute("UPDATE task_runs SET started_at=ended_at-60 WHERE id=(SELECT MAX(id) FROM task_runs WHERE task_id=?)", (task.id,))
    conn.commit()
    assert _dispatch(conn)[0] == [task.id], "um timed_out curto só não basta"


@pytest.mark.parametrize("second,kinds", [
    (3 * 3600, ()),                       # a execução longa que estourou o limite é trabalho, não laço
    (66, ("nfos_spec_saved",)),           # curta, mas deixou algo durável
    (66, ("nfos_report_saved",)),
    (66, ("nfos_probe",)),
    (66, ("nfos_effect_reconciled",)),
    (66, ("nfos_homologation_bound",)),
    (180, ()),                            # a fronteira: 180 s já não é curta
    (300, ()),
    (1700, ()),                           # abaixo de 1800, acima do corte
], ids=["long-run", "spec-saved", "report-saved", "probe", "effect-reconciled", "homologation-bound",
        "exactly-180s", "300s", "1700s"])
def test_a_run_that_is_long_or_delivered_something_breaks_the_streak(conn, second, kinds):
    task = _card(conn)
    _idle(conn, task.id)
    _ended_run(conn, task.id, 65)
    _ended_run(conn, task.id, second, kinds=kinds)
    spawned, _ = _dispatch(conn)
    assert spawned == [task.id] and kb.get_task(conn, task.id).status == "running"
    assert _events(conn, task.id, "gave_up") == []


def test_a_run_just_under_the_cut_is_still_the_loop(conn):
    task = _card(conn)
    _idle(conn, task.id)
    _ended_run(conn, task.id, 65)
    _ended_run(conn, task.id, 179)
    assert _dispatch(conn)[0] == []


def test_a_forward_workflow_step_breaks_the_streak(conn):
    task = _card(conn)
    _idle(conn, task.id)
    run_id = _ended_run(conn, task.id, 65)
    kb._append_event(conn, task.id, "nfos_progress", {"process": {"move": "forward"}}, run_id=run_id)
    _ended_run(conn, task.id, 66)
    conn.commit()
    assert _dispatch(conn)[0] == [task.id]


def test_other_short_outcomes_are_not_the_loop(conn):
    """crashed e reclaimed curtos são outro problema: a regra é só do timed_out."""
    task = _card(conn)
    _idle(conn, task.id)
    _ended_run(conn, task.id, 65, outcome="crashed", error="pid 1 not alive")
    _ended_run(conn, task.id, 66)
    assert _dispatch(conn)[0] == [task.id]


def test_card_outside_the_nfos_keeps_the_dispatcher_behaviour(conn):
    task_id = kb.create_task(conn, title="plain kanban card", assignee="worker")
    kb.claim_task(conn, task_id)
    _idle(conn, task_id)
    _ended_run(conn, task_id, 65)
    _ended_run(conn, task_id, 66)
    spawned, result = _dispatch(conn)
    assert spawned == [task_id] and result.auto_blocked == []


def _pending(conn, task_id):
    return [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')", (task_id,))]


def _continue(conn, task_id):
    [decision] = _pending(conn, task_id)
    delivery.resolve_decision(conn, decision["id"], action="continue", answer="CONTINUE: causa tratada.", author="Principal")
    assert delivery.sweep_awaiting_principal(conn) == [task_id]


@pytest.mark.parametrize("cycles", [2, 4])
def test_every_stop_creates_its_decision_not_only_the_first(conn, cycles):
    """Parada, continue, parada: unblock_task deixa o block_kind da parada anterior no card. A revisora do #362 mostrou
    a segunda parada sem decisão (own_breaker_stop só age em blocked sem tipo) e o sweep soltando o card em silêncio."""
    task = _card(conn)
    _idle(conn, task.id)
    for cycle in range(1, cycles + 1):
        _ended_run(conn, task.id, 65)
        _ended_run(conn, task.id, 66)
        spawned, result = _dispatch(conn)
        assert spawned == [] and result.auto_blocked == [task.id], f"ciclo {cycle}"
        held = kb.get_task(conn, task.id)
        assert (held.status, held.block_kind) == ("blocked", "awaiting_principal"), f"ciclo {cycle}"
        assert len(_pending(conn, task.id)) == 1, f"ciclo {cycle}: a parada tem de criar a decisão"
        assert delivery.sweep_awaiting_principal(conn) == [], f"ciclo {cycle}: com a decisão aberta o card fica"
        _continue(conn, task.id)
    assert len(_events(conn, task.id, "gave_up")) == cycles


@pytest.mark.parametrize("cycles", [2, 4])
def test_the_failure_breaker_of_315_creates_the_decision_on_every_stop(conn, cycles):
    """O mesmo defeito no disjuntor de falhas já em produção: qualquer card que o Principal liberou e que desiste de novo."""
    task = _card(conn)
    _idle(conn, task.id)
    for cycle in range(1, cycles + 1):
        for expected in (False, True):
            assert kb._record_task_failure(conn, task.id, "pid 1586801 exited with code 1", outcome="crashed",
                                           failure_limit=2, release_claim=False, end_run=False) is expected
        assert len(_pending(conn, task.id)) == 1, f"ciclo {cycle}: a desistência tem de criar a decisão"
        _continue(conn, task.id)
