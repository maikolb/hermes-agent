"""HUMAN_PRIORITY_20260910: a card born from a human message outranks promoted backlog; urgent stays 100."""
import os

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d


def _project(tmp_path):
    return {"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path)}


def _boot(conn, rid):
    req = d.reserve_request(conn, capacity=4)
    return d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid())


def _priority(conn, tid):
    return conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_human_request_gets_priority_10_and_urgent_100(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid = d.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "4", "message_id": "1"},
                                text="[Japa|1] corrigir o edital X", project=_project(tmp_path))
        t1 = _boot(conn, rid)
        assert _priority(conn, t1.id) == 10
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (t1.id,))
        conn.commit()
        rid2 = d.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "4", "message_id": "2"},
                                 text="[Maikol|9] prioridade máxima: corrigir o edital Y", project=_project(tmp_path))
        t2 = _boot(conn, rid2)
        assert _priority(conn, t2.id) == 100


def test_intake_priority_rules():
    assert d._intake_priority({"urgent": True, "source": {"platform": "telegram"}}) == 100
    assert d._intake_priority({"source": {"platform": "telegram"}}) == 10
    assert d._intake_priority({"source": {"platform": "telegram", "message_identity_kind": "retained-card"}}) == 0
    assert d._intake_priority({"source": {"platform": "api_server"}}) == 0
    assert d._intake_priority({}) == 0
