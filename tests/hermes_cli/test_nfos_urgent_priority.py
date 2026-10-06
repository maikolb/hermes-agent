"""URGENT_20260910 e URGENCY_CONTEXT_20260914: repetição ou resposta anexa ao card aberto; a urgência é o julgamento do
Principal sobre o contexto (nunca palavra-chave), sobe a prioridade e ganha vaga extra."""
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


def _portal_card(conn, rid):
    # Portal intake creates its card before approval, as Suporte._submit_request
    # does. It must never bootstrap an unapproved worker to test card identity.
    with kb.write_txn(conn):
        tid = kb.create_task(conn, title=rid, assignee='default', requires_repo=False)
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        conn.execute('INSERT INTO nfos_workflows(task_id,request_id,updated_at) VALUES(?,?,?)',
                     (tid, rid, int(time.time())))
        conn.execute("UPDATE nfos_requests SET status='attached',task_id=? WHERE id=?", (tid, rid))
    return kb.get_task(conn, tid)


def test_same_reference_attaches_instead_of_new_card(board):
    project, task = board
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        r = _req(conn, rid)
        assert r["status"] == "attached" and r["task_id"] == task.id
        assert d.reserve_request(conn, capacity=2) is None
        assert _task(conn, task.id)["priority"] == 10  # HUMAN_PRIORITY_20260910: nasce com 10, reenvio não escala
        body = conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0]
        assert body.startswith("[reenvio]")


def test_bare_host_is_not_a_reference_but_a_path_still_is(board):
    # INTAKE_REFERENCE_20261006: on 05/10 the Sineta reinstall cited https://hml.dovcrm.com.br first and was attached
    # to the newest open card citing the same host (DV-0016), not to the work it repeated.
    project, task = board
    host = "https://hml.example.com"
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            newer = kb.create_task(conn, title="outro pedido", body="Validar login em " + host + "/", assignee="default")
        for number, bare in enumerate((host, host + "/"), start=2):
            lone = d.receive_request(conn, source=_source(number), text="[Maikol|9] conferir " + bare, project=project)
            assert _req(conn, lone)["status"] == "pending" and _req(conn, lone)["task_id"] is None
        both = d.receive_request(conn, source=_source(4), text=f"[Maikol|9] {host} de novo: {URL}", project=project)
        assert _req(conn, both)["task_id"] == task.id != newer


@pytest.mark.parametrize("reference", [URL, URL.rsplit("/", 1)[1]])
def test_reference_with_path_or_uuid_still_attaches(board, reference):
    project, task = board
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source=_source(2), text="[Maikol|9] de novo " + reference, project=project)
        assert _req(conn, rid)["status"] == "attached" and _req(conn, rid)["task_id"] == task.id


def test_portal_tickets_sharing_url_keep_independent_identity(board):
    project, existing = board
    with kb.connect_closing() as conn:
        ids = []
        for chat, number in [('concursa', 'CS-0001'), ('concursa', 'CS-0002'), ('other', 'CS-0001')]:
            source = dict(_source(number), platform='portal', chat_id=chat, thread_id='suporte')
            origin = {'portal': {'chamado': number, 'projeto': chat}}
            rid = d.receive_request(conn, source=source, text=URL, project=project, origin=origin)
            assert _req(conn, rid)['task_id'] is None
            assert d.reserve_request(conn, capacity=4) is None
            task = _portal_card(conn, rid)
            assert d.receive_request(conn, source=source, text=URL, project=project, origin=origin) == rid
            assert _req(conn, rid)['task_id'] == task.id
            ids.append(task.id)
        assert len(set(ids)) == 3 and existing.id not in ids


@pytest.mark.parametrize('origin', [None, {}, {'portal': {}}, {'portal': {'chamado': ''}}])
def test_portal_without_ticket_keeps_reference_attachment(board, origin):
    project, task = board
    with kb.connect_closing() as conn:
        source = dict(_source(2), platform='portal')
        rid = d.receive_request(conn, source=source, text=URL, project=project, origin=origin)
        assert _req(conn, rid)['task_id'] == task.id


def test_portal_explicit_reply_still_attaches(board):
    project, task = board
    with kb.connect_closing() as conn:
        source = dict(_source('CS-0001'), platform='portal', thread_id='suporte')
        rid = d.receive_request(conn, source=source, text=URL, project=project,
                                origin={'portal': {'chamado': 'CS-0001'}})
        assert d.reserve_request(conn, capacity=4) is None
        original = _portal_card(conn, rid)
        reply = dict(source, message_id='reply-1')
        reply_id = d.receive_request(conn, source=reply, text='Mais contexto', project=project,
                                    origin={'portal': {'chamado': 'CS-0001'}}, reply_to_message_id='CS-0001')
        assert _req(conn, reply_id)['task_id'] == original.id != task.id


def test_keyword_reply_attaches_but_only_the_principal_escalates(board):
    project, task = board
    with kb.connect_closing() as conn:
        d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        rid = d.receive_request(conn, source=_source(3), text="[Maikol|9] prioridade máxima nesse", project=project,
                                defer_to_principal=True, reply_to_message_id="2")
        r = _req(conn, rid)
        assert r["status"] == "attached" and r["task_id"] == task.id
        assert _task(conn, task.id)["priority"] == 10  # URGENCY_CONTEXT_20260914: a palavra não sobe; o Principal julga
        d.escalate_urgent(conn, task.id, reason="Maikol pediu prioridade máxima neste card")
        t = _task(conn, task.id)
        assert t["priority"] == 100 and t["status"] == "ready"
        kinds = [x[0] for x in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,))]
        assert "priority_escalated" in kinds and "request_attached" in kinds


def test_bare_urgent_word_waits_for_the_principal(board):
    project, task = board
    with kb.connect_closing() as conn:
        d.receive_request(conn, source=_source(2), text="[Maikol|9] " + URL, project=project)
        rid = d.receive_request(conn, source=_source(3), text="[Maikol|9] urgente", project=project,
                                defer_to_principal=True, reply_to_message_id="4")
        assert _req(conn, rid)["status"] == "coordinating"  # URGENCY_CONTEXT_20260914: o Principal decide o que "urgente" quer dizer
        assert _task(conn, task.id)["priority"] == 10


def test_urgent_new_request_creates_card_with_priority_100(board):
    project, task = board
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task.id,))
        conn.commit()
        rid = d.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "9", "message_id": "7"},
                                text="[Maikol|9] subir o plano do tenant Y, o cliente vai cancelar amanhã", project=project,
                                urgency={"reason": "Cliente vai cancelar amanhã"})
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
