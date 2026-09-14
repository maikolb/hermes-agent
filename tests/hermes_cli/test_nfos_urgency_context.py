"""URGENCY_CONTEXT_20260914 (ordem do Maikol): urgência é o julgamento do Principal sobre o contexto da conversa, com motivo.
Palavra-chave sozinha não muda prioridade; o pedido urgente passa na frente e ganha vaga extra; card aberto sobe pelo CLI
urgent, que só o Principal usa."""
import json
import os
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d
from hermes_cli import nfos_runtime as rt

URL = "https://admin.concursaai.com/notice-uploads/a054d76a-11a4-443c-a397-4a6a884c4573"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kb.connect_closing() as conn:
        d.init_schema(conn)
    return {"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path)}


def _source(mid, thread="4"):
    return {"platform": "telegram", "chat_id": "-100", "thread_id": thread, "message_id": str(mid)}


def _payload(conn, rid):
    return json.loads(conn.execute("SELECT payload FROM nfos_requests WHERE id=?", (rid,)).fetchone()[0])


def _status(conn, rid):
    return conn.execute("SELECT status FROM nfos_requests WHERE id=?", (rid,)).fetchone()[0]


def _boot(conn, rid):
    req = d.reserve_request(conn, capacity=4)
    assert req["id"] == rid
    return d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid())


def _escalations(conn, task_id):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='priority_escalated' ORDER BY id", (task_id,))]


def test_keyword_alone_does_not_mark_urgency(board):
    assert not hasattr(d, "_URGENT_RE")
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(1), text="[Maikol|9] prioridade máxima, urgente: corrigir o edital", project=board)
        assert "urgent" not in _payload(conn, rid)
        task = _boot(conn, rid)
        assert kb.get_task(conn, task.id).priority == d.HUMAN_REQUEST_PRIORITY


def test_principal_judgment_on_coordinated_message_makes_the_new_card_urgent(board):
    text = "[Maikol|9] prioridade 100. A solução que foi pra produção não arrumou, parece que piorou"
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(2), text=text, project=board, defer_to_principal=True, reply_to_message_id="4")
        assert _status(conn, rid) == "coordinating" and "urgent" not in _payload(conn, rid)
        same = d.receive_request(conn, source=_source(2), text=text, project=board,
                                 urgency={"reason": "Produção piorou depois da entrega e o Maikol pediu prioridade 100"})
        assert same == rid
        saved = _payload(conn, rid)
        assert saved["urgent"] is True and saved["urgency"]["by"] == "Principal" and "piorou" in saved["urgency"]["reason"]
        assert saved["text"] == text and saved["coordination"]["reply_to_message_id"] == "4"
        assert _status(conn, rid) == "pending"
        task = _boot(conn, rid)
        assert kb.get_task(conn, task.id).priority == d.URGENT_PRIORITY


def test_urgency_needs_a_reason(board):
    with kb.connect_closing() as conn:
        for bad in ({}, {"reason": "  "}, "urgente", {"level": "urgent"}):
            with pytest.raises(d.WorkflowError, match="reason"):
                d.receive_request(conn, source=_source(3), text="[Maikol|9] x", project=board, urgency=bad)
        assert conn.execute("SELECT COUNT(*) FROM nfos_requests").fetchone()[0] == 0


