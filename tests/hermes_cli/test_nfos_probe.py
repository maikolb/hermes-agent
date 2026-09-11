"""RESULT_PROBE_20260911: critério obrigatório medido no destino. PASS só pela medição; FAIL devolve a nota de debug ao mesmo card;
medição anterior à mutação relevante ou de revisão antiga não vale; sonda corrigível com motivo e evidência; reclassificação recusada."""
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
WRONG = ["Anexo 1 – Distribuição de Vagas", "Anexo 2 – Vagas por Campus", "Língua Portuguesa", "Matemática"]


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


def _probe(query="select name from exam_disciplines", expect=None):
    return {"kind": "sql", "query": query, "expect": expect or {"set_equals": NAMES, "not_matches": "^Anexo"}}


def _spec(mandatory=True, probe=None, **extra):
    crit = [{"id": "C1", "text": "A oferta expõe as 7 disciplinas do Anexo 7, sem anexos administrativos"}, {"id": "C2", "text": "Cargos válidos preservados"}]
    if mandatory:
        crit[0].update(mandatory=True, probe=probe or _probe())
    spec = {"goal": "Disciplinas corretas na oferta", "criteria": crit, "steps": ["Reparar a população"], "delivery_type": "operation",
            "size": "P", "operation": {"target": "produção", "mutation": "vínculos de disciplina"}, "no_code_reason": "reparo de dado"}
    spec.update(extra)
    return spec


def _card(conn, n=1, *, method="sql", spec=None):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(70 + n)},
        text=f"Disciplinas erradas {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "produção: exam_disciplines", "method": method, "result": "4 anexos, 0 vínculos"}], "verdict": "not_delivered"})
    if spec is not None:
        delivery.save_spec(conn, task.id, task.current_run_id, spec, author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _artifact(tmp_path, name="proof.json"):
    p = tmp_path / name
    p.write_text('{"ok": true}', encoding="utf-8")
    return {"id": name, "path": str(p)}


def _report(tmp_path, c1, c2="PASS", **extra):
    a = _artifact(tmp_path)
    rep = {"summary": "relatório", "artifacts": [a],
           "criteria": [{"id": "C1", "status": c1, "evidence": [a["id"]]}, {"id": "C2", "status": c2, "evidence": [a["id"]]}]}
    rep.update(extra)
    return rep


def _stored(conn, task_id):
    return json.loads(delivery._artifact(conn, task_id, "report")["content"])


def _repair(conn, task, label="snap-1"):
    eff = delivery.begin_effect(conn, task.id, task.current_run_id, operation="repair", target="produção: exam_disciplines", candidate=label)
    delivery.reconcile_effect(conn, eff["id"], found=True, evidence={"readback": {"rows": 7}, "candidate": label}, caller_task_id=task.id, caller_run_id=task.current_run_id)


def test_spec_requires_mandatory_probe_when_precheck_measured(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        with pytest.raises(delivery.WorkflowError, match="mandatory=true"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(mandatory=False), author="worker", evidence={"source": "worker"})
        assert delivery.save_spec(conn, task.id, task.current_run_id, _spec(), author="worker", evidence={"source": "worker"}) == 1


def test_verbose_precheck_method_counts_as_measured(board):
    # MEASURED_PRECHECK_20260911: "authenticated Playwright readback https://admin..." e "curl -L" são reprodução medida.
    with kb.connect_closing() as conn:
        task = _card(conn, 11, method="authenticated Playwright readback https://admin.example/notice-uploads/9cc4 at 16:49Z")
        with pytest.raises(delivery.WorkflowError, match="mandatory=true"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(mandatory=False), author="worker", evidence={"source": "worker"})
        task2 = _card(conn, 12, method="leitura do card e do histórico do pai")
        assert delivery.save_spec(conn, task2.id, task2.current_run_id, _spec(mandatory=False), author="worker", evidence={"source": "worker"}) == 1


def test_probe_validation_rejects_mutation_and_weak_expectation(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 2)
        with pytest.raises(delivery.WorkflowError, match="SELECT"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe=_probe(query="delete from exam_disciplines")), author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="expect"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe={"kind": "sql", "query": "select 1", "expect": {}}), author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="not evaluated"):  # PROBE_EXPECT_KEYS_20260911
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe={"kind": "http", "url": "https://admin.example/api/health", "expect": {"json": {"ok": True}, "status": 200}}), author="worker", evidence={"source": "worker"})
        with pytest.raises(delivery.WorkflowError, match="status alone"):  # PROBE_STRICT_EXPECT_20260911
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe={"kind": "http", "url": "https://admin.example/api/x", "expect": {"status": 200}}), author="worker", evidence={"source": "worker"})


def test_declared_pass_is_replaced_by_the_measurement(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 3, spec=_spec())
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS"))
        row = _stored(conn, task.id)["criteria"][0]
        assert row["status"] == "NOT_RUN" and row["declared_status"] == "PASS"
        assert delivery.completion_evidence_check(conn, task.id) is None
        note = delivery.completion_refusal_note(conn, task.id)
        assert "C1" in note and "não medido" in note and "probe --criterion" in note


