"""PUBLIC_MESSAGES_20261009: o Balcão recebe só o que o operador precisa.

Maikol, 09/10/2026: "tá dando feedback demais ao operador. Isso está errado e gastando cota". O Principal publicava retorno
de andamento em quase toda decisão (até 6 por chamado no Concursa); a reconsideração copiava a pergunta da decisão
substituída para a sucessora, e cada resolução regravava a hora da mensagem, então a mesma pergunta aparecia de novo,
como nova. Agora: pergunta ao solicitante e entrega são publicadas; retorno de andamento escrito pelo Principal não é
(fica num evento interno); a resposta do solicitante recebe só a confirmação curta automática.
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

QUESTION = {"kind": "question", "text": "Pode enviar um vídeo curto da tela com o erro?", "to": "CS Concursa (solicitante)"}
ACK = "Sua resposta foi analisada pela equipe. O atendimento foi encaminhado para continuação."


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "projects": {"pilot": {"owner_questions": False}}})
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


def _card(conn, pid, message_id="11"):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": message_id},
                                   text="Simulado com disciplina errada.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="Falta o print da tela para localizar a disciplina.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id), decision


def _context(conn, decision):
    return json.loads(delivery.get_decision(conn, decision)["context"])


def _events(conn, task_id, kind):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, kind))]


def _asked_and_blocked(conn, worker):
    task, decision = _card(conn, worker.pid)
    delivery.resolve_decision(conn, decision, action="human", author="Principal",
                              answer="Pergunta ao solicitante: pode enviar um vídeo da tela?", public_message=QUESTION)
    worker.kill()
    worker.wait()
    delivery.reconcile_human_answers(conn)
    assert kb.get_task(conn, task.id).status == "blocked"
    return task, decision


def test_principal_progress_update_is_kept_internal(board, worker):
    task, decision = _card(board, worker.pid)
    delivery.resolve_decision(board, decision, action="continue", author="Principal", answer="CONTINUE: seguir com a conferência.",
                              public_message={"kind": "update", "text": "Conferimos o edital e seguimos com a análise."})
    assert "public_message" not in _context(board, decision)
    (event,) = _events(board, task.id, "nfos_public_update_suppressed")
    assert event["decision_id"] == decision and "Conferimos o edital" in event["text"]


def test_question_to_the_requester_and_delivery_are_published(board, worker):
    task, decision = _card(board, worker.pid)
    delivery.resolve_decision(board, decision, action="human", author="Principal",
                              answer="Pergunta ao solicitante: pode enviar um vídeo da tela?", public_message=QUESTION)
    assert _context(board, decision)["public_message"]["text"] == QUESTION["text"]
    task2, other = _card(board, worker.pid, message_id="12")
    delivery.resolve_decision(board, other, action="continue", author="Principal", answer="CONTINUE: dado entregue.",
                              public_message={"kind": "delivery", "text": "Corrigimos o conteúdo do seu plano."})
    assert _context(board, other)["public_message"]["kind"] == "delivery"
    assert not _events(board, task.id, "nfos_public_update_suppressed") and not _events(board, task2.id, "nfos_public_update_suppressed")


def test_answer_from_the_requester_gets_only_the_short_acknowledgment(board, worker):
    task, decision = _asked_and_blocked(board, worker)
    delivery.resume_after_answer(board, task.id, answer="Segue o vídeo da tela.",
                                 source={"platform": "portal", "message_id": "m-1", "actor": "CS Concursa"})
    assert delivery.get_decision(board, decision)["status"] == "pending"
    delivery.resolve_decision(board, decision, action="continue", author="Principal", answer="CONTINUE: vídeo recebido.",
                              public_message={"kind": "update", "text": "Recebemos o vídeo e vamos localizar a disciplina errada agora."})
    ctx = _context(board, decision)
    assert ctx["public_message"]["text"] == ACK
    assert [m["text"] for m in ctx["public_message_history"]] == [QUESTION["text"]]
    (event,) = _events(board, task.id, "nfos_public_update_suppressed")
    assert "Recebemos o vídeo" in event["text"]


def test_reconsidered_decision_does_not_repeat_the_question(board, worker):
    task, decision = _asked_and_blocked(board, worker)
    successor = delivery.reconsider_decision(board, decision, action="continue", author="Principal",
                                             reason="O print já está no relato original da Sineta",
                                             answer="CONTINUE: usar o print do relato original.")
    assert _context(board, decision)["public_message"]["text"] == QUESTION["text"]
    succ = _context(board, successor)
    assert "public_message" not in succ and "public_message_history" not in succ


def test_resolution_without_a_new_message_keeps_the_original_time(board, worker):
    task, decision = _card(board, worker.pid)
    delivery.resolve_decision(board, decision, action="human", author="Principal",
                              answer="Pergunta ao solicitante: pode enviar um vídeo da tela?", public_message=QUESTION)
    ctx = _context(board, decision)
    ctx["public_message"]["created_at"] = 1000
    board.execute("UPDATE nfos_decisions SET status='pending', action=NULL, answer=NULL, author=NULL, resolved_at=NULL, context=? "
                  "WHERE id=?", (json.dumps(ctx), decision))
    board.commit()
    delivery.resolve_decision(board, decision, action="continue", author="Principal", answer="CONTINUE: seguir sem o vídeo.")
    assert _context(board, decision)["public_message"]["created_at"] == 1000


def test_principal_instructions_no_longer_ask_for_public_updates():
    from hermes_cli.nfos_runtime import principal_instructions
    text = principal_instructions()
    assert 'Provide public_message={kind:"update"' not in text
    assert "Do not write public updates" in text and 'kind:"delivery"' in text
