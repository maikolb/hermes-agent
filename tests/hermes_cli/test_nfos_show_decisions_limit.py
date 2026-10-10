"""SHOW_DECISIONS_LIMIT_20261010: o show devolve as decisões que valem agora; a antiga se lê por id.

Em 10/10/2026 o show do t_8ed13ed7 (Concursa) devolvia 437 KB só em decisions, com 147 decisões, a cada execução de worker.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

LONG_ANSWER = "CONTINUE: " + "use a operação update do broker. " * 120
LONG_QUESTION = "O broker não oferece atualização de código preservando a base. " * 20


@pytest.fixture
def config():
    return {"principal_validation": False, "projects": {"pilot": {}}}


@pytest.fixture
def board(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: config)
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


def _card(conn, worker):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
                                   text="Erro ao enviar o edital.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker.pid)


def _decision(conn, task, order, *, kind="impediment", status="resolved", answer="CONTINUE: siga.", question="Posso seguir?",
              context=None):
    decision_id = f"dec_{order:020d}"
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,"
                 "author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (decision_id, task.id, task.current_run_id, kind, question, json.dumps(context or {"evidence": "x" * 300}), 1,
                  1000 + order, status, None if status == "pending" else "continue",
                  None if status == "pending" else answer, None if status == "pending" else "Principal",
                  None if status == "pending" else 1000 + order))
    return decision_id


def _long_card(conn, worker):
    """Card com 33 decisões: duas revisões antigas, 30 impedimentos resolvidos e um pendente."""
    task = _card(conn, worker)
    spec = _decision(conn, task, 1, kind="spec_review", answer=LONG_ANSWER)
    final = _decision(conn, task, 2, kind="final_review", answer=LONG_ANSWER)
    impediments = [_decision(conn, task, order, answer=LONG_ANSWER, question=LONG_QUESTION) for order in range(3, 33)]
    pending = _decision(conn, task, 33, status="pending")
    conn.commit()
    return task, spec, final, impediments, pending


def _show(monkeypatch, capsys, task, *extra):
    monkeypatch.setattr(sys, "argv", ["nfos_delivery", "show", "--task", task.id, *extra])
    delivery.main()
    return json.loads(capsys.readouterr().out)


def test_without_a_limit_every_decision_comes_whole(board, worker):
    task, *_ = _long_card(board, worker)
    rows, cut = delivery.card_decisions(board, task.id)
    assert cut is None
    assert rows == [dict(r) for r in board.execute("SELECT * FROM nfos_decisions WHERE task_id=? ORDER BY created_at", (task.id,))]


def test_limit_keeps_what_is_in_force_whole_and_cuts_the_recent_history(board, worker):
    task, spec, final, impediments, pending = _long_card(board, worker)
    rows, cut = delivery.card_decisions(board, task.id, 8)
    by_id = {row["id"]: row for row in rows}
    assert [row["id"] for row in rows] == [spec, final, *impediments[-9:], pending]
    assert cut["total"] == 33 and cut["omitted"] == 21 and "decision --decision <id>" in cut["hint"]
    for whole in (spec, final, impediments[-1], pending):
        assert "excerpt" not in by_id[whole] and json.loads(by_id[whole]["context"])
    assert by_id[spec]["answer"] == LONG_ANSWER and by_id[impediments[-1]]["answer"] == LONG_ANSWER
    for cut_row in impediments[-9:-1]:
        row = by_id[cut_row]
        assert row["excerpt"] is True and row["context"] is None
        assert row["answer"] == LONG_ANSWER[:delivery.SHOW_DECISION_ANSWER_CHARS] + " [...]"
        assert row["question"] == LONG_QUESTION[:delivery.SHOW_DECISION_QUESTION_CHARS] + " [...]"


@pytest.mark.parametrize("status", ["human", "pending", "esperando_algo_novo"])
@pytest.mark.parametrize("limit", [1, 8])
def test_open_decision_stays_whole_however_old(board, worker, status, limit):
    # SHOW_DECISIONS_OPEN_20261010: decisão sem desfecho é a obrigação vigente do card. Em 10/10/2026, com o limite em 8,
    # as 10 perguntas em 'human' dos cards abertos voltavam com a pergunta cortada e sem contexto; com mais de 8 decisões
    # depois dela, a pergunta sumia do show. Só histórico encerrado (resolved, superseded) é reduzido.
    task = _card(board, worker)
    old = _decision(board, task, 1, status=status, question=LONG_QUESTION, answer=None, context={"evidence": "y" * 900})
    later = [_decision(board, task, order, answer=LONG_ANSWER, question=LONG_QUESTION) for order in range(2, 14)]
    board.commit()
    rows, cut = delivery.card_decisions(board, task.id, limit)
    by_id = {row["id"]: row for row in rows}
    assert old in by_id, "a decisão aberta sumiu do show"
    assert not by_id[old].get("excerpt") and by_id[old]["question"] == LONG_QUESTION
    assert json.loads(by_id[old]["context"]) == {"evidence": "y" * 900}
    # O histórico encerrado continua reduzido: a última resolvida do tipo inteira, as ``limit`` anteriores cortadas.
    assert not by_id[later[-1]].get("excerpt")
    assert sum(1 for row in rows if row.get("excerpt")) == limit
    assert cut["total"] == 13 and cut["omitted"] == 13 - len(rows)


def test_latest_resolved_of_a_kind_is_the_latest_answer_not_the_latest_question(board, worker):
    # A pergunta 2 foi criada antes da 3 e respondida depois: a resposta que vale para o tipo é a dela.
    task = _card(board, worker)
    first = _decision(board, task, 1, answer=LONG_ANSWER)
    answered_last = _decision(board, task, 2, answer=LONG_ANSWER)
    created_last = _decision(board, task, 3, answer=LONG_ANSWER)
    board.execute("UPDATE nfos_decisions SET resolved_at=? WHERE id=?", (9000, answered_last))
    board.commit()
    rows, _ = delivery.card_decisions(board, task.id, 1)
    by_id = {row["id"]: row for row in rows}
    assert not by_id[answered_last].get("excerpt") and by_id[answered_last]["answer"] == LONG_ANSWER
    assert by_id[created_last].get("excerpt") and first not in by_id


def test_short_card_is_not_cut_by_the_limit(board, worker):
    task = _card(board, worker)
    _decision(board, task, 1, kind="spec_review")
    _decision(board, task, 2)
    board.commit()
    rows, cut = delivery.card_decisions(board, task.id, 8)
    assert cut is None and len(rows) == 2 and all("excerpt" not in row for row in rows)


def test_show_follows_the_configured_limit_and_full_returns_everything(board, worker, config, monkeypatch, capsys):
    task, *_ = _long_card(board, worker)
    plain = _show(monkeypatch, capsys, task)
    assert len(plain["decisions"]) == 33 and "decisions_total" not in plain
    config["show_decisions_limit"] = 8
    shown = _show(monkeypatch, capsys, task)
    assert len(shown["decisions"]) == 12 and shown["decisions_total"] == 33 and shown["decisions_omitted"] == 21
    assert "show --full" in shown["decisions_hint"]
    assert len(json.dumps(shown["decisions"])) < len(json.dumps(plain["decisions"])) / 2
    full = _show(monkeypatch, capsys, task, "--full")
    assert full["decisions"] == plain["decisions"] and "decisions_total" not in full


@pytest.mark.parametrize("value", ["oito", -3, None, 0])
def test_invalid_limit_means_no_limit(board, worker, config, monkeypatch, capsys, value):
    task, *_ = _long_card(board, worker)
    config["show_decisions_limit"] = value
    assert len(_show(monkeypatch, capsys, task)["decisions"]) == 33


def test_decision_reads_one_whole_decision_by_id(board, worker, monkeypatch, capsys):
    task, _spec, _final, impediments, _pending = _long_card(board, worker)
    monkeypatch.setattr(sys, "argv", ["nfos_delivery", "decision", "--decision", impediments[0]])
    delivery.main()
    read = json.loads(capsys.readouterr().out)
    assert read["decision"]["answer"] == LONG_ANSWER and read["decision"]["question"] == LONG_QUESTION
    assert json.loads(read["decision"]["context"]) == {"evidence": "x" * 300} and "in_force" not in read


def test_replaced_decision_comes_with_the_one_in_force(board, worker):
    task = _card(board, worker)
    current = _decision(board, task, 2, answer="CONTINUE: use a operação update do broker.")
    old = _decision(board, task, 1, status="superseded", answer="PERGUNTA para Maikol: qual acesso?",
                    context={"superseded_by": current})
    board.commit()
    read = delivery.read_decision(board, old)
    assert read["decision"]["id"] == old
    assert read["in_force"]["id"] == current and read["in_force"]["answer"] == "CONTINUE: use a operação update do broker."


@pytest.mark.parametrize("decision_id", ["dec_que_nao_existe", "", None])
def test_unknown_decision_is_refused(board, decision_id):
    with pytest.raises(delivery.WorkflowError, match="Unknown decision"):
        delivery.read_decision(board, decision_id)
