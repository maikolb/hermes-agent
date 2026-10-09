"""TRANSIENT_CONTINUE_20261009: retenção transient liberada pelo Principal volta à fila sozinha.

t_d8699d5f (09/10/2026): o card esperava a decisão do Principal, foi retido em blocked/transient às 00:49 e o Principal
respondeu CONTINUE às 00:57. Nada devolvia o card: resolve_decision só desbloqueia a adoção legada e a varredura só olhava
awaiting_principal. Ficou parado até a intervenção manual das 04:14.
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

RETENTION = ('Retenção pedida pelo executor efetivada: run encerrado aguardando decisão. Principal deve obter readback '
             'dos caminhos já registrados quando o canal liberar.')


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
def worker_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _ready_card_with_open_question(conn, pid):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao extrair o cargo do usuário.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="Reconciliar a execução remota incerta antes de retomar?", context={})
    # O worker saiu com a pergunta aberta: run encerrado e card de volta a ready, como no t_d8699d5f.
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id), decision


def _age_block(conn, task_id, seconds):
    conn.execute("UPDATE task_events SET created_at=created_at-? WHERE task_id=? AND kind='blocked'", (seconds, task_id))
    conn.commit()


def test_continue_after_the_retention_returns_the_card_to_the_queue(board, worker_pid):
    task, decision = _ready_card_with_open_question(board, worker_pid)
    assert kb.block_task(board, task.id, reason=RETENTION, kind='transient')
    _age_block(board, task.id, 480)
    delivery.resolve_decision(board, decision, action='continue', author='Principal',
                              answer='CONTINUE: reconciliação concluída; a retenção já foi efetivada e a precondição foi removida.')
    assert kb.get_task(board, task.id).status == 'blocked', 'reprodução: a resposta sozinha não devolve o card'
    assert delivery.sweep_awaiting_principal(board) == [task.id]
    assert kb.get_task(board, task.id).status == 'ready'
    assert delivery.sweep_awaiting_principal(board) == [], 'a varredura não repete o desbloqueio'


def test_retention_after_the_answer_stays_blocked(board, worker_pid):
    task, decision = _ready_card_with_open_question(board, worker_pid)
    delivery.resolve_decision(board, decision, action='continue', author='Principal', answer='CONTINUE: siga com o readback.')
    conn = board
    conn.execute("UPDATE nfos_decisions SET resolved_at=resolved_at-600 WHERE id=?", (decision,))
    conn.commit()
    assert kb.block_task(board, task.id, reason=RETENTION, kind='transient')
    assert delivery.sweep_awaiting_principal(board) == []
    assert kb.get_task(board, task.id).status == 'blocked', 'bloqueio novo, posterior à resposta, não é liberado por ela'


def test_open_decision_keeps_the_retention(board, worker_pid):
    task, decision = _ready_card_with_open_question(board, worker_pid)
    assert kb.block_task(board, task.id, reason=RETENTION, kind='transient')
    _age_block(board, task.id, 480)
    assert delivery.sweep_awaiting_principal(board) == []
    delivery.resolve_decision(board, decision, action='continue', author='Principal', answer='CONTINUE: liberado.')
    other = delivery.receive_owner_guidance(board, task.id, text='Confira o readback antes de retomar.',
                                            source={'platform': 'fixture', 'actor': 'Operador', 'message_id': 'guidance-1'})
    assert other['decision_id'] and delivery.sweep_awaiting_principal(board) == []
    assert kb.get_task(board, task.id).status == 'blocked'


def test_destination_wait_keeps_its_own_retention(board, worker_pid):
    task, decision = _ready_card_with_open_question(board, worker_pid)
    assert kb.block_task(board, task.id, reason=RETENTION, kind='transient')
    _age_block(board, task.id, 480)
    state = json.loads(delivery.get_workflow(board, task.id)['state_json'] or '{}') or {}
    state['destination_wait'] = {'target': 'https://destino.example', 'since': int(time.time()) - 600}
    board.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (json.dumps(state), task.id))
    board.commit()
    delivery.resolve_decision(board, decision, action='continue', author='Principal', answer='CONTINUE: mantém a espera do destino.')
    assert delivery.sweep_awaiting_principal(board) == []
    assert kb.get_task(board, task.id).status == 'blocked', 'na espera do destino, continue mantém a espera por desenho'