def test_urgent_request_goes_first_and_takes_one_extra_slot(board):
    with kb.connect_closing() as conn:
        normal = d.receive_request(conn, source=_source(10, "7"), text="[Japa|1] ajustar rodapé", project=board)
        urgent = d.receive_request(conn, source=_source(11, "7"), text="[Maikol|9] cliente sem login",
                                   project=board, urgency={"reason": "Cliente parado sem conseguir entrar"})
        other = d.receive_request(conn, source=_source(12, "7"), text="[Maikol|9] outro cliente sem login",
                                  project=board, urgency={"reason": "Segundo cliente parado"})
        for rid, age in ((normal, 30), (urgent, 20), (other, 10)):
            conn.execute("UPDATE nfos_requests SET created_at=created_at-? WHERE id=?", (age, rid))
        conn.commit()
        assert d.reserve_request(conn, capacity=4)["id"] == urgent  # na frente do pedido normal mais antigo
        conn.execute("UPDATE nfos_requests SET status='pending', claim_token=NULL, claimed_at=NULL WHERE id=?", (urgent,))
        conn.execute("INSERT INTO tasks (id, title, status, created_at, task_role, priority) VALUES ('t_busy', 'x', 'running', ?, 'work', 10)",
                     (int(time.time()),))
        conn.commit()
        assert d.reserve_request(conn, capacity=1)["id"] == urgent  # board cheio: vaga extra para o urgente
        assert d.reserve_request(conn, capacity=1) is None  # um urgente já iniciando: sem segunda vaga extra
        assert _status(conn, normal) == "pending" and _status(conn, other) == "pending"


def test_principal_raises_open_card_through_cli_and_a_worker_cannot(board, tmp_path, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(20), text="[Japa|1] corrigir o edital", project=board)
        task = _boot(conn, rid)
        conn.execute("UPDATE tasks SET status='blocked', block_kind='dependency', claim_lock=NULL, worker_pid=NULL WHERE id=?", (task.id,))
        conn.commit()
    reason = tmp_path / "urgency.json"
    reason.write_text(json.dumps({"reason": "O Maikol pediu para passar na frente: produção piorou"}), encoding="utf-8")
    argv = ["nfos_delivery", "urgent", "--db", str(tmp_path / "kanban.db"), "--task", task.id, "--input", str(reason)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    with pytest.raises(d.WorkflowError, match="Principal"):
        d.main()
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    capsys.readouterr()
    d.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["priority"] == d.URGENT_PRIORITY and out["status"] == "ready"
    with kb.connect_closing() as conn:
        ev = _escalations(conn, task.id)
        assert ev and ev[-1]["by"] == "Principal" and "produção piorou" in ev[-1]["reason"] and ev[-1]["previous_status"] == "blocked"
        body = conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0]
        assert body.startswith("[urgente] ")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task.id,))
        conn.commit()
        with pytest.raises(d.WorkflowError, match="closed card"):
            d.escalate_urgent(conn, task.id, reason="tarde demais")


def test_urgency_on_a_message_already_attached_to_an_open_card_escalates_that_card(board):
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(30), text="[Japa|1] corrigir o edital " + URL, project=board)
        task = _boot(conn, rid)
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL WHERE id=?", (task.id,))
        conn.commit()
        text = "[Maikol|9] " + URL + " isso aqui está travando a venda"
        again = d.receive_request(conn, source=_source(31), text=text, project=board, defer_to_principal=True, reply_to_message_id="4")
        assert tuple(conn.execute("SELECT status, task_id FROM nfos_requests WHERE id=?", (again,)).fetchone()) == ("attached", task.id)
        assert kb.get_task(conn, task.id).priority == d.HUMAN_REQUEST_PRIORITY  # sem julgamento do Principal, não sobe
        d.receive_request(conn, source=_source(31), text=text, project=board, urgency={"reason": "Travando a venda do cliente"})
        assert kb.get_task(conn, task.id).priority == d.URGENT_PRIORITY
        ev = _escalations(conn, task.id)
        assert ev[-1]["how"] == "attached_request" and ev[-1]["reason"] == "Travando a venda do cliente"


def test_principal_is_told_to_judge_urgency_by_context():
    ctx = {"db_path": "/srv/x/kanban.db", "request": {"source": {}, "text": "t", "project": {}, "attachments": [], "part": "0"}}
    text = rt.coordinator_intake_instructions(ctx, reply_to="4")
    assert "never a keyword match" in text and "\"urgency\"" in text
    assert " urgent --db " in text and "--task CARD_ID --input urgency.json" in text
    principal = rt.principal_instructions()
    assert "never from keywords" in principal and "urgent --db DB --task CARD --input urgency.json" in principal
