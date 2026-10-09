"""WAIT_FOLLOWS_SUCCESSOR_20261009: o wait de uma decisão substituída responde pela sucessora vigente.

No t_8ed13ed7 (09/10/2026) o Principal reconsiderou dec_785f17b753874f9699d0 (pergunta ao Maikol) com CONTINUE pela sucessora
nd_1facb9ca63b34325a1ed9978. O worker continuava esperando a decisão antiga; o wait devolvia action human e a pergunta, e o
worker a reabria a cada run (04:23, 05:52 e 05:55).
"""
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


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "projects": {"pilot": {}}})
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


def _owner_question(conn, worker):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao enviar o edital.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker.pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="O broker não oferece atualização de código preservando a base.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    delivery.resolve_decision(conn, decision, action='human', answer=QUESTION, author='Principal')
    worker.kill()
    worker.wait()
    assert kb.get_task(conn, task.id).status == 'blocked'
    return task, decision


def test_wait_on_reconsidered_question_returns_the_principal_continue(board, worker):
    task, decision = _owner_question(board, worker)
    successor = delivery.reconsider_decision(board, decision, action='continue', author='Principal',
                                             reason='Capacidade update publicada e conferida ao vivo',
                                             answer='CONTINUE: retiro a escalada de acesso; use a operação update.')
    assert delivery.get_decision(board, decision)['status'] == 'superseded'
    got = delivery.wait_decision(board, decision, timeout=0)
    assert got['id'] == successor and got['action'] == 'continue' and 'Maikol' not in got['answer']
    assert got['requested_decision'] == decision and got['superseded_chain'] == [decision]


def test_wait_on_refused_owner_question_follows_to_the_pending_successor(board, worker, monkeypatch):
    task, decision = _owner_question(board, worker)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "projects": {"pilot": {"owner_questions": False}}})
    (old, new), = delivery.review_owner_questions(board)
    got = delivery.wait_decision(board, decision, timeout=0)
    assert got['id'] == new and got['status'] == 'pending' and got['superseded_chain'] == [decision]
    assert got.get('action') != 'human'


def test_wait_on_a_current_decision_is_unchanged(board, worker):
    task, decision = _owner_question(board, worker)
    got = delivery.wait_decision(board, decision, timeout=0)
    assert got['id'] == decision and got['status'] == 'human' and 'superseded_chain' not in got
