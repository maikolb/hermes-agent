"""NO_OWNER_QUESTIONS_20261009: projeto com owner_questions: false não pergunta ao dono.

Maikol, 09/10/2026: "Não quero que essas merdas de card fiquem perguntando coisas pra mim ou travando". Nessa noite, cinco
cards do Concursa ficaram horas em decisão humana perguntando ao Maikol por um "canal administrativo". O único humano que o
card pode consultar é o solicitante, pelo Balcão.
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

QUESTION = ("PERGUNTA para Maikol: Maikol, qual acesso ou canal autorizado da administração do Concursa-Isolado podemos usar "
            "para carregar o candidato na cópia retida?")


def _settings(owner_questions):
    projects = {'pilot': {} if owner_questions is None else {'owner_questions': owner_questions}}
    return lambda: {'principal_validation': False, 'projects': projects}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", _settings(False))
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


def _ready_card_with_open_question(conn, pid):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao enviar o edital.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="O broker não oferece atualização de código preservando a base.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id), decision


def test_principal_cannot_ask_the_owner(board, worker):
    task, decision = _ready_card_with_open_question(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match='não há pergunta ao Maikol'):
        delivery.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    with pytest.raises(delivery.WorkflowError, match='não há pergunta ao Maikol'):
        delivery.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal',
                                  public_message={'kind': 'question', 'text': 'Qual canal?', 'to': 'Maikol'})
    assert delivery.get_decision(board, decision)['status'] == 'pending'


def test_principal_can_still_ask_the_requester(board, worker):
    task, decision = _ready_card_with_open_question(board, worker.pid)
    delivery.resolve_decision(board, decision, action='human', author='Principal',
                              answer='Pergunta ao solicitante: pode enviar um vídeo da tela?',
                              public_message={'kind': 'question', 'text': 'Pode enviar um vídeo curto da tela com o erro?'})
    assert delivery.get_decision(board, decision)['status'] == 'human'


def test_project_without_the_switch_refuses_the_owner_question_too(board, worker, monkeypatch):
    # NO_OWNER_QUESTIONS_UNIVERSAL_20261010: até 10/10 este projeto, sem a chave, ainda perguntava ao dono.
    monkeypatch.setattr(review, "settings", _settings(None))
    task, decision = _ready_card_with_open_question(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match='não há pergunta ao Maikol'):
        delivery.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    assert delivery.get_decision(board, decision)['status'] == 'pending'
    assert delivery.review_owner_questions(board) == []


def test_open_owner_question_returns_to_the_principal(board, worker, monkeypatch):
    monkeypatch.setattr(review, "settings", _settings(True))  # o estado de antes: o projeto ainda perguntava
    task, decision = _ready_card_with_open_question(board, worker.pid)
    delivery.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    assert kb.get_task(board, task.id).status == 'blocked', 'reprodução: a pergunta ao dono prende o card'
    worker.kill()
    worker.wait()  # o worker do run já saiu, como no caso real
    monkeypatch.setattr(review, "settings", _settings(False))
    moved = delivery.review_owner_questions(board)
    assert len(moved) == 1 and moved[0][0] == decision
    old, new = delivery.get_decision(board, decision), delivery.get_decision(board, moved[0][1])
    assert old['status'] == 'superseded' and json.loads(old['context'])['superseded_reason'] == 'no_owner_questions'
    assert new['status'] == 'pending' and 'não há pergunta ao Maikol' in new['question'] and 'canal autorizado' in new['question']
    assert kb.get_task(board, task.id).status == 'ready'
    assert delivery.review_owner_questions(board) == [], 'a varredura não repete'


def test_requester_question_stays_open(board, worker):
    task, decision = _ready_card_with_open_question(board, worker.pid)
    delivery.resolve_decision(board, decision, action='human', author='Principal',
                              answer='Pergunta ao solicitante: pode enviar um vídeo da tela?',
                              public_message={'kind': 'question', 'text': 'Pode enviar um vídeo curto da tela com o erro?'})
    assert delivery.review_owner_questions(board) == []
    assert delivery.get_decision(board, decision)['status'] == 'human'
