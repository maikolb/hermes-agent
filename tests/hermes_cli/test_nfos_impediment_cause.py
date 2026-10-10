"""IMPEDIMENT_CAUSE_20261010: o worker declara a causa do impedimento numa lista fechada; nesta fase ela só fica registrada.

Em 09/10/2026, 109 de 358 impedimentos do Concursa eram mecânicos e cada classe só foi achada depois, por regex no texto.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

QUESTION = "O concursa-lab saiu com 75 duas vezes; sigo esperando?"


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


@pytest.fixture
def task(board, worker):
    rid = delivery.receive_request(board, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao enviar o edital.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(board, capacity=2)
    return delivery.bootstrap_card(board, rid, request["claim_token"], pid=worker.pid)


def _requests(conn, task_id):
    return [e for e in kb.list_events(conn, task_id) if e.kind == "nfos_principal_requested"]


def _ask(conn, task, question=QUESTION, **context):
    return delivery.get_decision(conn, delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                                              question=question, context=context))


def test_declared_cause_is_recorded_and_the_question_still_goes_to_the_principal(board, task):
    decision = _ask(board, task, declared_cause="lab_busy")
    assert json.loads(decision["context"])["declared_cause"] == "lab_busy"
    assert decision["status"] == "pending" and decision["author"] is None
    assert [e.payload["decision_id"] for e in _requests(board, task.id)] == [decision["id"]]


def test_impediment_without_a_cause_is_asked_as_before(board, task):
    decision = _ask(board, task)
    assert "declared_cause" not in json.loads(decision["context"])
    assert decision["status"] == "pending" and len(_requests(board, task.id)) == 1


@pytest.mark.parametrize("cause", ["laboratorio", "", ["lab_busy"], 75])
def test_unknown_cause_is_refused_and_nothing_is_recorded(board, task, cause):
    with pytest.raises(delivery.WorkflowError, match="Unknown impediment cause; use one of: lab_transport"):
        _ask(board, task, declared_cause=cause)
    assert not board.execute("SELECT 1 FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()
    assert not _requests(board, task.id)


def test_declared_cause_does_not_widen_the_pending_deduplication(board, task):
    first = _ask(board, task, declared_cause="lab_busy")
    same = _ask(board, task, declared_cause="lab_busy")
    other = _ask(board, task, question="O broker não tem o comando de atualizar o código.", declared_cause="lab_busy")
    assert same["id"] == first["id"] and other["id"] != first["id"]


def _cli_ask(monkeypatch, capsys, tmp_path, task, *extra):
    payload = tmp_path / "question.json"
    payload.write_text(json.dumps({"question": QUESTION}), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(sys, "argv", ["nfos_delivery", "ask", "--kind", "impediment", "--input", str(payload), *extra])
    delivery.main()
    return json.loads(capsys.readouterr().out)


def test_cli_records_the_cause_and_hints_only_when_it_is_missing(board, task, monkeypatch, capsys, tmp_path):
    asked = _cli_ask(monkeypatch, capsys, tmp_path, task, "--cause", "lab_capability")
    assert "cause_hint" not in asked
    assert json.loads(delivery.get_decision(board, asked["decision_id"])["context"])["declared_cause"] == "lab_capability"


def test_cli_without_a_cause_returns_the_list_of_causes(board, task, monkeypatch, capsys, tmp_path):
    asked = _cli_ask(monkeypatch, capsys, tmp_path, task)
    assert "declared_cause" not in json.loads(delivery.get_decision(board, asked["decision_id"])["context"])
    assert all(cause in asked["cause_hint"] for cause in delivery.IMPEDIMENT_CAUSES)


def test_cli_refuses_a_cause_outside_the_list(board, task, monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit):
        _cli_ask(monkeypatch, capsys, tmp_path, task, "--cause", "laboratorio")
    assert not board.execute("SELECT 1 FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()
