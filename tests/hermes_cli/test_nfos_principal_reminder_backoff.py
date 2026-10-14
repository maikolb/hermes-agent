"""REMINDER_BACKOFF_20261014: a decisão pendente do Principal é uma espera do registro, com lembretes contados e recuo.

Maikol, 10/10/2026: "O NFOS tem que ser autonomo, eu não quero ser babá de IA." Até 14/10 o lembrete de uma decisão retida
saía de 15 em 15 min, sem teto: em 7 dias, 42 decisões retidas levaram 252 lembretes, com intervalo mediano de 15,9 min. Agora
os três primeiros lembretes e a retenção mantêm a hora de sempre; depois, com o Principal ativo (respondeu outra decisão do
quadro desde o lembrete anterior), o intervalo dobra até 2 h. Sem resposta nenhuma dele (parede de cota, sessão parada) o
lembrete segue a cada 15 min, e o card retoma num intervalo quando ele volta. O motor não responde pelo Principal: no prazo
final de 7 dias a decisão segue pendente e visível.
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

HOUR = 3600
DAY = 24 * HOUR
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
    """Card com uma decisão pendente do Principal (impedimento em texto livre: sem continue automático) e o worker fora do ar."""
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


def _at(conn, task_id, kind, start, **match):
    out = []
    for created, payload in conn.execute("SELECT created_at, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                                         (task_id, kind)):
        payload = json.loads(payload or "{}")
        if all(payload.get(key) == value for key, value in match.items()):
            out.append(int(created) - start)
    return out


def _reminders(conn, task_id, start):
    """Só os lembretes: o pedido que abre a decisão não leva o número do lembrete."""
    return [int(created) - start for created, payload in conn.execute(
        "SELECT created_at, payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' ORDER BY id", (task_id,))
        if json.loads(payload or "{}").get("reminder")]


def _rewind_creation(conn, decision, to):
    conn.execute("UPDATE nfos_decisions SET created_at=? WHERE id=?", (to, decision))
    conn.commit()


def _answer_another(conn, task_id, at):
    """O Principal responde outra decisão do quadro: está ativo."""
    run_id = conn.execute("SELECT max(id) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,"
                 "resolved_at) VALUES(?,?,?,'impediment','Outra pergunta?','{}',0,?,'resolved','continue','CONTINUE: outra.','Principal',?)",
                 (f"dec_other_{at}", task_id, run_id, at - 1, at))
    conn.commit()


def _drive(conn, clock, task_id, until, every=300, active=False):
    """O despacho chama o lembrete a cada passada (aqui a cada 5 min) para o card com decisão aberta. Com `active`, o
    Principal responde outra decisão do quadro a cada passada."""
    while clock.now < until:
        clock.now += every
        if active:
            _answer_another(conn, task_id, clock.now - 1)
        delivery.nudge_open_decisions(conn, task_id)


def _wait(conn, decision):
    row = conn.execute("SELECT id FROM nfos_waits WHERE reason='principal_answer' AND json_extract(predicate,'$.decision_id')=? "
                       "ORDER BY created_at DESC,id LIMIT 1", (decision,)).fetchone()
    return nfos_waits.get(conn, row[0]) if row else None


def test_with_the_principal_active_the_gap_doubles_up_to_two_hours_after_the_stall(board, monkeypatch):
    task_id, decision = _card(board)
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    _drive(board, clock, task_id, start + DAY, active=True)
    reminders = _reminders(board, task_id, start)
    assert reminders[:3] == [600, 1500, 2400]
    assert _at(board, task_id, "nfos_decision_stalled", start) == [3300]
    assert kb.get_task(board, task_id).block_kind == "awaiting_principal"
    assert reminders[3:6] == [4200, 7800, 15000]  # 30 min, 60 min, 2 h depois do lembrete anterior
    assert all(b - a == 2 * HOUR for a, b in zip(reminders[5:], reminders[6:]))
    assert len(reminders) == 3 + 3 + (DAY - 15000) // (2 * HOUR)  # antes: um a cada 15 min depois da retenção, 92 no dia
    wait = _wait(board, decision)
    assert wait["status"] == "open" and wait["reason"] == "principal_answer"
    assert wait["executor"] == nfos_waits.PRINCIPAL and wait["predicate"] == {"kind": "decision_resolved", "decision_id": decision}
    # A release anterior pula no lembrete a decisão com espera aberta; esta espera não se liga à decisão por decision_id.
    assert wait["decision_id"] is None and nfos_waits.open_for_decision(board, decision) is None
    assert not nfos_waits.known_decision(board, decision)
    context = json.loads(delivery.get_decision(board, decision)["context"])
    assert [t - start for t in context["reminders"]] == reminders and context["stalled_at"] == start + 3300  # a release anterior lê
    assert all(p["wait_id"] == wait["id"] for p in (json.loads(r[0]) for r in board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' AND created_at>?", (task_id, start))))


def test_with_no_answer_at_all_the_reminder_keeps_every_15_minutes(board, monkeypatch):
    """Parede de cota ou sessão parada: o despertar é recusado e não custa. O lembrete segue a cada 15 min, como antes, e
    quando o Principal volta o card retoma num intervalo."""
    task_id, decision = _card(board, "17")
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    _drive(board, clock, task_id, start + 4 * HOUR)
    assert _reminders(board, task_id, start) == [600, 1500, 2400] + list(range(3600, 4 * HOUR + 1, 900))
    assert _at(board, task_id, "nfos_decision_stalled", start) == [3300]
    _answer_another(board, task_id, clock.now + 1)  # o Principal volta e responde outra decisão
    _drive(board, clock, task_id, start + 6 * HOUR)
    after = [t for t in _reminders(board, task_id, start) if t > 4 * HOUR]
    assert after[0] == 4 * HOUR + 2 * HOUR  # ativo de novo: o lembrete seguinte espera o recuo (2 h depois de 16 lembretes)


def test_the_registry_reminds_by_itself_without_the_dispatcher(board, monkeypatch):
    task_id, decision = _card(board, "12")
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    clock.now = start + 600
    assert delivery.nudge_open_decisions(board, task_id) == [(decision, "reminder 1")]
    for now in (start + 1499, start + 1500, start + 2400, start + 3300, start + 4200):
        clock.now = now  # os eventos levam a hora do relógio
        nfos_waits.sweep(board, now=now)
    assert _reminders(board, task_id, start) == [600, 1500, 2400, 4200]
    assert _at(board, task_id, "nfos_decision_stalled", start) == [3300]
    assert kb.get_task(board, task_id).block_kind == "awaiting_principal"


def test_a_decision_only_the_principal_resolves_is_never_answered_by_the_runtime(board, monkeypatch):
    """Orientação do dono: sem continue automático, sem retenção nova, lembretes com o mesmo recuo e, no prazo final, a
    decisão segue pendente e visível."""
    task_id, decision = _card(board, "13")
    context = json.loads(delivery.get_decision(board, decision)["context"])
    context["owner_guidance"] = "Use a base de outubro."
    board.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(context), decision))
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    _drive(board, clock, task_id, start + 4 * HOUR, active=True)
    assert _reminders(board, task_id, start)[:5] == [600, 1500, 2400, 4200, 7800]
    assert _at(board, task_id, "nfos_decision_stalled", start) == []
    deadline = _wait(board, decision)["deadline_at"]
    assert deadline == start + 600 + 7 * DAY
    _drive(board, clock, task_id, deadline + DAY, every=2 * HOUR, active=True)
    expired = _wait(board, decision)
    assert expired["status"] == "expired" and expired["outcome"] == "still_pending"
    (unanswered,) = [json.loads(r[0]) for r in board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_decision_unanswered'", (task_id,))]
    assert unanswered["decision_id"] == decision and unanswered["wait_id"] == expired["id"]
    assert delivery.get_decision(board, decision)["status"] == "pending"
    after = _reminders(board, task_id, start)
    assert after and after[-1] <= deadline - start  # nada depois do prazo final
    assert "sem resposta do Principal há 7 dias" in delivery.get_workflow(board, task_id)["next_action"]
    _drive(board, clock, task_id, clock.now + DAY, every=2 * HOUR)
    assert _reminders(board, task_id, start) == after and _wait(board, decision)["id"] == expired["id"]


def test_the_answer_satisfies_the_wait_and_ends_the_reminders(board, monkeypatch):
    task_id, decision = _card(board, "14")
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    _drive(board, clock, task_id, start + 600)
    delivery.resolve_decision(board, decision, action="continue", author="Principal", answer="CONTINUE: base de outubro.")
    nfos_waits.sweep(board, now=start + 1500)
    assert _wait(board, decision)["status"] == "satisfied"
    _drive(board, clock, task_id, start + DAY, every=HOUR)
    assert _reminders(board, task_id, start) == [600]


def test_a_human_question_on_the_card_takes_the_decision_out_of_the_reminders(board, monkeypatch):
    task_id, decision = _card(board, "15")
    start = int(time.time())
    clock = Clock(monkeypatch, start)
    _rewind_creation(board, decision, start)
    _drive(board, clock, task_id, start + 600)
    run_id = delivery.get_decision(board, decision)["run_id"]
    board.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,"
                  "resolved_at) VALUES('dec_human_1',?,?,'impediment','Qual base?','{}',0,?,'human','human','PERGUNTA para Ana: qual base?',"
                  "'Principal',?)", (task_id, run_id, start + 700, start + 700))
    board.commit()
    assert nfos_waits.sweep(board, now=start + 1500) == [(_wait(board, decision)["id"], "withdrawn")]
    assert _wait(board, decision)["outcome"] == "human_question"
    _drive(board, clock, task_id, start + 4 * HOUR)
    assert _reminders(board, task_id, start) == [600]


def test_reminders_given_before_this_rule_are_counted(board, monkeypatch):
    """O caso do #267: três lembretes e a retenção já dados, o card em awaiting_principal. O quarto sai na hora (não houve
    resposta nenhuma desde o terceiro) e, com o Principal ativo depois dele, o quinto 60 min depois (antes: 15 min)."""
    task_id, decision = _card(board, "16")
    now = int(time.time())
    clock = Clock(monkeypatch, now)
    context = json.loads(delivery.get_decision(board, decision)["context"])
    context.update(reminders=[now - 3600, now - 2700, now - 1800], stalled_at=now - 900)
    board.execute("UPDATE nfos_decisions SET created_at=?, context=? WHERE id=?", (now - 5000, json.dumps(context), decision))
    board.execute("UPDATE tasks SET status='blocked', block_kind='awaiting_principal' WHERE id=?", (task_id,))
    board.commit()
    assert delivery.nudge_open_decisions(board, task_id) == [(decision, "reminder 4")]
    assert _wait(board, decision)["origin"]["reminders_before"] == 3
    _answer_another(board, task_id, now + 60)  # o Principal está ativo depois do quarto
    for step in range(1, 13):
        clock.now = now + step * 300
        delivery.nudge_open_decisions(board, task_id)
    assert _reminders(board, task_id, now) == [0, 3600]
