"""REQUESTER_DEADLINE_20261010 e REQUESTER_WAIT_20261014: a pergunta ao solicitante é uma espera do registro, com aviso e prazo.

Maikol, 10/10/2026: "3. Mande colocar um prazo" e "O NFOS tem que ser autonomo, eu não quero ser babá de IA." O card esperava para
sempre: em 10/10 havia card parado há 17 dias, e de 10/10 a 14/10 uma chave em zero desligou o prazo de todos os projetos sem um
evento sequer. Agora a pergunta é uma linha de `nfos_waits`: o solicitante é avisado pelo caminho de origem do card 24 h antes e,
no prazo, o motor segue sozinho como entrega parcial. Sem aviso entregue ninguém vence: a pergunta vai ao Principal uma vez.
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
from hermes_cli import nfos_waits


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


def _tick(conn, now):
    """O que o tick do runtime faz com a pergunta: o registro dá o passo das esperas na hora e a varredura adota as novas."""
    stepped = nfos_waits.sweep(conn, now=now)
    deadline.sweep(conn, now=now)
    return stepped


def _wait(conn, decision):
    if not nfos_waits._table(conn):
        return None
    row = conn.execute("SELECT id FROM nfos_waits WHERE decision_id=? AND reason='requester_answer' ORDER BY created_at DESC,id LIMIT 1",
                       (decision,)).fetchone()
    return nfos_waits.get(conn, row[0]) if row else None


def _asked(conn, worker, **kwargs):
    """Pergunta publicada e adotada pelo registro no tick seguinte."""
    task, decision = _card(conn, worker, **kwargs)
    _ask(conn, decision)
    seed = _context(conn, decision)["requester_deadline"]
    deadline.sweep(conn, now=seed["asked_at"])
    return task, decision, _wait(conn, decision)


def _expired(conn, worker):
    task, decision, wait = _asked(conn, worker)
    _tick(conn, wait["next_check_at"])
    _tick(conn, wait["deadline_at"])
    old = _context(conn, decision)
    assert old["superseded_reason"] == "requester_silence"
    return task, decision, old["superseded_by"]


def _receipt(conn, task_id, decision, due_at):
    """O recibo que o notificador do gateway grava quando publica o aviso no tópico do card."""
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "nfos_requester_reminder_delivered",
                         {"decision_id": decision, "due_at": due_at, "message_id": "77", "platform": "telegram",
                          "chat_id": "-10001", "thread_id": "8"})


def test_question_to_the_requester_leaves_with_the_deadline_line(board, worker):
    from hermes_cli import nfos_public_text
    task, decision = _card(board, worker)
    _ask(board, decision)
    text = _context(board, decision)["public_message"]["text"]
    seed = _context(board, decision)["requester_deadline"]
    assert text.startswith(QUESTION + "\n\n")
    assert "Se não recebermos sua resposta até" in text and "fechamos como entrega parcial" in text
    assert "Se você responder depois, o chamado reabre." in text
    assert seed["due_at"] == seed["asked_at"] + 48 * HOUR
    assert seed["channel"] == "portal" and seed["question_text"] == QUESTION
    assert nfos_public_text.form_problems(text, question=True) == []


def test_the_deadline_line_states_the_time_in_the_project_timezone():
    from zoneinfo import ZoneInfo
    due = int(datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc).timestamp())
    assert deadline.notice(due, ZoneInfo("America/Sao_Paulo")).startswith("Se não recebermos sua resposta até 12/10 às 04:00,")


def test_the_question_is_a_wait_of_the_registry_with_the_requester_as_executor(board, worker):
    task, decision = _card(board, worker)
    _ask(board, decision)
    seed = _context(board, decision)["requester_deadline"]
    (adopted,) = deadline.sweep(board, now=seed["asked_at"])
    assert adopted["action"] == "adopt" and adopted["decision_id"] == decision
    wait = _wait(board, decision)
    assert wait["status"] == "open" and wait["reason"] == "requester_answer" and wait["decision_id"] == decision
    assert wait["executor"] == {"kind": "requester", "name": TO}
    assert wait["predicate"] == {"kind": "human_reply", "decision_id": decision}
    assert wait["deadline_at"] == seed["due_at"] and wait["next_check_at"] == seed["due_at"] - DAY
    (declared,) = _events(board, task.id, "nfos_wait_declared")
    assert declared["wait_id"] == wait["id"] and declared["reason"] == "requester_answer"
    assert deadline.sweep(board, now=seed["asked_at"] + 60) == []  # a mesma pergunta não ganha segunda espera
    assert [w["id"] for w in nfos_waits.card_waits(board, task.id)] == [wait["id"]]


def test_nothing_changes_before_the_reminder(board, worker):
    task, decision, wait = _asked(board, worker)
    before = _context(board, decision)
    assert _tick(board, wait["next_check_at"] - 1) == []
    assert _context(board, decision) == before
    assert _wait(board, decision)["revision"] == wait["revision"]


def test_the_requester_is_reminded_a_day_before_the_deadline(board, worker):
    task, decision, wait = _asked(board, worker)
    original = _context(board, decision)["public_message"]
    warn_at, due = wait["next_check_at"], wait["deadline_at"]
    assert _tick(board, warn_at) == [(wait["id"], "wait")]
    context = _context(board, decision)
    assert context["public_message"]["created_at"] == warn_at
    assert context["public_message"]["text"] == original["text"]
    assert context["public_message_history"] == [original]
    assert delivery.get_decision(board, decision)["status"] == "human"
    assert _events(board, task.id, "nfos_requester_reminded") == [{"decision_id": decision, "due_at": due, "wait_id": wait["id"]}]
    stepped = _wait(board, decision)
    assert stepped["evidence"]["warned_at"] == warn_at and stepped["next_check_at"] == due and stepped["attempts"] == 1


def test_at_the_deadline_the_runtime_continues_the_card_as_a_partial_delivery_without_the_principal(board, worker):
    task, decision, wait = _asked(board, worker)
    delivery.reconcile_human_answers(board, lab_sweep=False)
    assert kb.get_task(board, task.id).status == "blocked"
    _tick(board, wait["next_check_at"])
    assert _tick(board, wait["deadline_at"] - 1) == []
    asked_principal = len(_events(board, task.id, "nfos_principal_requested"))
    assert _tick(board, wait["deadline_at"]) == [(wait["id"], "expired")]
    expired = _wait(board, decision)
    assert expired["status"] == "expired" and expired["outcome"] == "returned_to_worker"
    assert expired["evidence"]["expired_because"] == "deadline"
    old = delivery.get_decision(board, decision)
    old_context = json.loads(old["context"])
    assert old["status"] == "superseded" and old_context["superseded_reason"] == "requester_silence"
    successor = delivery.get_decision(board, old_context["superseded_by"])
    assert successor["status"] == "resolved" and successor["action"] == "continue" and successor["kind"] == "impediment"
    assert "entrega parcial" in successor["answer"] and "continuation" in successor["answer"] and QUESTION in successor["answer"]
    carried = json.loads(successor["context"])
    assert carried["supersedes"] == decision
    assert carried["wait_rule"] == {"wait_id": wait["id"], "resolved_by": "runtime", "outcome": "returned_to_worker",
                                    "at": wait["deadline_at"]}
    assert carried["requester_silence"]["due_at"] == wait["deadline_at"]
    assert [row for row in delivery.pending_decisions(board) if row["task_id"] == task.id] == []
    assert len(_events(board, task.id, "nfos_principal_requested")) == asked_principal  # o Principal não é acordado
    assert kb.get_task(board, task.id).status == "ready"
    (silence,) = _events(board, task.id, "nfos_requester_silence")
    assert silence["decision_id"] == decision and silence["new_decision_id"] == successor["id"] and silence["why"] == "deadline"


def test_question_asked_before_the_rule_is_adopted_warned_and_expires_a_day_later(board, worker):
    task, decision = _card(board, worker)
    _ask(board, decision)
    asked = _context(board, decision)["requester_deadline"]["asked_at"]
    now = asked + 9 * DAY

    def to_legacy(context):
        context.pop("requester_deadline")
        context["public_message"].update(text=QUESTION, created_at=asked)
    _rewrite(board, decision, to_legacy)
    _tick(board, now)  # adotada: o aviso já passou da hora
    _tick(board, now)
    wait = _wait(board, decision)
    assert wait["created_at"] == asked and wait["evidence"]["warned_at"] == now and wait["deadline_at"] == now + DAY
    assert "Se não recebermos sua resposta até" in _context(board, decision)["public_message"]["text"]
    assert delivery.get_decision(board, decision)["status"] == "human"
    assert _tick(board, now + DAY - 1) == []
    assert _tick(board, now + DAY) == [(wait["id"], "expired")]
    assert delivery.get_decision(board, decision)["status"] == "superseded"


def _client_side(conn, task_id, decision):
    """Tudo o que pode chegar a quem pediu: a mensagem pública da decisão (Balcão) e os pedidos de aviso ao gateway (chat)."""
    context = _context(conn, decision)
    return (context.get("public_message"), context.get("public_message_history"),
            _events(conn, task_id, "nfos_requester_reminder"), _events(conn, task_id, "nfos_requester_reminded"))


@pytest.mark.parametrize("origin", ["portal", "telegram"])
def test_question_already_warned_by_the_old_clock_is_not_warned_again(board, worker, origin):
    """Em 14/10 às 16:08Z o relógio antigo avisou quatro perguntas de produção: uma no Balcão (t_ac820bc8) e três no Telegram
    (t_f7e2059e, t_57b12eeb, t_b52998e4), com o prazo de 15/10 16:08Z. Adotadas, nada mais sai para o cliente até o prazo."""
    source = PORTAL if origin == "portal" else TELEGRAM
    task, decision = _card(board, worker, origin=source, message_id="46")
    _ask(board, decision, to=TO if origin == "portal" else "Lucas")
    asked = _context(board, decision)["requester_deadline"]["asked_at"]
    warned, due = asked - 7 * HOUR, asked + 17 * HOUR
    old = {"asked_at": asked - 5 * DAY, "hours": 48, "channel": origin, "due_at": due, "warned_at": warned}
    if origin == "portal":  # o formato que o _warn antigo gravou, com a pergunta republicada e a linha do prazo
        old.update(warn_at=warned, question_text=QUESTION)
    else:  # o formato que o _topic antigo gravou depois do recibo
        old.update(reminder={"attempts": 1, "due_at": due, "requested_at": warned - 6}, reminder_message_id="21430")
    _rewrite(board, decision, lambda context: context.update(requester_deadline=old))
    before = _client_side(board, task.id, decision)
    _tick(board, asked + HOUR)  # a release nova entra: a pergunta é adotada
    wait = _wait(board, decision)
    assert wait["next_check_at"] == due and wait["deadline_at"] == due and wait["evidence"]["warned_at"] == warned
    for now in (asked + 2 * HOUR, asked + 9 * HOUR, due - 1):
        assert _tick(board, now) == []
    assert _client_side(board, task.id, decision) == before
    assert _tick(board, due) == [(wait["id"], "expired")]
    assert _events(board, task.id, "nfos_requester_reminder") == before[2]
    assert _events(board, task.id, "nfos_requester_reminded") == before[3]


def test_question_asked_while_the_key_was_zero_is_warned_once_with_the_deadline_line(board, worker):
    """t_1f500f9f (concursa-ai, CS Concursa): perguntada em 14/10 08:41Z com a chave em 0, sem linha do prazo e sem registro.
    Adotada às 16:23Z, ela é avisada uma vez, 24 h antes do prazo de 48 h, com a linha; e não de novo até vencer."""
    task, decision = _card(board, worker)
    _ask(board, decision, to="CS Concursa")
    asked = _context(board, decision)["requester_deadline"]["asked_at"]

    def asked_with_the_key_at_zero(context):
        context.pop("requester_deadline")
        context["public_message"].update(text=QUESTION, created_at=asked)
    _rewrite(board, decision, asked_with_the_key_at_zero)
    _tick(board, asked + 7 * HOUR + 42 * 60)
    wait = _wait(board, decision)
    assert wait["next_check_at"] == asked + DAY and wait["deadline_at"] == asked + 2 * DAY
    assert _tick(board, asked + DAY - 1) == [] and _events(board, task.id, "nfos_requester_reminded") == []
    assert _tick(board, asked + DAY) == [(wait["id"], "wait")]
    context = _context(board, decision)
    due = asked + 2 * DAY
    assert context["public_message"]["text"] == QUESTION + "\n\n" + deadline.notice(due, deadline._zone(board, task.id))
    assert context["public_message"]["created_at"] == asked + DAY
    assert [m["text"] for m in context["public_message_history"]] == [QUESTION]
    for now in (asked + DAY + HOUR, asked + DAY + 12 * HOUR, due - 1):
        assert _tick(board, now) == []
    assert _events(board, task.id, "nfos_requester_reminded") == [{"decision_id": decision, "due_at": due, "wait_id": wait["id"]}]
    assert len(_context(board, decision)["public_message_history"]) == 1


def test_every_warning_is_kept_where_the_previous_release_reads_it(board, worker):
    """PREVIOUS_RELEASE_RECORD_20261014: numa volta de pacote, o relógio antigo lê context.requester_deadline. O aviso que a
    espera deu fica lá, no formato dele, e o relógio antigo não avisa o cliente de novo."""
    task, decision, wait = _asked(board, worker)
    _tick(board, wait["next_check_at"])
    record = _context(board, decision)["requester_deadline"]
    assert record["warned_at"] == record["warn_at"] == wait["next_check_at"] and record["due_at"] == wait["deadline_at"]
    assert record["asked_at"] == wait["created_at"] and record["channel"] == "portal" and record["question_text"] == QUESTION
    chat, asked_chat = _card(board, worker, origin=TELEGRAM, message_id="50")
    _ask(board, asked_chat, to="Jhonatan")
    start = _context(board, asked_chat)["requester_deadline"]["asked_at"] + DAY
    deadline.sweep(board, now=start - 1)
    _tick(board, start)
    (requested,) = _events(board, chat.id, "nfos_requester_reminder")
    pending = _context(board, asked_chat)["requester_deadline"]
    assert pending["reminder"] == {"requested_at": start, "due_at": requested["due_at"], "attempts": 1} and "warned_at" not in pending
    _receipt(board, chat.id, asked_chat, requested["due_at"])
    _tick(board, start + HOUR)
    delivered = _context(board, asked_chat)["requester_deadline"]
    assert delivered["warned_at"] and delivered["due_at"] == requested["due_at"] and delivered["reminder_message_id"] == "77"


@pytest.mark.parametrize("origin", ["portal", "telegram"])
def test_at_the_deadline_nothing_new_reaches_the_client_and_the_card_gets_the_criterion(board, worker, origin):
    """No prazo o motor não escreve ao cliente: o aviso já disse que o pedido segue como entrega parcial. O texto que ele lê
    depois é a entrega do card (public_delivery), conferida na revisão final. O card leva o critério para a continuação."""
    source = PORTAL if origin == "portal" else TELEGRAM
    to = TO if origin == "portal" else "Jhonatan"
    task, decision = _card(board, worker, origin=source, message_id="51")
    _ask(board, decision, to=to)
    if origin == "telegram":
        start = _context(board, decision)["requester_deadline"]["asked_at"] + DAY
        deadline.sweep(board, now=start - 1)
        _tick(board, start)
        (requested,) = _events(board, task.id, "nfos_requester_reminder")
        _receipt(board, task.id, decision, requested["due_at"])
        _tick(board, start + HOUR)
        due = requested["due_at"]
    else:
        _tick(board, _context(board, decision)["requester_deadline"]["asked_at"])
        wait = _wait(board, decision)
        _tick(board, wait["next_check_at"])
        due = wait["deadline_at"]
    before = _client_side(board, task.id, decision)
    _tick(board, due)
    assert _client_side(board, task.id, decision) == before
    successor = delivery.get_decision(board, _context(board, decision)["superseded_by"])
    carried = json.loads(successor["context"])
    assert "public_message" not in carried
    criterion = f'Concluir o que ficou pendente quando chegar a resposta à pergunta feita a {to}: "{QUESTION}"'
    assert carried["continuation_criterion"] == criterion
    assert successor["answer"].startswith("CONTINUE (runtime do NFOS): feche como entrega parcial (partial_delivery=true) com o "
                                          f'que já está conferido; o card de continuação (continuation) leva o critério obrigatório "{criterion}".')
    # O worker relançado recebe a resposta pelo contexto da retomada, cortada em 600 caracteres: o critério chega inteiro.
    (answered, *_) = delivery._current_decisions_context(board, task.id)
    assert f'- {successor["id"]} (continue, ' in answered and criterion in answered


def test_the_criterion_reaches_the_worker_whole_even_for_a_long_question(board, worker):
    long_question = "Pode confirmar " + "qual disciplina, qual simulado e qual questão apresentam o erro de classificação, " * 6 + "?"
    task, decision = _card(board, worker)
    _ask(board, decision, text=long_question, to="solicitante do chamado CS-0021 (Central de Suporte Concursa)")
    _tick(board, _context(board, decision)["requester_deadline"]["asked_at"])
    wait = _wait(board, decision)
    _tick(board, wait["next_check_at"])
    _tick(board, wait["deadline_at"])
    successor = json.loads(delivery.get_decision(board, _context(board, decision)["superseded_by"])["context"])
    (answered, *_) = delivery._current_decisions_context(board, task.id)
    assert successor["continuation_criterion"] in answered


def test_successor_keeps_the_maintenance_pause_obligation_of_the_question_it_replaces(board, worker):
    task, decision, wait = _asked(board, worker)
    pause_run = delivery.get_decision(board, decision)["run_id"]
    board.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                  (json.dumps({"maintenance_pause": {"reason": "laboratório fora do ar", "repaired_at": None}}), pause_run))
    board.commit()
    obligation = {"pause_run_id": pause_run, "reason": "laboratório fora do ar", "resume_route": "resume-after-repair",
                  "budget_mode": "per_run"}
    _rewrite(board, decision, lambda context: context.update(maintenance_recovery=obligation, impediment_identity={"probe": "C1"}))
    _tick(board, wait["next_check_at"])
    _tick(board, wait["deadline_at"])
    successor_id = _context(board, decision)["superseded_by"]
    successor = _context(board, successor_id)
    assert successor["maintenance_recovery"] == obligation and successor["impediment_identity"] == {"probe": "C1"}
    assert not {"public_message", "public_message_history", "requester_deadline", "human_reply"} & set(successor)
    # A pausa segue sem reparo: o continue do motor fica com a obrigação, pendente para o Principal, e o card segue bloqueado.
    assert delivery.get_decision(board, successor_id)["status"] == "pending"
    assert kb.get_task(board, task.id).status == "blocked"


def test_card_born_in_a_chat_counts_the_deadline_only_from_the_receipt_of_its_reminder(board, worker):
    from hermes_cli import nfos_public_text
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="42")
    _ask(board, decision, to="Jhonatan")
    context = _context(board, decision)
    seed = context["requester_deadline"]
    assert context["public_message"]["text"] == QUESTION  # no grupo quem escreve a pergunta é o Principal
    assert seed["channel"] == "telegram" and "due_at" not in seed
    warn_at = seed["asked_at"] + DAY
    assert _tick(board, warn_at - 1) == []
    wait = _wait(board, decision)
    assert wait["next_check_at"] == warn_at
    _tick(board, warn_at)
    (event,) = _events(board, task.id, "nfos_requester_reminder")
    assert event["decision_id"] == decision and event["due_at"] == seed["asked_at"] + 48 * HOUR
    assert event["origin"] == {"platform": "telegram", "chat_id": "-10001", "thread_id": "8"} and event["wait_id"] == wait["id"]
    assert event["text"].startswith(f"PERGUNTA para Jhonatan: {QUESTION}\n\nSe não recebermos sua resposta até")
    assert event["text"].endswith("Se você responder depois, retomamos o pedido.")
    assert nfos_public_text.form_problems(event["text"], question=True) == []
    # Sem recibo o prazo não conta, nem depois da data escrita no aviso.
    assert _tick(board, warn_at + 600) == []
    assert delivery.get_decision(board, decision)["status"] == "human"
    # Aviso que o gateway não entregou em uma hora é pedido de novo, com data nova.
    later = event["due_at"] + HOUR
    _tick(board, later)
    again = _events(board, task.id, "nfos_requester_reminder")[-1]
    assert len(_events(board, task.id, "nfos_requester_reminder")) == 2 and again["due_at"] == later + DAY
    assert delivery.get_decision(board, decision)["status"] == "human"
    _receipt(board, task.id, decision, again["due_at"])
    _tick(board, later + HOUR)
    (reminded,) = _events(board, task.id, "nfos_requester_reminded")
    assert reminded["due_at"] == again["due_at"] and reminded["message_id"] == "77"
    warned = _wait(board, decision)
    assert warned["evidence"]["due_at"] == again["due_at"] and warned["deadline_at"] == again["due_at"]
    assert _tick(board, again["due_at"] - 1) == []
    assert _tick(board, again["due_at"]) == [(wait["id"], "expired")]
    assert delivery.get_decision(board, decision)["status"] == "superseded"


def _never_confirmed(conn, worker, message_id):
    task, decision = _card(conn, worker, origin=TELEGRAM, message_id=message_id)
    _ask(conn, decision, to="Jhonatan")
    start = _context(conn, decision)["requester_deadline"]["asked_at"] + DAY
    deadline.sweep(conn, now=start - 1)
    for n in range(4):
        _tick(conn, start + n * HOUR)
    return task, decision, start


def test_reminder_never_confirmed_goes_to_the_principal_once_and_nobody_expires_unwarned(board, worker):
    """REMINDER_LIMIT_20261010: sem recibo o motor não sabe se a mensagem saiu. Pedir de hora em hora para sempre viraria a
    mesma mensagem repetida no grupo de quem pediu; e sem aviso o prazo não corre."""
    task, decision, start = _never_confirmed(board, worker, "44")
    assert len(_events(board, task.id, "nfos_requester_reminder")) == 3
    expired = _wait(board, decision)
    assert expired["status"] == "expired" and expired["outcome"] == "principal_handoff"
    assert expired["evidence"]["unarmed"] == "reminder_undelivered"
    old = _context(board, decision)
    assert old["superseded_reason"] == "requester_unreached"
    successor = delivery.get_decision(board, old["superseded_by"])
    assert successor["status"] == "pending" and "não chegou ao tópico de origem" in successor["question"]
    assert successor["id"] in [row["id"] for row in delivery.pending_decisions(board)]
    (unreached,) = _events(board, task.id, "nfos_requester_unreached")
    assert unreached["decision_id"] == decision and unreached["why"] == "reminder_undelivered"
    assert _events(board, task.id, "nfos_requester_silence") == []
    reminded = nfos_waits.open_for_decision(board, successor["id"])
    assert reminded["reason"] == "principal_decision" and reminded["origin"]["expired_reason"] == "requester_answer"
    assert kb.get_task(board, task.id).status == "ready"
    _tick(board, start + 60 * DAY)
    assert len(_events(board, task.id, "nfos_requester_reminder")) == 3


def test_second_question_without_a_delivered_reminder_continues_by_itself(board, worker):
    task, decision, start = _never_confirmed(board, worker, "47")
    successor = _context(board, decision)["superseded_by"]
    _ask(board, successor, to="Jhonatan")  # perguntar de novo é permitido uma vez
    restart = _context(board, successor)["requester_deadline"]["asked_at"] + DAY
    deadline.sweep(board, now=restart - 1)
    for n in range(4):
        _tick(board, restart + n * HOUR)
    second = _wait(board, successor)
    assert second["status"] == "expired" and second["outcome"] == "returned_to_worker"
    assert second["evidence"]["expired_because"] == "unreached_again"
    third = delivery.get_decision(board, _context(board, successor)["superseded_by"])
    assert third["status"] == "resolved" and third["action"] == "continue"
    assert json.loads(third["context"])["wait_rule"]["resolved_by"] == "runtime"
    assert len(_events(board, task.id, "nfos_requester_unreached")) == 1
    assert _events(board, task.id, "nfos_requester_silence")[0]["why"] == "unreached_again"


def test_receipt_of_the_last_request_still_starts_the_deadline(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="45")
    _ask(board, decision, to="Jhonatan")
    start = _context(board, decision)["requester_deadline"]["asked_at"] + DAY
    deadline.sweep(board, now=start - 1)
    for n in range(3):
        _tick(board, start + n * HOUR)
    assert _wait(board, decision)["evidence"]["reminder"]["attempts"] == 3
    _receipt(board, task.id, decision, _events(board, task.id, "nfos_requester_reminder")[-1]["due_at"])
    _tick(board, start + 3 * HOUR)
    assert len(_events(board, task.id, "nfos_requester_reminded")) == 1
    assert _wait(board, decision)["status"] == "open" and _events(board, task.id, "nfos_requester_unreached") == []


def test_older_question_without_a_public_message_is_reminded_by_its_addressee(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="43")
    _ask(board, decision, to="Jhonatan")

    def to_legacy(context):
        context.pop("public_message")
        context.pop("requester_deadline")
    _rewrite(board, decision, to_legacy)
    board.execute("UPDATE nfos_decisions SET answer=? WHERE id=?", ("PERGUNTA para Jhonatan: qual competência conferir?\nContexto interno.", decision))
    board.commit()
    now = int(time.time()) + 17 * DAY
    _tick(board, now)
    _tick(board, now)
    (event,) = _events(board, task.id, "nfos_requester_reminder")
    assert event["recipient"] == "Jhonatan" and event["due_at"] == now + DAY
    assert event["text"].startswith("PERGUNTA para Jhonatan: qual competência conferir?\n\nSe não recebermos")
    assert "Contexto interno" not in event["text"]
    assert deadline.requester({}, "PERGUNTA para Maikol: autoriza publicar?") == ""


def test_old_question_out_of_form_is_not_published_again():
    from zoneinfo import ZoneInfo
    from hermes_cli import nfos_public_text
    due = int(datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp())
    text = deadline.topic_reminder("Jhonatan", "Qual competência " + chr(0x2014) + " a de agosto?", due, ZoneInfo("America/Sao_Paulo"))
    assert text.startswith("Jhonatan, a pergunta deste pedido segue sem resposta.\n\nSe não recebermos sua resposta até 12/10 às 09:00,")
    assert nfos_public_text.form_problems(text, question=True) == []
    with_id = deadline.topic_reminder("Jhonatan", "PERGUNTA para Jhonatan (7550030839): qual servidor usar?", due, ZoneInfo("America/Sao_Paulo"))
    assert with_id.startswith("PERGUNTA para Jhonatan: qual servidor usar?\n\n")


def _without_origin(conn, task_id):
    request = delivery.get_workflow(conn, task_id)["request_id"]
    payload = json.loads(delivery.get_request(conn, request)["payload"])
    payload["source"] = {"platform": "cli"}
    conn.execute("UPDATE nfos_requests SET payload=? WHERE id=?", (json.dumps(payload), request))
    conn.commit()


def test_card_without_a_desk_or_a_chat_cannot_ask_the_requester(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="48")
    _without_origin(board, task.id)
    with pytest.raises(delivery.WorkflowError, match="não tem por onde perguntar ao solicitante"):
        _ask(board, decision, to="Jhonatan")
    assert delivery.get_decision(board, decision)["status"] == "pending"


def test_old_question_on_a_card_without_a_channel_goes_to_the_principal_once(board, worker):
    task, decision = _card(board, worker, origin=TELEGRAM, message_id="49")
    _ask(board, decision, to="Jhonatan")
    _rewrite(board, decision, lambda context: context.pop("requester_deadline"))
    _without_origin(board, task.id)
    now = int(time.time()) + 30 * DAY
    for _ in range(3):
        _tick(board, now)
    (unreached,) = _events(board, task.id, "nfos_requester_unreached")
    assert unreached["why"] == "no_warning_channel" and unreached["channel"] == "cli"
    successor = _context(board, decision)["superseded_by"]
    assert delivery.get_decision(board, successor)["status"] == "pending"
    with pytest.raises(delivery.WorkflowError, match="não tem por onde perguntar"):
        _ask(board, successor, to="Jhonatan")


def test_question_to_the_owner_is_left_alone(board, worker):
    task, decision = _card(board, worker)
    _ask(board, decision)
    _rewrite(board, decision, lambda context: context["public_message"].update(to="Maikol"))
    assert deadline.sweep(board, now=int(time.time()) + 30 * DAY) == []
    assert _wait(board, decision) is None
    assert delivery.get_decision(board, decision)["status"] == "human"


def test_an_answer_before_the_deadline_follows_the_usual_path_and_satisfies_the_wait(board, worker):
    task, decision, wait = _asked(board, worker)
    delivery.reconcile_human_answers(board, lab_sweep=False)
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo da tela.", source=dict(REPLY, message_id="m-1"))
    assert delivery.get_decision(board, decision)["status"] == "pending"
    now = wait["created_at"] + HOUR
    assert _wait(board, decision)["next_check_at"] <= now  # o tick da resposta acordou a espera, antes da hora marcada
    assert nfos_waits.sweep(board, now=now) == [(wait["id"], "satisfied")]
    assert _wait(board, decision)["status"] == "satisfied"
    assert _tick(board, wait["deadline_at"] + DAY) == []
    assert not _events(board, task.id, "nfos_requester_silence")


def test_question_the_principal_reconsiders_withdraws_its_wait(board, worker):
    task, decision, wait = _asked(board, worker)
    delivery.reconcile_human_answers(board, lab_sweep=False)
    delivery.reconsider_decision(board, decision, action="continue", reason="O vídeo não é necessário: o log basta.",
                                 answer="CONTINUE: seguir pelo log.")
    now = wait["created_at"] + HOUR
    _tick(board, now)
    assert nfos_waits.sweep(board, now=now) == [(wait["id"], "withdrawn")]
    assert _wait(board, decision)["outcome"] == "decision_closed"


def _pending(conn, task_id, run_id):
    """Uma decisão nova do card para o Principal, como a de um impedimento seguinte."""
    new_id = "dec_next_" + str(int(time.time() * 1000))
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (new_id, task_id, run_id, "impediment", "Falta outro dado do solicitante.", "{}",
                  delivery.get_workflow(conn, task_id)["spec_revision"], int(time.time())))
    conn.commit()
    return new_id


def test_second_question_after_a_silence_is_refused_until_the_requester_answers(board, worker):
    task, decision, successor = _expired(board, worker)
    following = _pending(board, task.id, delivery.get_decision(board, decision)["run_id"])
    with pytest.raises(delivery.WorkflowError, match="encerrada pelo motor sem resposta"):
        _ask(board, following, text="Consegue mandar o vídeo agora?")
    assert delivery.get_decision(board, following)["status"] == "pending"
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo.", source=dict(REPLY, message_id="m-2"))
    _ask(board, following, text="O erro aparece em todas as disciplinas?")
    assert delivery.get_decision(board, following)["status"] == "human"


def test_late_answer_on_an_open_card_reaches_the_principal_once(board, worker):
    task, decision, successor = _expired(board, worker)
    source = dict(REPLY, message_id="m-3")
    assert delivery.resume_after_answer(board, task.id, answer="Desculpe a demora, segue o vídeo.", source=source) is False
    (late,) = [row for row in delivery.pending_decisions(board) if row["task_id"] == task.id]
    context = json.loads(late["context"])
    assert context["late_requester_answer"] is True and context["supersedes"] == successor
    assert context["human_reply"]["answer"] == "Desculpe a demora, segue o vídeo." and context["human_reply"]["source"] == source
    assert "respondeu depois do prazo" in late["question"]
    delivery.resume_after_answer(board, task.id, answer="Desculpe a demora, segue o vídeo.", source=source)
    assert len(_events(board, task.id, "nfos_late_requester_answer")) == 1


def test_late_answer_on_a_closed_card_keeps_the_existing_route(board, worker):
    task, decision, successor = _expired(board, worker)
    board.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), task.id))
    board.commit()
    with pytest.raises(delivery.WorkflowError, match="No pending human question"):
        delivery.resume_after_answer(board, task.id, answer="Segue o vídeo.", source=dict(REPLY, message_id="m-5"))
    assert not _events(board, task.id, "nfos_late_requester_answer")
    assert "human_reply" not in _context(board, successor)


def test_read_only_run_reports_what_it_would_adopt_and_writes_nothing(board, worker):
    task, decision = _card(board, worker)
    _ask(board, decision)
    before = (board.execute("SELECT count(*) FROM task_events").fetchone()[0], _context(board, decision))
    (action,) = deadline.sweep(board, now=int(time.time()), dry_run=True)
    assert action["action"] == "adopt" and action["task_id"] == task.id and action["channel"] == "portal"
    assert before == (board.execute("SELECT count(*) FROM task_events").fetchone()[0], _context(board, decision))
    assert _wait(board, decision) is None


def test_the_deadline_comes_from_one_global_key(board, worker, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "requester_answer_hours": 12})
    task, decision, wait = _asked(board, worker)
    asked = wait["created_at"]
    assert wait["deadline_at"] == asked + 12 * HOUR and wait["next_check_at"] == asked + 6 * HOUR


@pytest.mark.parametrize("value", [0, False, True, "0", "48", None, "", -1, 721, [48]])
def test_there_is_no_switch_that_turns_the_deadline_off(board, worker, monkeypatch, value):
    """De 10/10 a 14/10 a chave em zero desligou o prazo de todos os projetos sem um evento sequer. Valor fora de 0 a 720,
    zero inclusive, vale o padrão: a pergunta sai com o prazo e ganha a sua espera."""
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "requester_answer_hours": value})
    assert deadline.hours() == deadline.DEFAULT_HOURS
    task, decision, wait = _asked(board, worker)
    assert "Se não recebermos sua resposta até" in _context(board, decision)["public_message"]["text"]
    assert wait["deadline_at"] == wait["created_at"] + 48 * HOUR


def test_absent_key_means_the_default_deadline(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    assert deadline.hours() == deadline.DEFAULT_HOURS
    monkeypatch.setattr(review, "settings", lambda: {"requester_answer_hours": 36.5})
    assert deadline.hours() == 36.5


def test_the_runtime_tick_adopts_and_expires_the_question(board, worker):
    task, decision = _card(board, worker)
    _ask(board, decision)
    delivery.reconcile_human_answers(board)
    wait = _wait(board, decision)
    assert wait["status"] == "open"
    past = int(time.time()) - HOUR
    asked = past - 2 * DAY
    evidence = dict(wait["evidence"], asked_at=asked, warned_at=past - DAY, due_at=past)
    board.execute("UPDATE nfos_waits SET evidence=?, created_at=?, next_check_at=?, deadline_at=? WHERE id=?",
                  (json.dumps(evidence), asked, past, past, wait["id"]))
    board.commit()
    delivery.reconcile_human_answers(board)
    assert _wait(board, decision)["status"] == "expired"
    assert delivery.get_decision(board, decision)["status"] == "superseded"
    assert kb.get_task(board, task.id).status == "ready"


def test_principal_instructions_state_the_deadline_and_what_follows_it():
    from hermes_cli.nfos_runtime import principal_instructions
    text = principal_instructions()
    assert "kanban.delivery.requester_answer_hours" in text and "partial delivery" in text
