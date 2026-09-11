"""RECORD_CONTINUATION_20260911: continuação explícita sem dependência circular, herança de prioridade, precondição herdada,
reclassificação com histórico, fechamento parcial só com continuação válida, consumo da cadeia."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime


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
    return tmp_path


def _card(conn, tmp_path, n=1):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(40 + n)},
        text=f"Corrigir upload {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "prod", "method": "ui", "result": "bug"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, {
        "goal": "Upload correto", "criteria": [{"id": "C1", "text": "cargos corretos"}, {"id": "C2", "text": "deploy"}],
        "steps": ["Reparar"], "delivery_type": "report", "size": "P"}, author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _artifact(tmp_path, name):
    p = tmp_path / name
    p.write_text('{"ok": true}', encoding="utf-8")
    return {"id": name, "path": str(p)}


def _report(tmp_path, c1, c2="PASS", **extra):
    a = _artifact(tmp_path, "proof.json")
    rep = {"summary": "parcial", "artifacts": [a],
           "criteria": [{"id": "C1", "status": c1, "evidence": [a["id"]]}, {"id": "C2", "status": c2, "evidence": [a["id"]]}]}
    rep.update(extra)
    return rep


def test_continuation_is_executable_urgent_and_idempotent(board):
    with kb.connect_closing() as conn:
        parent = _card(conn, board)
        conn.execute("UPDATE tasks SET priority=100 WHERE id=?", (parent.id,)); conn.commit()
        delivery.save_report(conn, parent.id, parent.current_run_id, _report(board, "FAIL"))
        res = delivery.create_continuation(conn, parent.id, title="Reparar o dado", requester=parent.id)
        child = kb.get_task(conn, res["task_id"])
        assert child.status == "ready" and child.priority == 100 and res["existing"] is False
        assert conn.execute("SELECT count(*) FROM task_links WHERE child_id=?", (child.id,)).fetchone()[0] == 0
        assert "C1: FAIL" in (child.body or "")
        again = delivery.create_continuation(conn, parent.id, title="Reparar o dado", requester=parent.id)
        assert again["task_id"] == child.id and again["existing"] is True
        assert delivery.continuation_links(conn, parent.id)["children"] == [child.id]
        assert delivery.continuation_links(conn, child.id)["of"] == parent.id


def test_blocked_parent_passes_its_question_to_the_child(board):
    with kb.connect_closing() as conn:
        parent = _card(conn, board, 2)
        conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), parent.current_run_id))
        conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (parent.id,)); conn.commit()
        assert kb.block_task(conn, parent.id, reason="PERGUNTA para Maikol: qual credencial usar no TEST?", kind="needs_input")
        res = delivery.create_continuation(conn, parent.id, requester="principal")
        child = kb.get_task(conn, res["task_id"])
        assert child.status == "blocked" and child.block_kind == "needs_input"


def test_fail_cannot_be_rewritten_without_reclassification(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board, 3)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "FAIL"))
        with pytest.raises(delivery.WorkflowError, match="reclassified"):
            delivery.save_report(conn, task.id, task.current_run_id, _report(board, "NOT_RUN"))
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS", reclassified=[
            {"id": "C1", "previous": "FAIL", "reason": "leitura anterior no ambiente errado", "evidence": ["proof.json"]}]))
        assert delivery._artifact(conn, task.id, "report")["revision"] == 2


def test_partial_completion_needs_a_valid_continuation(board):
    with kb.connect_closing() as conn:
        task = _card(conn, board, 4)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "FAIL", partial_delivery=True))
        assert delivery.completion_evidence_check(conn, task.id) is None
        res = delivery.create_continuation(conn, task.id, requester=task.id)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "FAIL", partial_delivery=True, continuation=res["task_id"]))
        assert delivery.completion_evidence_check(conn, task.id) is not None
        cons = delivery.chain_consumption(conn, res["task_id"])
        assert cons["chain"] == [task.id, res["task_id"]] and cons["runs"] >= 1


def test_worker_protocol_mentions_continuation(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert "continuation_of" in text and "never rewrite FAIL as NOT_RUN" in text


def test_tool_creates_continuation_without_assignee(board, monkeypatch):
    # RECORD_CONTINUATION_FIX_20260911: continuation_of e o primeiro caminho do kanban_create, antes de exigir assignee.
    with kb.connect_closing() as conn:
        parent = _card(conn, board, 5)
        conn.execute("UPDATE tasks SET priority=100 WHERE id=?", (parent.id,)); conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent.id)
    from tools import kanban_tools as kt
    out = kt._handle_create({"continuation_of": parent.id, "title": "Reparar o dado"})
    assert "assignee is required" not in out and '"ok": true' in out
    res = json.loads(out)
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, res["task_id"])
        assert child.priority == 100 and child.assignee == parent.assignee
    schema = json.dumps(kt.TOOL_SCHEMAS if hasattr(kt, "TOOL_SCHEMAS") else "")
    assert "continuation_of" in open(kt.__file__, encoding="utf-8").read()


def test_closed_parent_continues_only_by_owner_order(board):
    # CONTINUATION_CLOSED_PARENT_20260911: card fechado só continua com allow_closed (ordem do dono); herda prioridade.
    with kb.connect_closing() as conn:
        parent = _card(conn, board, 7)
        conn.execute("UPDATE tasks SET priority=100, status='done', completed_at=?, worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (int(time.time()), parent.id)); conn.commit()
        with pytest.raises(delivery.WorkflowError, match="allow_closed"):
            delivery.create_continuation(conn, parent.id, requester="worker")
        res = delivery.create_continuation(conn, parent.id, title="Reparar o dado", body="O pedido continua errado em produção.", requester="owner", allow_closed=True)
        child = kb.get_task(conn, res["task_id"])
        assert child.status == "ready" and child.priority == 100 and res["existing"] is False
        assert "continua errado" in (child.body or "") and delivery.continuation_links(conn, parent.id)["children"] == [child.id]
        again = delivery.create_continuation(conn, parent.id, requester="owner", allow_closed=True)
        assert again["task_id"] == child.id and again["existing"] is True


def test_concurrent_continuations_yield_one_child(board):
    import threading
    with kb.connect_closing() as conn:
        parent = _card(conn, board, 6)
    results = []
    errors = []

    def worker():
        try:
            with kb.connect_closing() as c:
                for _ in range(3):
                    results.append(delivery.create_continuation(c, parent.id, requester="t")["task_id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not errors, errors
    assert len(set(results)) == 1, set(results)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM nfos_continuations WHERE parent_id=?", (parent.id,)).fetchone()[0] == 1