def test_fail_then_repair_then_pass(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 4, spec=_spec())
        _names(board, WRONG)
        res = delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        assert res[0]["state"] == "FAIL" and "Anexo 1" in json.dumps(res[0]["observed"], ensure_ascii=False)
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS"))
        assert _stored(conn, task.id)["criteria"][0]["status"] == "FAIL"
        note = delivery.completion_refusal_note(conn, task.id)
        assert "observado" in note and "Anexo 1" in note
        assert delivery.completion_evidence_check(conn, task.id) is None
        _names(board, NAMES)
        _repair(conn, task)
        # a medição antiga não vale depois da mutação: precisa medir de novo
        assert delivery.mandatory_pending(conn, task.id)[0]["status"] == "NOT_RUN"
        res = delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        assert res[0]["state"] == "PASS"
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "FAIL"))  # o worker declarou errado: vale a medição
        stored = _stored(conn, task.id)
        assert stored["criteria"][0]["status"] == "PASS" and "probe:C1" in stored["criteria"][0]["evidence"]
        assert stored["measurements"]["C1"]["state"] == "PASS"
        assert delivery.completion_evidence_check(conn, task.id) is not None
        assert delivery.completion_refusal_note(conn, task.id) is None


def test_measurement_before_mutation_is_stale(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 5, spec=_spec())
        _names(board, NAMES)
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")[0]["state"] == "PASS"
        time.sleep(1.1)
        _repair(conn, task, "snap-2")
        pending = delivery.mandatory_pending(conn, task.id)
        assert pending and "predates" in pending[0]["why"]


def test_reclassification_of_mandatory_is_refused(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 6, spec=_spec())
        _names(board, WRONG)
        delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "FAIL"))
        with pytest.raises(delivery.WorkflowError, match="reclassification"):
            delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS", reclassified=[
                {"id": "C1", "previous": "FAIL", "reason": "li errado", "evidence": ["proof.json"]}]))


def test_probe_correction_needs_reason_and_invalidates_old_pass(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 7, spec=_spec())
        _names(board, NAMES)
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")[0]["state"] == "PASS"
        better = _probe(query="select name from exam_disciplines where name is not null")
        with pytest.raises(delivery.WorkflowError, match="probe_corrections"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe=better), author="worker", evidence={"source": "worker"})
        rev = delivery.save_spec(conn, task.id, task.current_run_id, _spec(probe=better, probe_corrections=[
            {"id": "C1", "reason": "a consulta anterior lia a tabela errada", "evidence": ["proof.json"]}]), author="worker", evidence={"source": "worker"})
        assert rev == 2
        pending = delivery.mandatory_pending(conn, task.id)
        assert pending and "earlier spec revision" in pending[0]["why"]
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS"))
        assert _stored(conn, task.id)["criteria"][0]["status"] == "NOT_RUN"


def test_attempts_appear_in_the_note(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 8, spec=_spec())
        _names(board, WRONG)
        delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        wf = delivery.get_workflow(conn, task.id); st = json.loads(wf["state_json"] or "{}")
        st["attempts"] = list(st.get("attempts") or []) + [{"at": int(time.time()), "hypothesis": "appliesTo vazio nos grupos", "change": "preencher appliesTo do grupo 2"}]
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task.id)); conn.commit()
        note = delivery.completion_refusal_note(conn, task.id)
        assert "appliesTo vazio nos grupos" in note and "Tentativas registradas: 1" in note


def test_edge_challenge_is_indeterminate_not_fail(board, monkeypatch):
    # PROBE_HARDENING_20260911: desafio da Vercel (x-vercel-mitigated / 429) não é resultado funcional.
    with kb.connect_closing() as conn:
        task = _card(conn, 13, spec=_spec(probe={"kind": "http", "url": "https://app.example/api/x", "expect": {"contains_all": ["ok"]}}))
        monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env: {"status": 429, "mitigated": "x-vercel-mitigated=challenge", "value": ""})
        res = delivery.run_probes(conn, task.id, task.current_run_id, criterion="C1")
        assert res[0]["state"] == "INDETERMINADO" and "edge protection" in res[0]["error"]
        assert delivery.mandatory_pending(conn, task.id)[0]["status"] == "NOT_RUN"


def test_cancellation_by_owner_still_closes(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 9, spec=_spec())
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "NOT_RUN", disposition="cancelled_by_owner"))
        assert _stored(conn, task.id)["criteria"][0]["status"] == "NOT_RUN"
        assert delivery.completion_evidence_check(conn, task.id) is not None


def test_outside_owner_mode_nothing_changes(board, monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        task = _card(conn, 10)
        delivery.save_spec(conn, task.id, task.current_run_id, _spec(mandatory=False), author="worker", evidence={"source": "worker"})
        delivery.save_report(conn, task.id, task.current_run_id, _report(board, "PASS"))
        assert _stored(conn, task.id)["criteria"][0]["status"] == "PASS"
        assert delivery.completion_refusal_note(conn, task.id) is None


def test_worker_protocol_mentions_the_probe(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert "probe --criterion" in text and "mandatory=true" in text and "probe_corrections" in text
