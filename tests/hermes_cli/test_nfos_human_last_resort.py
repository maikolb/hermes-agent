"""HUMAN_LAST_RESORT_20260914: pergunta a humano é o último recurso. Sonda, medição, destino e manutenção do runtime não vão a humano;
pergunta desse tipo parada volta ao Principal; a credencial da sonda vai para o cofre do projeto pelo probe-env; a sonda obrigatória mede o
host do destino e nunca uma cópia local; destino fora do ar espera sem pergunta e o card volta sozinho quando ele responde."""
import json
import os
import stat
import time
import urllib.error
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime

TARGET = "https://infotributos.example.com"


def _project(**extra):
    def config(board, config=None):
        return dict({"enabled": True, "board": board, "project_id": "infotributos", "delivery_environment": "test"}, **extra)
    return config


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    (tmp_path / "secrets").mkdir()
    monkeypatch.setattr(runtime, "project_config", _project())
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _http_probe(url=TARGET + "/api/auth/profile-summary"):
    return {"kind": "http", "url": url, "headers": {"Cookie": "$env:PROFILE_COOKIE"}, "json_path": "plano_atual.tipo",
            "expect": {"equals": "pacote_ativo"}}


def _spec(probe):
    return {"goal": "Validade do plano registrada", "size": "P", "delivery_type": "operation", "steps": ["medir no destino"],
            "operation": {"target": "TEST", "mutation": "validade do plano"}, "no_code_reason": "teste do contrato",
            "delivery_destination": {"environment": "test", "target": TARGET, "source": "origin/main",
                                     "authorization_message": "pedido do cliente", "verification_operation": "deploy"},
            "criteria": [{"id": "AC-01", "text": "Compra aprovada persiste a validade do plano", "mandatory": True, "probe": probe}]}


def _card(conn, n, spec=None):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "3234", "message_id": str(900 + n)},
        text=f"Registrar a validade do plano {n}.",
        project={"board": "infotributos-board", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    delivery.record_precheck(conn, task.id, task.current_run_id, {
        "checked": [{"target": "TEST: /api/auth/profile-summary", "method": "http", "result": "sem validade"}], "verdict": "not_delivered"})
    if spec is not None:
        delivery.save_spec(conn, task.id, task.current_run_id, spec, author="worker", evidence={"source": "worker"})
    return kb.get_task(conn, task.id)


def _worker_gone(conn, task, monkeypatch):
    """O worker do run saiu: processo morto e card de volta à fila."""
    monkeypatch.setattr(delivery, "_run_process_alive", lambda conn, task_id, run_id: False)
    monkeypatch.setattr(runtime, "run_termination_pending", lambda conn, task_id, run_id: False)
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL WHERE id=?", (task.id,))
    conn.commit()


def test_product_quota_word_is_not_a_rate_limit_and_probe_questions_get_the_route():
    question = ("A prova funcional composta foi executada: login normal + profile-summary HTTP 200 confirma cliente/pacote/cota. "
                "O fechamento continua bloqueado porque sua sonda HTTP recebeu 401/INDETERMINADO.")
    answer = delivery._auto_continue_answer("impediment", question)
    assert answer and "rate limit" not in answer and "probe-env" in answer and "host do destino" in answer
    assert "rate limit" in delivery._auto_continue_answer("impediment", "A API devolveu HTTP 429 Too Many Requests")
    assert "rate limit" in delivery._auto_continue_answer("impediment", "cota excedida no provedor de modelo")
    assert delivery._auto_continue_answer("impediment", "Qual cargo exato está afetado, cota do cliente 12?") is None


def test_human_about_probe_or_runtime_is_refused_but_a_business_question_is_not(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Qual plano Hotmart conta como pago?", context={})
        _worker_gone(conn, task, monkeypatch)
        with pytest.raises(delivery.WorkflowError, match="HUMAN_LAST_RESORT_20260914"):
            delivery.resolve_decision(conn, decision, action="human", author="Principal",
                answer="PERGUNTA para Maikol: pode configurar no contexto Hermes Runtime o probe_env do Infotributos?\nBLOCK por configuração externa.")
        assert delivery.get_decision(conn, decision)["status"] == "pending"
        delivery.resolve_decision(conn, decision, action="human", author="Principal",
                                  answer="PERGUNTA para Jhonatan: qual plano Hotmart conta como pago?")
        assert kb.get_task(conn, task.id).status == "blocked"


def test_stuck_probe_question_goes_back_to_the_principal_and_leaves_the_block(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 2)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                          question="Goal-mode worker's output looked complete but it never called kanban_complete", context={})
        _worker_gone(conn, task, monkeypatch)
        legacy = "PERGUNTA para Maikol: pode configurar kanban.delivery.projects.infotributos.probe_env para carregar PROFILE_COOKIE?\nBLOCK."
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=?,resolved_at=? WHERE id=?",
                     (legacy, int(time.time()) - 3 * 86400, decision))
        conn.commit()
        assert kb.block_task(conn, task.id, reason=legacy, kind="needs_input")
        assert kb.get_task(conn, task.id).status == "blocked"

        delivery.reconcile_human_answers(conn)

        old = delivery.get_decision(conn, decision)
        assert old["status"] == "superseded" and json.loads(old["context"])["superseded_reason"] == "human_last_resort"
        pending = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchall()
        assert len(pending) == 1
        assert "HUMAN_LAST_RESORT_20260914" in pending[0]["question"] and "probe_env" in pending[0]["question"]
        assert kb.get_task(conn, task.id).status == "ready"
        wakes = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'", (task.id,))]
        assert any(w.get("decision_id") == pending[0]["id"] for w in wakes)
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_human_question_refused'", (task.id,)).fetchone()[0] == 1

        delivery.reconcile_human_answers(conn)  # idempotente: nada novo no segundo tick
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchone()[0] == 1


