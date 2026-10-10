"""SHOW_CALLS_LIMIT_20261010: o show devolve as chamadas nativas que ainda importam; a antiga se lê por id.

Em 10/10/2026, com o limite das decisões já no ar, o show do t_8ed13ed7 (Concursa) ainda tinha 273 KB, e 143 KB eram o
campo native_calls, com 151 chamadas.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_tool as tool


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
        tool.init_schema(conn)
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


def _call(conn, task, order, status="succeeded"):
    call_id = f"call_{order:04d}"
    conn.execute("INSERT INTO nfos_tool_calls(id,task_id,run_id,argv_json,cwd,status,created_at,timeout_seconds,returncode) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (call_id, task.id, task.current_run_id, json.dumps(["concursa-lab", "run", f"passo-{order}"]), "/srv/x", status,
                  1000.0 + order, 600.0, None if status in tool.ACTIVE else 0))
    return call_id


def _calls(conn, task, total=30, running=()):
    ids = [_call(conn, task, order, "running" if order in running else "succeeded") for order in range(1, total + 1)]
    conn.commit()
    return ids


def _show(monkeypatch, capsys, task, *extra):
    monkeypatch.setattr(sys, "argv", ["nfos_delivery", "show", "--task", task.id, *extra])
    delivery.main()
    return json.loads(capsys.readouterr().out)


def test_without_a_limit_every_call_is_listed(board, task):
    ids = _calls(board, task)
    rows, cut = delivery.card_calls(board, task.id)
    assert cut is None and [row["id"] for row in rows] == ids


def test_limit_keeps_the_most_recent_calls_and_the_running_ones(board, task):
    ids = _calls(board, task, running={3})
    rows, cut = delivery.card_calls(board, task.id, 10)
    assert [row["id"] for row in rows] == [ids[2], *ids[-10:]]
    assert cut["total"] == 30 and cut["omitted"] == 19 and "call --call <id>" in cut["hint"]
    assert all(row["argv_json"] and row["cwd"] for row in rows)


@pytest.mark.parametrize("status", tool.ACTIVE)
def test_limit_keeps_every_call_without_an_outcome(board, task, status):
    # Chamada sem desfecho é o efeito incerto que o worker lê antes de repetir: intent e stopping valem como running.
    ids = [_call(board, task, order, status if order == 1 else "succeeded") for order in range(1, 13)]
    board.commit()
    rows, cut = delivery.card_calls(board, task.id, 10)
    assert [row["id"] for row in rows] == [ids[0], *ids[-10:]]
    assert rows[0]["status"] == status and rows[0]["argv_json"] and rows[0]["cwd"]
    assert cut["total"] == 12 and cut["omitted"] == 1


def test_card_with_few_calls_is_not_cut(board, task):
    ids = _calls(board, task, total=7)
    rows, cut = delivery.card_calls(board, task.id, 10)
    assert cut is None and [row["id"] for row in rows] == ids


def test_show_follows_the_configured_limit_and_full_returns_everything(board, task, config, monkeypatch, capsys):
    ids = _calls(board, task)
    plain = _show(monkeypatch, capsys, task)
    assert [call["id"] for call in plain["native_calls"]] == ids and "native_calls_total" not in plain
    config["show_native_calls_limit"] = 10
    shown = _show(monkeypatch, capsys, task)
    assert [call["id"] for call in shown["native_calls"]] == ids[-10:]
    assert shown["native_calls_total"] == 30 and shown["native_calls_omitted"] == 20 and "show --full" in shown["native_calls_hint"]
    full = _show(monkeypatch, capsys, task, "--full")
    assert full["native_calls"] == plain["native_calls"] and "native_calls_total" not in full


def test_the_two_limits_are_independent(board, task, config, monkeypatch, capsys):
    _calls(board, task)
    config["show_decisions_limit"] = 8
    shown = _show(monkeypatch, capsys, task)
    assert len(shown["native_calls"]) == 30 and "native_calls_total" not in shown


@pytest.mark.parametrize("value", ["dez", -3, None, 0])
def test_invalid_limit_means_no_limit(board, task, config, monkeypatch, capsys, value):
    _calls(board, task)
    config["show_native_calls_limit"] = value
    assert len(_show(monkeypatch, capsys, task)["native_calls"]) == 30


def test_call_reads_one_omitted_call_whole_by_id(board, task, monkeypatch, capsys):
    ids = _calls(board, task)
    monkeypatch.setattr(sys, "argv", ["nfos_delivery", "call", "--call", ids[0]])
    delivery.main()
    read = json.loads(capsys.readouterr().out)["call"]
    assert read["id"] == ids[0] and json.loads(read["argv_json"]) == ["concursa-lab", "run", "passo-1"] and read["status"] == "succeeded"


@pytest.mark.parametrize("call_id", ["call_que_nao_existe", "", None])
def test_unknown_call_is_refused(board, call_id):
    with pytest.raises(delivery.WorkflowError, match="Unknown native call"):
        delivery.read_call(board, call_id)
