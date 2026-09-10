"""URGENT_20260910: urgent or repeated requests attach to the open card, escalate priority, and get a burst slot."""
import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d

URL = "https://admin.concursaai.com/notice-uploads/a054d76a-11a4-443c-a397-4a6a884c4573"


def _source(mid):
    return {"platform": "telegram", "chat_id": "-100", "thread_id": "4", "message_id": str(mid)}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    project = {"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path)}
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid = d.receive_request(conn, source=_source(1), text="[Japa|1] corrigir o edital " + URL, project=project)
        req = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid())
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL WHERE id=?", (task.id,))
        conn.commit()
    return project, task


def _req(conn, rid):
    return conn.execute("SELECT status, task_id FROM nfos_requests WHERE id=?", (rid,)).fetchone()


def _task(conn, tid):
    return conn.execute("SELECT status, priority FROM tasks WHERE id=?", (tid,)).fetchone()


def test_same_reference_attaches_instead_of_new_card(board):
    project, task = board
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        r = _req(conn, rid)
        assert r["status"] == "attached" and r["task_id"] == task.id
        assert d.reserve_request(conn, capacity=2) is None
        assert _task(conn, task.id)["priority"] == 0
        body = conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0]
        assert body.startswith("[reenvio]")


def test_urgent_reply_escalates_the_existing_card(board):
    project, task = board
    with kb.connect_closing() as conn:
        d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        rid = d.receive_request(conn, source=_source(3), text="[Maikol|9] prioridade máxima nesse", project=project,
                                defer_to_principal=True, reply_to_message_id="2")
        r = _req(conn, rid)
        assert r["status"] == "attached" and r["task_id"] == task.id
        t = _task(conn, task.id)
        assert t["priority"] == 100 and t["status"] == "ready"
        kinds = [x[0] for x in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,))]
        assert "priority_escalated" in kinds and "request_attached" in kinds


def test_urgent_without_reply_uses_previous_message_in_thread(board):
    project, task = board
    with kb.connect_closing() as conn:
        d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        rid = d.receive_request(conn, source=_source(3), text="[Maikol|9] urgente", project=project)
        assert _req(conn, rid)["task_id"] == task.id
        assert _task(conn, task.id)["priority"] == 100


def test_urgent_new_request_creates_card_with_priority_100(board):
    project, task = board
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task.id,))
        conn.commit()
        time.sleep(0)
        rid = d.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "9", "message_id": "7"},
                                text="[Maikol|9] prioridade máxima: subir o plano do tenant Y", project=project)
        assert _req(conn, rid)["status"] == "pending"
        req = d.reserve_request(conn, capacity=2)
        new = d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid())
        assert _task(conn, new.id)["priority"] == 100


def test_burst_slot_and_order(tmp_path):
    conn = kb.connect(tmp_path / "k.db")
    now = int(time.time())
    conn.execute("INSERT INTO tasks (id, title, status, created_at, task_role, priority) VALUES "
                 "('t_old', 'a', 'ready', ?, 'work', 3), ('t_urg', 'b', 'ready', ?, 'work', 100), ('t_int', 'c', 'ready', ?, 'work', 3)",
                 (now - 9000, now - 100, now - 50))
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 1
    with kb.write_txn(conn):
        kb._append_event(conn, "t_int", "interrupted", {})
    rows = conn.execute("SELECT id, assignee, priority FROM tasks WHERE status='ready' ORDER BY priority DESC, created_at ASC").fetchall()
    assert [r["id"] for r in kb._resume_first(conn, rows)] == ["t_urg", "t_int", "t_old"]
    conn.execute("UPDATE tasks SET status='running' WHERE id='t_urg'")
    conn.commit()
    assert kb._urgent_burst_slots(conn) == 0
