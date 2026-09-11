"""CLOSURE_RECOVERY_20260911: NOT_RUN não opcional barra o fechamento; revisão da spec não enfraquece; decisão pendente recebe
auto-continue, lembretes e estado visível; rework vira continuação; contrato Git não inicializado não é adulteração; saída
limpa depois de recusa não é violação."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime

NAMES = ["Língua Portuguesa", "Matemática", "Geografia", "História", "Biologia", "Química", "Física"]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    (tmp_path / "secrets").mkdir()
    prod = tmp_path / "prod.db"
    c = sqlite3.connect(prod); c.execute("create table exam_disciplines(name text)"); c.executemany("insert into exam_disciplines(name) values (?)", [(x,) for x in NAMES]); c.commit(); c.close()
    (tmp_path / "secrets" / "pilot.env").write_text(f"DATABASE_URL=sqlite://{prod}\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"enabled": True, "board": board, "probe_env": "pilot.env", "delivery_environment": "production"})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _spec(criteria=None, **extra):
    spec = {"goal": "Resultado no destino", "steps": ["fazer"], "delivery_type": "operation", "size": "P",
            "operation": {"target": "produção", "mutation": "dado"}, "no_code_reason": "reparo",
            "criteria": criteria or [{"id": "C1", "text": "dado corrigido"}, {"id": "C2", "text": "tela conferida"}]}
    spec.update(extra)
    return spec


def _card(conn, n, *, method="leitura do card", spec=None):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(200 + n)},
        text=f"Pedido {n}.", project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {"checked": [{"target": "alvo", "method": method, "result": "errado"}], "verdict": "not_delivered"})
    delivery.save_spec(conn, task.id, task.current_run_id, spec or _spec(), author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _artifact(tmp_path):
    p = tmp_path / "proof.json"; p.write_text('{"ok": true}', encoding="utf-8")
    return {"id": "proof.json", "path": str(p)}


def _report(tmp_path, statuses, **extra):
    a = _artifact(tmp_path)
    rep = {"summary": "relatório", "artifacts": [a], "criteria": [{"id": cid, "status": st, "evidence": [a["id"]]} for cid, st in statuses.items()]}
    rep.update(extra)
    return rep


def test_not_run_non_optional_blocks_and_continuation_or_optional_unblock(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, {"C1": "PASS", "C2": "NOT_RUN"}))
        assert delivery.completion_evidence_check(conn, task.id) is None
        note = delivery.completion_refusal_note(conn, task.id)
        assert "C2" in note and "requisito não opcional" in note
        child = delivery.create_continuation(conn, task.id, requester=task.id)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, {"C1": "PASS", "C2": "NOT_RUN"}, partial_delivery=True, continuation=child["task_id"]))
        assert delivery.completion_evidence_check(conn, task.id) is not None
        # optional na primeira revisão, com motivo
        task2 = _card(conn, 2, spec=_spec([{"id": "C1", "text": "dado"}, {"id": "C2", "text": "tela", "optional": True, "optional_reason": "sem acesso à tela; medido pelo dado"}]))
        delivery.save_report(conn, task2.id, task2.current_run_id, _report(board, {"C1": "PASS", "C2": "NOT_RUN"}))
        assert delivery.completion_evidence_check(conn, task2.id) is not None
        with pytest.raises(delivery.WorkflowError, match="optional_reason"):
            _card(conn, 3, spec=_spec([{"id": "C1", "text": "dado", "optional": True}]))


def test_revision_cannot_drop_mandatory_or_weaken_or_remove(board):
    probe = {"kind": "sql", "query": "select name from exam_disciplines", "expect": {"set_equals": NAMES}}
    with kb.connect_closing() as conn:
        task = _card(conn, 4, method="sql", spec=_spec([{"id": "C1", "text": "7 disciplinas", "mandatory": True, "probe": probe}, {"id": "C2", "text": "tela"}]))
        with pytest.raises(delivery.WorkflowError, match="stays mandatory"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec([{"id": "C1", "text": "7 disciplinas", "probe": probe}, {"id": "C2", "text": "tela"}]), author="worker", evidence={"source": "worker"})
        weaker = dict(probe, expect={"count_between": [7, 7]})
        with pytest.raises(delivery.WorkflowError, match="weaker"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec([{"id": "C1", "text": "7 disciplinas", "mandatory": True, "probe": weaker}, {"id": "C2", "text": "tela"}],
                                                                         probe_corrections=[{"id": "C1", "reason": "x", "evidence": ["e"]}]), author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="disappear"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec([{"id": "C1", "text": "7 disciplinas", "mandatory": True, "probe": probe}]), author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="optional"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec([{"id": "C1", "text": "7 disciplinas", "mandatory": True, "probe": probe}, {"id": "C2", "text": "tela", "optional": True, "optional_reason": "x"}]), author="worker", evidence={"source": "worker"})


def _pending(conn, task, question, created_at):
    did = "dec_" + os.urandom(6).hex()
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                 (did, task.id, task.current_run_id, "impediment", question, "{}", 1, created_at, "pending"))
    conn.commit()
    return did


def test_pending_decision_auto_continue_reminders_and_visible_stall(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 5)
        old = int(time.time()) - 700
        auto = _pending(conn, task, "provider returned rate limit 429; should I wait?", old)
        plain = _pending(conn, task, "Como habilitar a geração na fixture HML para a identidade QA?", old)
        out = dict(delivery.nudge_open_decisions(conn, task.id))
        assert out[auto] == "auto_continue" and out[plain] == "reminder 1"
        assert conn.execute("SELECT status FROM nfos_decisions WHERE id=?", (auto,)).fetchone()[0] == "resolved"
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'", (task.id,)).fetchone()[0] >= 1
        # sem novo lembrete dentro do intervalo
        assert delivery.nudge_open_decisions(conn, task.id) == []
        # três lembretes antigos: estado visível com responsável, e o card sai sozinho quando a decisão é resolvida
        ctx = {"reminders": [old - 3000, old - 2000, old - 1000]}
        conn.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(ctx), plain)); conn.commit()
        conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,)); conn.commit()
        out = dict(delivery.nudge_open_decisions(conn, task.id))
        assert out[plain] == "stalled"
        t = kb.get_task(conn, task.id)
        assert t.status == "blocked" and t.block_kind == "awaiting_principal"
        assert "Responsável" in (delivery.get_workflow(conn, task.id)["next_action"] or "")
        delivery.resolve_decision(conn, plain, action="continue", answer="use a conta QA do cofre", author="Principal")
        assert task.id in delivery.sweep_awaiting_principal(conn)
        assert kb.get_task(conn, task.id).status == "ready"


def test_rework_creates_formal_continuation_idempotently(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 6)
        conn.execute("UPDATE tasks SET status='done', completed_at=?, worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (int(time.time()), task.id)); conn.commit()
        res = delivery.request_rework(conn, task.id, criterion="C1", reason="registro ainda contém -1 errado", evidence=["registro 3fa80d2b campo penalty", "leitura 21:05Z"], author="auditor")
        child = kb.get_task(conn, res["task_id"])
        assert child.status == "ready" and "registro 3fa80d2b" in (child.body or "") and "critério obrigatório" in (child.body or "")
        again = delivery.request_rework(conn, task.id, criterion="C1", reason="idem", evidence=["e"], author="auditor")
        assert again["task_id"] == child.id and again["existing"] is True
        monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
        with pytest.raises(delivery.WorkflowError, match="not by the worker"):
            delivery.request_rework(conn, task.id, criterion="C1", reason="x", evidence=["e"], author="worker")


def test_uninitialized_contract_and_refused_exit_helpers(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 7)
        now = int(time.time())
        assert kb._nfos_delivery_recorded(conn, task.id) is False
        conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,created_at,updated_at) VALUES('e0',?,?,'repair','produção','pre-1','confirmed',?,?)", (task.id, task.current_run_id, now, now)); conn.commit()
        assert kb._nfos_delivery_recorded(conn, task.id) is False  # repair não é publicação
        conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,created_at,updated_at) VALUES('e1',?,?,'merge','https://github.com/x/y','abc1234','unknown',?,?)", (task.id, task.current_run_id, now, now)); conn.commit()
        assert kb._nfos_delivery_recorded(conn, task.id) is False  # só efeito confirmado
        conn.execute("UPDATE nfos_effects SET status='confirmed' WHERE id='e1'"); conn.commit()
        assert kb._nfos_delivery_recorded(conn, task.id) is True
        conn.execute("UPDATE tasks SET worker_started_at=? WHERE id=?", (float(now - 5), task.id)); conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
        assert kb._nfos_refused_in_run(conn, row) is False
        kb._append_event(conn, task.id, "nfos_completion_refused", {"pending": ["C1"]}, run_id=task.current_run_id); conn.commit()
        assert kb._nfos_refused_in_run(conn, row) is True
        assert kb._nfos_refused_streak(conn, task.id) == 0
        for i in range(2):
            conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES(?,?,?)", (task.id, "refused_exit", now - 100 + i))
        conn.commit()
        assert kb._nfos_refused_streak(conn, task.id) == 2


def test_protocol_mentions_closing_rule(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert "11. Closing rule" in text and "optional_reason" in text
