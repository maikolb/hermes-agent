"""CURRENT_DECISIONS_20261010 e SUPERSEDED_REFERENCE_20261010: o worker relançado recebe a decisão que vale.

Em 09/10/2026 o Principal respondeu 52 impedimentos do Concursa como já decididos. Em 24 a pergunta era a primeira de uma
execução nova, que não recebia as respostas anteriores; em 11 a pergunta citava uma decisão já substituída e a resposta só
apontava a sucessora.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')  # NO_OWNER_QUESTIONS_UNIVERSAL_20261010

OWNER_QUESTION = "PERGUNTA para Maikol: Maikol, qual acesso da administração do Concursa-Isolado podemos usar?"
CURRENT_ANSWER = "CONTINUE: retiro a escalada de acesso; use a operação update do broker."


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


def _replaced_question(conn, worker):
    """Pergunta ao dono reconsiderada pelo Principal, com o card de volta a uma execução nova."""
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao enviar o edital.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker.pid)
    old = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                 question="O broker não oferece atualização de código preservando a base.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    delivery.resolve_decision(conn, old, action='human', answer=OWNER_QUESTION, author='Principal')
    worker.kill()
    worker.wait()
    current = delivery.reconsider_decision(conn, old, action='continue', author='Principal',
                                           reason='Capacidade update publicada', answer=CURRENT_ANSWER)
    relaunched = kb.claim_task(conn, task.id)
    assert relaunched and relaunched.current_run_id != task.current_run_id
    return relaunched, old, current


def _principal_requests(conn, task_id):
    return [e for e in kb.list_events(conn, task_id) if e.kind == 'nfos_principal_requested']


def test_question_citing_a_replaced_decision_is_answered_with_the_one_in_force(board, worker):
    task, old, current = _replaced_question(board, worker)
    asked_before = len(_principal_requests(board, task.id))
    decision = delivery.get_decision(board, delivery.ask_principal(
        board, task.id, task.current_run_id, kind="impediment", context={},
        question=f"Decisão {old} retornou action=human no wait; destinatário Maikol. {OWNER_QUESTION}"))
    assert (decision['status'], decision['action'], decision['author']) == ('resolved', 'continue', 'NFOS automation')
    assert current in decision['answer'] and CURRENT_ANSWER in decision['answer']
    assert len(_principal_requests(board, task.id)) == asked_before
    assert decision['id'] not in [d['id'] for d in delivery.pending_decisions(board)]


def test_truncated_reference_is_resolved_inside_the_card(board, worker):
    task, old, current = _replaced_question(board, worker)
    decision = delivery.get_decision(board, delivery.ask_principal(
        board, task.id, task.current_run_id, kind="impediment", context={},
        question=f"Bloqueio determinado pela decisão {old[:12]} (human). Autoriza seguir?"))
    assert decision['author'] == 'NFOS automation' and current in decision['answer']


def test_insisting_in_the_same_execution_reaches_the_principal(board, worker):
    task, old, _ = _replaced_question(board, worker)
    question = f"A decisão {old} pedia acesso; a vigente não cobre o carregamento do candidato."
    delivery.ask_principal(board, task.id, task.current_run_id, kind="impediment", question=question, context={})
    second = delivery.get_decision(board, delivery.ask_principal(
        board, task.id, task.current_run_id, kind="impediment", question=question + " Falta a rota de carga.", context={}))
    assert second['status'] == 'pending'


def test_question_citing_decisions_in_force_goes_to_the_principal(board, worker):
    task, _, current = _replaced_question(board, worker)
    decision = delivery.get_decision(board, delivery.ask_principal(
        board, task.id, task.current_run_id, kind="impediment", context={},
        question=f"Apliquei {current}; o update devolveu erro de migração. Qual o próximo passo?"))
    assert decision['status'] == 'pending'


def test_relaunched_worker_receives_the_answers_in_force(board, worker):
    task, old, current = _replaced_question(board, worker)
    context = delivery.worker_context(board, task.id)
    assert CURRENT_ANSWER in context and current in context
    assert f"{old} -> {current} (resolved/continue)" in context
    assert OWNER_QUESTION not in context
