"""PRINCIPAL_BOTTLENECK_20261016: toda decisão pendente do Principal ganha a sua espera sem passar pelo despacho.

Em 16/10/2026, cinco decisões de worker em quatro cards `ready` ficaram sem espera `principal_answer` e sem lembrete: o lembrete
só nascia em `nudge_open_decisions`, que o despachante chama depois das guardas que pulam o card (vaga, rota fechada, recuo,
respawn_guarded). O card ficava sem despacho enquanto a decisão estava aberta, e a decisão sem quem a lembrasse.
`sweep_lab_waits` (a cada tick do runtime) agora adota essas decisões.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_waits

TELEGRAM = {"platform": "telegram", "chat_id": "-10001", "thread_id": "8"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
        yield conn


class Clock:
    def __init__(self, monkeypatch, start):
        self.now = start
        monkeypatch.setattr(delivery.time, "time", lambda: self.now)


def _card(conn, message_id="11"):
    """Card `ready` com uma decisão `impediment` pendente criada por ask_principal, sem passar pelo despacho."""
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rid = delivery.receive_request(conn, source=dict(TELEGRAM, message_id=message_id), text="Relatório de matrículas.",
                                       project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
        request = delivery.reserve_request(conn, capacity=4)
        task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker.pid)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="O relatório usa a base de setembro ou a de outubro?", context={})
        conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
                     (int(time.time()), task.current_run_id))
        conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
        conn.commit()
    finally:
        worker.kill()
        worker.wait()
    return task.id, decision


def _start(conn, monkeypatch, decision):
    start = int(time.time())
    conn.execute("UPDATE nfos_decisions SET created_at=? WHERE id=?", (start, decision))
    conn.commit()
    return Clock(monkeypatch, start), start


def _waits(conn, decision):
    if not nfos_waits._table(conn):  # o registro cria a tabela no primeiro uso
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM nfos_waits WHERE reason='principal_answer' AND json_extract(predicate,'$.decision_id')=? ORDER BY id", (decision,))]


def _reminder_times(conn, task_id, decision, start):
    return [int(created) - start for created, p in conn.execute(
        "SELECT created_at, payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' ORDER BY id", (task_id,))
        if json.loads(p).get("decision_id") == decision and json.loads(p).get("reminder")]


def _requests(conn, task_id, decision):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' ORDER BY id", (task_id,))
        if json.loads(p).get("decision_id") == decision]


def test_a_worker_decision_gets_its_wait_and_its_first_reminder_without_the_dispatcher(board, monkeypatch):
    task_id, decision = _card(board)
    clock, start = _start(board, monkeypatch, decision)
    assert _waits(board, decision) == []  # ask_principal não abre a espera: ela só nascia no despacho
    delivery.sweep_lab_waits(board)
    (wait,) = _waits(board, decision)
    assert wait["status"] == "open" and wait["task_id"] == task_id
    assert [r for r in _requests(board, task_id, decision) if r.get("reminder")] == []  # antes da hora, nenhum lembrete
    clock.now = start + delivery.DECISION_REMINDER_AFTER
    delivery.sweep_lab_waits(board)
    reminders = [r for r in _requests(board, task_id, decision) if r.get("reminder")]
    assert [r["reminder"] for r in reminders] == [1] and reminders[0]["wait_id"] == wait["id"]
    assert kb.get_task(board, task_id).status == "ready"  # o sweep não responde nem retém: o despacho continua dono do card


def test_the_adopted_wait_keeps_reminding_and_never_holds_the_card(board, monkeypatch):
    """O card que o despacho não alcança continua `ready` depois do 3º lembrete (e depois da hora da retenção, 55 min): a
    espera adotada só lembra, com o recuo de sempre. A retenção awaiting_principal é decisão do despacho."""
    task_id, decision = _card(board, "21")
    clock, start = _start(board, monkeypatch, decision)
    while clock.now < start + 4 * 3600:
        delivery.sweep_lab_waits(board)  # o tick real do runtime, sem nudge_open_decisions
        clock.now += 300
    task = kb.get_task(board, task_id)
    assert task.status == "ready" and task.block_kind != "awaiting_principal"
    assert board.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_decision_stalled'", (task_id,)).fetchone()[0] == 0
    times = _reminder_times(board, task_id, decision, start)
    assert times[:4] == [600, 1500, 2400, 3300]  # nos mesmos minutos 10, 25 e 40 de sempre; o recuo segue depois deles
    assert times[3] < 3600 < times[-1] and len(set(times)) == len(times)  # os lembretes continuam depois dos 55 min
    (wait,) = _waits(board, decision)
    assert wait["status"] == "open" and nfos_waits.get(board, wait["id"])["origin"].get("no_hold") is True


def test_a_decision_held_by_the_lab_queue_gets_no_principal_wait(board, monkeypatch):
    task_id, decision = _card(board, "12")
    clock, start = _start(board, monkeypatch, decision)
    context = json.loads(delivery.get_decision(board, decision)["context"])
    context["lab_wait"] = {"hold": True, "receipt": "r-1", "since": start, "next_check_at": start + 10 ** 6}
    board.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(context), decision))
    board.commit()
    clock.now = start + 2 * delivery.DECISION_REMINDER_AFTER
    assert delivery._adopt_principal_answer_waits(board, clock.now) == []
    assert _waits(board, decision) == []


def test_two_sweeps_open_one_wait_for_the_same_decision(board, monkeypatch):
    task_id, decision = _card(board, "13")
    clock, start = _start(board, monkeypatch, decision)
    delivery.sweep_lab_waits(board)
    clock.now += 60
    delivery.sweep_lab_waits(board)
    assert len(_waits(board, decision)) == 1
    assert delivery._adopt_principal_answer_waits(board, clock.now) == []  # a chamada direta também não duplica


def test_a_card_the_dispatcher_reaches_is_held_at_55_minutes_as_before(board, monkeypatch):
    """A dispensa da retenção vale só enquanto o despacho não alcança o card. Com sweep e nudge no mesmo tick, desde a idade 0,
    o nudge encontra a espera que o sweep adotou e a retenção do REMINDER_BACKOFF_20261014 continua no minuto 55."""
    task_id, decision = _card(board, "23")
    clock, start = _start(board, monkeypatch, decision)
    while clock.now <= start + 3300:
        delivery.sweep_lab_waits(board)
        delivery.nudge_open_decisions(board, task_id)
        clock.now += 60
    stalled = [int(c) - start for (c,) in board.execute(
        "SELECT created_at FROM task_events WHERE task_id=? AND kind='nfos_decision_stalled'", (task_id,))]
    assert stalled == [3300]
    assert kb.get_task(board, task_id).block_kind == "awaiting_principal"
    assert _reminder_times(board, task_id, decision, start) == [600, 1500, 2400]
    (wait,) = _waits(board, decision)
    origin = nfos_waits.get(board, wait["id"])["origin"]
    assert "no_hold" not in origin and origin["reached_by_dispatch"] is True


def test_a_closed_card_and_an_answered_decision_are_not_adopted(board, monkeypatch):
    done_task, done_decision = _card(board, "14")
    clock, start = _start(board, monkeypatch, done_decision)
    board.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (start, done_task))
    answered_task, answered = _card(board, "15")
    delivery.resolve_decision(board, answered, action="continue", answer="CONTINUE: siga.", author="Principal")
    board.commit()
    clock.now = start + 2 * delivery.DECISION_REMINDER_AFTER
    delivery.sweep_lab_waits(board)
    assert _waits(board, done_decision) == [] and _waits(board, answered) == []


def test_a_decision_with_another_open_wait_in_the_registry_is_left_to_it(board, monkeypatch):
    """nfos_waits.open_for_decision é a exclusão que o lembrete sempre teve: o dono do passo é a outra espera."""
    task_id, decision = _card(board, "22")
    clock, start = _start(board, monkeypatch, decision)
    nfos_waits.declare(board, task_id, None, reason="principal_decision", executor=nfos_waits.PRINCIPAL,
                       predicate={"kind": "decision_resolved", "decision_id": decision}, decision_id=decision,
                       subject="decisão entregue ao Principal", now=start)
    assert nfos_waits.open_for_decision(board, decision)
    clock.now = start + 2 * delivery.DECISION_REMINDER_AFTER
    assert delivery._adopt_principal_answer_waits(board, clock.now) == []
    assert _waits(board, decision) == []  # só existe a espera de outro motivo; a adoção não abre uma segunda


def test_a_decision_behind_an_open_human_question_is_left_to_the_person(board, monkeypatch):
    """Pergunta `human` aberta no card, e a decisão sem orientação nem resposta humana: o card espera a pessoa, como o
    lembrete sempre pulou."""
    task_id, decision = _card(board, "16")
    clock, start = _start(board, monkeypatch, decision)
    board.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status) "
                  "SELECT 'dec_human',task_id,run_id,'impediment','Pergunta ao humano?','{}',0,created_at,'human' "
                  "FROM nfos_decisions WHERE id=?", (decision,))
    board.commit()
    assert delivery._adopt_principal_answer_waits(board, start + 2 * delivery.DECISION_REMINDER_AFTER) == []
    assert _waits(board, decision) == []
