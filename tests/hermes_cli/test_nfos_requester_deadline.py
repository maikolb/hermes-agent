"""REQUESTER_DEADLINE_20261010: pergunta ao solicitante sem resposta ganha prazo no motor.

Maikol, 10/10/2026: "3. Mande colocar um prazo" e "As correções no NFOS são pra tudo. Todos os projetos." O card esperava para
sempre: em 10/10 havia card parado há 17 dias. Agora a pergunta sai com a linha do prazo, o solicitante é lembrado 24 h antes e,
no vencimento, a pergunta volta ao Principal como decisão pendente. O motor não fecha nada. O prazo só vence onde o aviso chega
por caminho que o motor controla (o Balcão); card de outra origem fica sem prazo, com o motivo registrado.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review


class _Deadline:
    """O módulo do prazo, carregado no uso: na árvore sem a mudança o teste falha no que afirma, não na coleta."""

    def __getattr__(self, name):
        from hermes_cli import nfos_requester_deadline
        return getattr(nfos_requester_deadline, name)


deadline = _Deadline()

HOUR = 3600
DAY = 24 * HOUR
TO = "solicitante do chamado CS-0021"
QUESTION = "Pode enviar um vídeo curto da tela com o erro?"
PORTAL = {"platform": "portal", "chat_id": "concursa", "thread_id": "suporte", "chat_type": "portal", "user_id": "portal:concursa:cs"}
TELEGRAM = {"platform": "telegram", "chat_id": "-10001", "thread_id": "8"}
REPLY = {"platform": "portal", "actor": "CS Concursa", "ticket": "CS-0021"}


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


@pytest.fixture
def worker():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _card(conn, worker, origin=PORTAL, message_id="CS-0021"):
    """Card com uma decisão pendente do Principal e o worker já fora do ar, como fica quando a pergunta vai ao solicitante."""
    rid = delivery.receive_request(conn, source=dict(origin, message_id=message_id), text="Simulado com disciplina errada.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    if origin.get("platform") == "portal":  # chamado do Balcão já aprovado para execução, como nos quadros de produção
        payload = json.loads(delivery.get_request(conn, rid)["payload"])
        payload["support_approval"] = {"required": True, "approved_at": 1790993604, "approved_by": "Maikol",
                                       "decided_at": 1790993604, "decided_by": "Maikol", "decision": "approved"}
        conn.execute("UPDATE nfos_requests SET payload=? WHERE id=?", (json.dumps(payload), rid))
        conn.commit()
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker.pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="Falta o vídeo da tela para localizar a disciplina.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    if worker.poll() is None:
        worker.kill()
        worker.wait()
    return kb.get_task(conn, task.id), decision


def _ask(conn, decision, text=QUESTION, to=TO):
    # O mesmo formato que o `decide --resolution human` grava: a pergunta e o destinatário abrem a resposta.
    delivery.resolve_decision(conn, decision, action="human", author="Principal", answer=f"PERGUNTA para {to}: {text}" + chr(10) + "Contexto interno.",
                              public_message={"kind": "question", "text": text, "to": to})


def _context(conn, decision):
    return json.loads(delivery.get_decision(conn, decision)["context"])


def _rewrite(conn, decision, change):
    context = _context(conn, decision)
    change(context)
    conn.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(context), decision))
    conn.commit()


def _events(conn, task_id, kind):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, kind))]


def _asked(conn, worker, **kwargs):
    task, decision = _card(conn, worker, **kwargs)
    _ask(conn, decision)
    return task, decision, _context(conn, decision)["requester_deadline"]


def _expired(conn, worker):
    task, decision, clock = _asked(conn, worker)
    deadline.sweep(conn, now=clock["warn_at"])
    (action,) = deadline.sweep(conn, now=clock["due_at"])
    assert action["action"] == "expire"
    successor = _context(conn, decision)["superseded_by"]
    return task, decision, successor


def test_question_to_the_requester_leaves_with_the_deadline_line(board, worker):
    from hermes_cli import nfos_public_text
    task, decision, clock = _asked(board, worker)
    text = _context(board, decision)["public_message"]["text"]
    assert text.startswith(QUESTION + "\n\n")
    assert "Se não recebermos sua resposta até" in text and "fechamos como entrega parcial" in text
    assert "Se você responder depois, o chamado reabre." in text
    assert clock["due_at"] == clock["asked_at"] + 48 * HOUR and clock["warn_at"] == clock["due_at"] - DAY
    assert clock["channel"] == "portal" and clock["question_text"] == QUESTION
    assert nfos_public_text.form_problems(text, question=True) == []


def test_the_deadline_line_states_the_time_in_the_project_timezone():
    from zoneinfo import ZoneInfo
    due = int(datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc).timestamp())
    assert deadline.notice(due, ZoneInfo("America/Sao_Paulo")).startswith("Se não recebermos sua resposta até 12/10 às 04:00,")


def test_nothing_changes_before_the_reminder(board, worker):
    task, decision, clock = _asked(board, worker)
    before = _context(board, decision)
    (action,) = deadline.sweep(board, now=clock["warn_at"] - 1)
    assert action["action"] == "waiting" and action["due_at"] == clock["due_at"]
    assert _context(board, decision) == before


def test_the_requester_is_reminded_a_day_before_the_deadline(board, worker):
    task, decision, clock = _asked(board, worker)
    original = _context(board, decision)["public_message"]
    (action,) = deadline.sweep(board, now=clock["warn_at"])
    assert action["action"] == "warn" and action["due_at"] == clock["due_at"]
    context = _context(board, decision)
    assert context["public_message"]["created_at"] == clock["warn_at"]
    assert context["public_message"]["text"] == original["text"]
    assert context["public_message_history"] == [original]
    assert context["requester_deadline"]["warned_at"] == clock["warn_at"]
    assert delivery.get_decision(board, decision)["status"] == "human"
    assert _events(board, task.id, "nfos_requester_reminded") == [{"decision_id": decision, "due_at": clock["due_at"]}]


def test_at_the_deadline_the_question_returns_to_the_principal_and_nothing_is_closed(board, worker):
    task, decision, clock = _asked(board, worker)
    delivery.reconcile_human_answers(board)
    assert kb.get_task(board, task.id).status == "blocked"
    deadline.sweep(board, now=clock["warn_at"])
    assert deadline.sweep(board, now=clock["due_at"] - 1)[0]["action"] == "waiting"
    (action,) = deadline.sweep(board, now=clock["due_at"])
    assert action["action"] == "expire"
    old = delivery.get_decision(board, decision)
    old_context = json.loads(old["context"])
    assert old["status"] == "superseded" and old_context["superseded_reason"] == "requester_silence"
    successor = delivery.get_decision(board, old_context["superseded_by"])
    assert successor["status"] == "pending" and successor["kind"] == "impediment"
    assert "entrega parcial" in successor["question"] and QUESTION in successor["question"]
    assert json.loads(successor["context"])["supersedes"] == decision
    assert successor["id"] in [row["id"] for row in delivery.pending_decisions(board)]
    assert kb.get_task(board, task.id).status == "ready"
    (silence,) = _events(board, task.id, "nfos_requester_silence")
    assert silence["decision_id"] == decision and silence["new_decision_id"] == successor["id"]


def test_question_asked_before_the_rule_is_warned_first_and_expires_a_day_later(board, worker):
    task, decision, clock = _asked(board, worker)
    now = clock["asked_at"] + 9 * DAY

    def to_legacy(context):
        context.pop("requester_deadline")
        context["public_message"].update(text=QUESTION, created_at=clock["asked_at"])
    _rewrite(board, decision, to_legacy)
    (action,) = deadline.sweep(board, now=now)
    assert action["action"] == "warn" and action["due_at"] == now + DAY
    assert "Se não recebermos sua resposta até" in _context(board, decision)["public_message"]["text"]
    assert delivery.get_decision(board, decision)["status"] == "human"
    assert deadline.sweep(board, now=now + DAY - 1)[0]["action"] == "waiting"
    assert deadline.sweep(board, now=now + DAY)[0]["action"] == "expire"
    assert delivery.get_decision(board, decision)["status"] == "superseded"


def test_successor_keeps_the_maintenance_pause_obligation_of_the_question_it_replaces(board, worker):
    task, decision, clock = _asked(board, worker)
    obligation = {"pause_run_id": 7, "reason": "laboratório fora do ar", "resume_route": "resume-after-repair", "budget_mode": "per_run"}
    _rewrite(board, decision, lambda context: context.update(maintenance_recovery=obligation, impediment_identity={"probe": "C1"}))
    deadline.sweep(board, now=clock["warn_at"])
    deadline.sweep(board, now=clock["due_at"])
    successor = _context(board, _context(board, decision)["superseded_by"])
    assert successor["maintenance_recovery"] == obligation and successor["impediment_identity"] == {"probe": "C1"}
    assert not {"public_message", "public_message_history", "requester_deadline", "human_reply"} & set(successor)
    assert board.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status='pending' "
                         "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=7", (task.id,)).fetchone()[0] == 1


def test_card_from_another_origin_has_no_deadline_and_says_why(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="42")
    _ask(board, decision, to="Jhonatan")
    context = _context(board, decision)
    assert context["public_message"]["text"] == QUESTION
    assert context["requester_deadline"]["unarmed"] == "no_warning_channel" and "due_at" not in context["requester_deadline"]
    (action,) = deadline.sweep(board, now=int(time.time()) + 30 * DAY)
    assert action["action"] == "no_warning_channel" and action["channel"] == "telegram" and action["recipient"] == "Jhonatan"
    assert delivery.get_decision(board, decision)["status"] == "human"


def test_older_question_without_a_public_message_is_recognised_by_its_addressee(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="43")
    _ask(board, decision, to="Jhonatan")

    def to_legacy(context):
        context.pop("public_message")
        context.pop("requester_deadline")
    _rewrite(board, decision, to_legacy)
    board.execute("UPDATE nfos_decisions SET answer=? WHERE id=?", ("PERGUNTA para Jhonatan: qual competência conferir?\nContexto interno.", decision))
    board.commit()
    for _ in range(2):
        (action,) = deadline.sweep(board, now=int(time.time()) + 17 * DAY)
        assert action["action"] == "no_warning_channel" and action["recipient"] == "Jhonatan"
    assert len(_events(board, task.id, "nfos_requester_deadline_unarmed")) == 1
    assert deadline.requester({}, "PERGUNTA para Maikol: autoriza publicar?") == ""


def test_question_to_the_owner_is_left_alone(board, worker):
    task, decision, clock = _asked(board, worker)
    _rewrite(board, decision, lambda context: context["public_message"].update(to="Maikol"))
    assert deadline.sweep(board, now=clock["due_at"] + 30 * DAY) == []
    assert delivery.get_decision(board, decision)["status"] == "human"


def test_an_answer_before_the_deadline_follows_the_usual_path(board, worker):
    task, decision, clock = _asked(board, worker)
    delivery.reconcile_human_answers(board)
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo da tela.", source=dict(REPLY, message_id="m-1"))
    assert delivery.get_decision(board, decision)["status"] == "pending"
    assert deadline.sweep(board, now=clock["due_at"] + DAY) == []
    assert not _events(board, task.id, "nfos_requester_silence")


def test_second_question_after_a_silence_is_refused_until_the_requester_answers(board, worker):
    task, decision, successor = _expired(board, worker)
    with pytest.raises(delivery.WorkflowError, match="sem resposta até o prazo"):
        _ask(board, successor, text="Consegue mandar o vídeo agora?")
    assert delivery.get_decision(board, successor)["status"] == "pending"
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo.", source=dict(REPLY, message_id="m-2"))
    _ask(board, successor, text="O erro aparece em todas as disciplinas?")
    assert delivery.get_decision(board, successor)["status"] == "human"


def test_late_answer_on_an_open_card_reaches_the_principal(board, worker):
    task, decision, successor = _expired(board, worker)
    source = dict(REPLY, message_id="m-3")
    assert delivery.resume_after_answer(board, task.id, answer="Desculpe a demora, segue o vídeo.", source=source) is False
    context = _context(board, successor)
    assert context["human_reply"]["answer"] == "Desculpe a demora, segue o vídeo." and context["human_reply"]["source"] == source
    assert delivery.get_decision(board, successor)["status"] == "pending"
    delivery.resume_after_answer(board, task.id, answer="Desculpe a demora, segue o vídeo.", source=source)
    assert len(_events(board, task.id, "nfos_late_requester_answer")) == 1


def test_late_answer_after_the_principal_already_decided_opens_a_decision_with_it(board, worker):
    task, decision, successor = _expired(board, worker)
    delivery.resolve_decision(board, successor, action="continue", author="Principal", answer="CONTINUE: seguir com o que temos.")
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo.", source=dict(REPLY, message_id="m-4"))
    (late,) = [row for row in delivery.pending_decisions(board) if row["task_id"] == task.id]
    context = json.loads(late["context"])
    assert context["late_requester_answer"] is True and context["human_reply"]["answer"] == "Segue o vídeo."
    assert "respondeu depois do prazo" in late["question"]


def test_late_answer_on_a_closed_card_keeps_the_existing_route(board, worker):
    task, decision, successor = _expired(board, worker)
    board.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), task.id))
    board.commit()
    with pytest.raises(delivery.WorkflowError, match="No pending human question"):
        delivery.resume_after_answer(board, task.id, answer="Segue o vídeo.", source=dict(REPLY, message_id="m-5"))
    assert not _events(board, task.id, "nfos_late_requester_answer")
    assert "human_reply" not in _context(board, successor)


def test_read_only_run_reports_what_it_would_do_and_writes_nothing(board, worker):
    task, decision, clock = _asked(board, worker)
    deadline.sweep(board, now=clock["warn_at"])
    before = (_context(board, decision), board.execute("SELECT count(*) FROM task_events").fetchone()[0],
              board.execute("SELECT count(*) FROM nfos_decisions").fetchone()[0])
    (action,) = deadline.sweep(board, now=clock["due_at"], dry_run=True)
    assert action["action"] == "expire" and action["task_id"] == task.id
    assert before == (_context(board, decision), board.execute("SELECT count(*) FROM task_events").fetchone()[0],
                      board.execute("SELECT count(*) FROM nfos_decisions").fetchone()[0])
    assert delivery.get_decision(board, decision)["status"] == "human"


def test_the_deadline_comes_from_one_global_key_and_zero_turns_it_off(board, worker, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "requester_answer_hours": 12})
    task, decision, clock = _asked(board, worker)
    assert clock["due_at"] == clock["asked_at"] + 12 * HOUR and clock["warn_at"] == clock["due_at"] - 6 * HOUR
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "requester_answer_hours": 0})
    assert deadline.sweep(board, now=clock["due_at"] + DAY) == []
    other, second = _card(board, worker, message_id="CS-0022")
    _ask(board, second)
    assert _context(board, second)["public_message"]["text"] == QUESTION and "requester_deadline" not in _context(board, second)


def test_the_runtime_tick_applies_the_deadline(board, worker):
    task, decision, clock = _asked(board, worker)
    past = int(time.time()) - HOUR

    def already_warned(context):
        context["requester_deadline"].update(warned_at=past - DAY, warn_at=past - DAY, due_at=past)
    _rewrite(board, decision, already_warned)
    delivery.reconcile_human_answers(board)
    assert delivery.get_decision(board, decision)["status"] == "superseded"
    assert kb.get_task(board, task.id).status == "ready"


def test_principal_instructions_state_the_deadline_and_what_follows_it():
    from hermes_cli.nfos_runtime import principal_instructions
    text = principal_instructions()
    assert "kanban.delivery.requester_answer_hours" in text and "partial delivery" in text