def test_probe_env_stores_the_credential_in_the_project_vault_and_the_probe_reads_it(board, monkeypatch):
    seen = {}

    def fake_http(probe, env, **kw):
        seen["cookie"] = env.get("PROFILE_COOKIE")
        return {"status": 200, "mitigated": None, "value": "pacote_ativo"}

    monkeypatch.setattr(delivery, "_run_http_probe", fake_http)
    with kb.connect_closing() as conn:
        task = _card(conn, 3, spec=_spec(_http_probe()))
        with pytest.raises(delivery.WorkflowError, match="UPPER_SNAKE_CASE"):
            delivery.probe_env_command(conn, task.id, set_name="profile-cookie", value="x")
        with pytest.raises(delivery.WorkflowError, match="one non-empty line"):
            delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="\n")
        result = delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="sid=s3cr3t-value\n")
        assert result["saved"] and "s3cr3t" not in json.dumps(result)
        vault = board / "secrets" / "infotributos" / "probe.env"
        assert "PROFILE_COOKIE=sid=s3cr3t-value" in vault.read_text(encoding="utf-8")
        if os.name == "posix":
            assert stat.S_IMODE(vault.stat().st_mode) == 0o600
        payloads = [r[0] for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_probe_env_changed'", (task.id,))]
        assert payloads and all("s3cr3t" not in p for p in payloads)
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")[0]["state"] == "PASS"
        assert seen["cookie"] == "sid=s3cr3t-value"
        assert delivery.probe_env_command(conn, task.id)["names"] == ["PROFILE_COOKIE"]
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_outro_card")
        with pytest.raises(delivery.WorkflowError, match="card of this worker"):
            delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="x")


def test_configured_probe_env_wins_over_the_vault(board, monkeypatch):
    (board / "secrets" / "maint.env").write_text("PROFILE_COOKIE=do-config\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "project_config", _project(probe_env="maint.env"))
    seen = {}
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: seen.update(cookie=env.get("PROFILE_COOKIE")) or {"status": 200, "mitigated": None, "value": "pacote_ativo"})
    with kb.connect_closing() as conn:
        task = _card(conn, 4, spec=_spec(_http_probe()))
        delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="do-cofre")
        delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")
        assert seen["cookie"] == "do-config"


def test_probe_on_a_local_host_measures_a_copy_not_the_destination(board, monkeypatch):
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: pytest.fail("a local copy must not be measured"))
    local = _http_probe("http://127.0.0.1:13152/api/auth/profile-summary")
    with kb.connect_closing() as conn:
        task = _card(conn, 5)
        with pytest.raises(delivery.WorkflowError, match="local host"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(local), author="worker", evidence={"source": "worker"})
        delivery.save_spec(conn, task.id, task.current_run_id, _spec(_http_probe()), author="worker", evidence={"source": "worker"})
        state, _observed, error = delivery._execute_probe(conn, task.id, local)
        assert state == "INDETERMINADO" and "local host" in error and "infotributos.example.com" in error
        delivery.probe_env_command(conn, task.id, set_name="PROBE_DATABASE_URL", value="postgresql://leitor:x@127.0.0.1:5432/app")
        state, _observed, error = delivery._execute_probe(conn, task.id, {"kind": "sql", "query": "select 1", "expect": {"scalar": 1}})
        assert state == "INDETERMINADO" and "local host" in error
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.20", "app.127.0.0.1.nip.io", "host.docker.internal"):
        assert delivery._local_host(host), host
    for host in ("infotributos.15.229.26.202.nip.io", "app.concursaai.com", "187.127.60.126"):
        assert not delivery._local_host(host), host


def test_local_host_is_allowed_when_the_project_lists_it(board, monkeypatch):
    monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["127.0.0.1"]))
    with kb.connect_closing() as conn:
        task = _card(conn, 6)
        assert delivery._local_probe_problem(conn, task.id, "127.0.0.1") is None


def test_unreachable_destination_waits_without_a_question_and_comes_back_alone(board, monkeypatch):
    def down(probe, env, **kw):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(delivery, "_run_http_probe", down)
    with kb.connect_closing() as conn:
        task = _card(conn, 7, spec=_spec(_http_probe()))
        result = delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")
        assert result[0]["state"] == "INDETERMINADO" and "timed out" in result[0]["error"]
        assert "Destino sem resposta" in delivery.completion_refusal_note(conn, task.id)
        _worker_gone(conn, task, monkeypatch)

        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=8: False)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        blocked = kb.get_task(conn, task.id)
        assert blocked.status == "blocked" and blocked.block_kind == "transient"
        reason = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0])["reason"]
        assert TARGET in reason and "nenhuma pergunta a humano" in reason
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')", (task.id,)).fetchone()[0] == 0

        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=8: pytest.fail("rechecked before 10 min"))
        assert delivery.sweep_destination_waits(conn) == []

        wf = delivery.get_workflow(conn, task.id)
        st = json.loads(wf["state_json"])
        st["destination_wait"]["checked_at"] -= delivery.DESTINATION_RECHECK_SECONDS + 1
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task.id))
        conn.commit()
        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=8: True)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]
        assert kb.get_task(conn, task.id).status == "ready"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,))]
        assert "nfos_destination_wait" in kinds and "nfos_destination_back" in kinds
