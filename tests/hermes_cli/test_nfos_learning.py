"""LEARNING_20260911: FAIL -> PASS vira hipótese automaticamente; o contexto do worker recupera o caso e as lições do projeto
(comprovadas como orientação, hipóteses como hipóteses); promoção exige motivo e evidência."""
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
WRONG = ["Anexo 1 – Distribuição de Vagas", "Língua Portuguesa"]


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
    c = sqlite3.connect(prod); c.execute("create table exam_disciplines(name text)"); c.commit(); c.close()
    (tmp_path / "secrets" / "pilot.env").write_text(f"DATABASE_URL=sqlite://{prod}\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"enabled": True, "board": board, "probe_env": "pilot.env", "delivery_environment": "production"})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _names(tmp_path, names):
    c = sqlite3.connect(tmp_path / "prod.db")
    c.execute("delete from exam_disciplines"); c.executemany("insert into exam_disciplines(name) values (?)", [(n,) for n in names]); c.commit(); c.close()


def _spec():
    return {"goal": "Disciplinas corretas na oferta", "steps": ["Reparar"], "delivery_type": "operation", "size": "P",
            "operation": {"target": "produção", "mutation": "vínculos"}, "no_code_reason": "reparo de dado",
            "criteria": [{"id": "C1", "text": "A oferta expõe as 7 disciplinas", "mandatory": True,
                          "probe": {"kind": "sql", "query": "select name from exam_disciplines", "expect": {"set_equals": NAMES, "not_matches": "^Anexo"}}},
                         {"id": "C2", "text": "Cargos preservados"}]}


def _card(conn, n, target="produção: exam_disciplines", with_spec=True):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(90 + n)},
        text=f"Disciplinas erradas na oferta {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {"checked": [{"target": target, "method": "sql", "result": "errado"}], "verdict": "not_delivered"})
    if with_spec:
        delivery.save_spec(conn, task.id, task.current_run_id, _spec(), author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _attempt(conn, task_id, hypothesis, change):
    wf = delivery.get_workflow(conn, task_id); st = json.loads(wf["state_json"] or "{}")
    st["attempts"] = list(st.get("attempts") or []) + [{"at": int(time.time()), "hypothesis": hypothesis, "change": change}]
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task_id)); conn.commit()


def _repair(conn, task, label):
    eff = delivery.begin_effect(conn, task.id, task.current_run_id, operation="repair", target="produção: exam_disciplines", candidate=label)
    delivery.reconcile_effect(conn, eff["id"], found=True, evidence={"readback": {"rows": 7}, "candidate": label}, caller_task_id=task.id, caller_run_id=task.current_run_id)


def test_fail_then_pass_is_captured_as_hypothesis(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        _names(board, WRONG)
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")[0]["state"] == "FAIL"
        _attempt(conn, task.id, "grupos com appliesTo vazio não materializam", "preencher appliesTo do grupo 2 e repopular")
        _names(board, NAMES)
        _repair(conn, task, "snap-1")
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")[0]["state"] == "PASS"
        lessons = delivery.list_lessons(conn)
        assert len(lessons) == 1 and lessons[0]["kind"] == "hypothesis"
        assert lessons[0]["component"] == "exam_disciplines" and lessons[0]["operation"] == "repair"
        assert "preencher appliesTo" in lessons[0]["text"] and "probe:C1 r1" in lessons[0]["evidence"]


def test_context_recovers_case_and_project_lessons(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 2)
        _names(board, WRONG)
        delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        _attempt(conn, task.id, "snapshot não materializa", "rodar população específica")
        proven = delivery.record_lesson(conn, kind="proven", text="Editar o snapshot não materializa; confira a etapa de população e a rota do aluno.",
                                        component="position_disciplines,notice_extraction_snapshots", condition="reparo de disciplinas de upload existente",
                                        evidence=["population_steps lido 11/09"], limit="verificar listByPosition na versão atual", author="Codex")
        hyp = delivery.record_lesson(conn, kind="hypothesis", text="appliesTo vazio impede vínculo por cargo", component="exam_disciplines", author="runtime")
        other = delivery.record_lesson(conn, kind="hypothesis", text="cache do CDN atrasa a tela", component="/api/cdn", author="runtime")
        ctx = delivery.worker_context(conn, task.id)
        assert "Case context" in ctx and "snapshot não materializa" in ctx and "C1=FAIL" in ctx
        assert "Project lessons" in ctx and f"[L{proven}]" in ctx and "Proven" in ctx
        assert f"[L{hyp}]" in ctx and "Hypotheses" in ctx
        assert f"[L{other}]" not in ctx  # componente não casa com o pedido
        # card novo do mesmo projeto, sem spec: recebe a comprovada e a hipótese cujo componente aparece no precheck
        fresh = _card(conn, 3, with_spec=False)
        ctx2 = delivery.worker_context(conn, fresh.id)
        assert f"[L{proven}]" in ctx2 and f"[L{hyp}]" in ctx2 and f"[L{other}]" not in ctx2
        assert "Attempts already made" not in ctx2  # card novo: só a próxima ação registrada pelo precheck, sem tentativas


def test_promotion_needs_reason_and_evidence(board):
    with kb.connect_closing() as conn:
        lid = delivery.record_lesson(conn, kind="hypothesis", text="h", component="x", author="runtime")
        with pytest.raises(delivery.WorkflowError, match="reason"):
            delivery.promote_lesson(conn, lid, reason="", evidence=["e"], author="Principal")
        with pytest.raises(delivery.WorkflowError, match="reason"):
            delivery.promote_lesson(conn, lid, reason="confirmado em dois cards", evidence=[], author="Principal")
        delivery.promote_lesson(conn, lid, reason="confirmado em dois cards", evidence=["probe:C1 r2 (t_a)", "probe:C1 r3 (t_b)"], author="Principal")
        row = delivery.list_lessons(conn)[0]
        assert row["kind"] == "proven" and row["promoted_by"] == "Principal" and "t_b" in row["evidence"]
        with pytest.raises(delivery.WorkflowError, match="condition"):
            delivery.record_lesson(conn, kind="proven", text="sem condição", evidence=["e"], author="Principal")
        delivery.retire_lesson(conn, lid, reason="contradita pelo caso t_c", author="auditor")
        assert delivery.list_lessons(conn) == [] and delivery.list_lessons(conn, include_retired=True)[0]["kind"] == "retired"


def test_lesson_command_authoring(board):
    with kb.connect_closing() as conn:
        out = delivery.lesson_command(conn, {"op": "add", "kind": "hypothesis", "text": "x", "component": "y"}, author="Principal")
        assert out["lesson_id"] == 1
        assert delivery.lesson_command(conn, {"op": "list"}, author="Principal")["lessons"][0]["author"] == "Principal"
        with pytest.raises(delivery.WorkflowError, match="op"):
            delivery.lesson_command(conn, {"op": "x"}, author="Principal")


def test_protocol_mentions_learning(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert "Project lessons" in text or "project lessons" in text
    assert "captured automatically as a hypothesis" in text
