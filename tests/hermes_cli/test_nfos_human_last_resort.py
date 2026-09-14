"""HUMAN_LAST_RESORT_20260914: pergunta a humano é o último recurso. Configuração do runtime nunca vai a humano; sonda e medição não vão
ao dono nem à manutenção; pergunta desse tipo parada volta ao Principal; a credencial da sonda vai para o cofre do projeto pelo probe-env e
só viaja para o destino ou probe_hosts; a sonda obrigatória mede o destino, nunca cópia local; destino fora do ar espera sem pergunta,
volta com duas respostas seguidas e, depois de 1 h, o Principal procura quem opera o destino."""
import json
import os
import stat
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
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
def dns(monkeypatch):
    table = {}
    monkeypatch.setattr(delivery, "_resolve_host", lambda host: table.get(host, []))
    return table


@pytest.fixture
def board(tmp_path, monkeypatch, dns):
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
    request = delivery.reserve_request(conn, capacity=16)
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


def _down(probe, env, **kw):
    raise urllib.error.URLError("timed out")


def _park(conn, task, monkeypatch):
    """Mede com o destino fora do ar e deixa o card na fila, como depois de um run recusado."""
    monkeypatch.setattr(delivery, "_run_http_probe", _down)
    delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")
    _worker_gone(conn, task, monkeypatch)


def _state(conn, task_id):
    return json.loads(delivery.get_workflow(conn, task_id)["state_json"])


def _shift(conn, task_id, **fields):
    st = _state(conn, task_id)
    st["destination_wait"].update(fields)
    conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task_id))
    conn.commit()


def _reachable(value):
    return lambda target, timeout=5: value


# --- perguntas a humano ---------------------------------------------------------------------------------------------------------------

def test_product_words_are_not_rate_limits_and_probe_questions_get_the_route():
    auto = lambda q: delivery._auto_continue_answer("impediment", q)  # noqa: E731
    question = ("A prova funcional composta foi executada: login normal + profile-summary HTTP 200 confirma cliente/pacote/cota. "
                "O fechamento continua bloqueado porque sua sonda HTTP recebeu 401/INDETERMINADO.")
    answer = auto(question)
    assert answer and "rate limit" not in answer and "probe-env" in answer and "host do destino" in answer
    assert "rate limit" in auto("A API devolveu HTTP 429 Too Many Requests")
    assert "rate limit" in auto("cota excedida no provedor de modelo")
    assert auto("Qual cargo exato está afetado, cota do cliente 12?") is None
    assert auto("O plano grátis tem limite de uso de 10 consultas por mês; mantenho?") is None
    assert auto("O probe_env do projeto já está certo para o AC-02?") is None
    assert "probe-env" in auto("O projeto não tem probe_env configurado para carregar o cookie")


def test_question_part_classifies_runtime_config_and_owner_measurement_but_lets_operator_actions_through():
    refused = [
        "PERGUNTA para Maikol:\nprecisa configurar o probe_env?",
        "PERGUNTA para Maikol: pode configurar no contexto Hermes Runtime o probe_env do Infotributos?\nBLOCK.",
        "PERGUNTA para Maikol: a sonda do TEST falhou, o que faço?",
        "PERGUNTA para Jhonatan: pode configurar o probe_env do projeto?",
        "PERGUNTA ao mantenedor: o dispatcher travou?",
    ]
    allowed = [
        "PERGUNTA para Jhonatan: pode religar a VPS do TEST? A sonda não alcança 15.229.26.202 e o runtime espera.",
        "PERGUNTA para Jhonatan: qual plano Hotmart conta como pago?",
        "PERGUNTA para Maikol: autoriza formatar o EBS vazio?",
        "PERGUNTA para Jhonatan: o servidor do TEST foi desligado e qual é o endereço atual?",
    ]
    for text in refused:
        assert delivery._human_is_maintenance(text), text
    for text in allowed:
        assert not delivery._human_is_maintenance(text), text


def test_human_about_the_runtime_is_refused_but_a_business_question_is_not(board, monkeypatch):
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


# --- cofre da sonda e escopo ----------------------------------------------------------------------------------------------------------

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
            assert not [p.name for p in vault.parent.iterdir() if p.name.startswith(".probe.env.")]  # temporário não sobra
        payloads = [r[0] for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_probe_env_changed'", (task.id,))]
        assert payloads and all("s3cr3t" not in p for p in payloads)
        assert delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")[0]["state"] == "PASS"
        assert seen["cookie"] == "sid=s3cr3t-value"
        assert delivery.probe_env_command(conn, task.id)["names"] == ["PROFILE_COOKIE"]
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_outro_card")
        with pytest.raises(delivery.WorkflowError, match="card of this worker"):
            delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="x")


def test_probe_env_refuses_a_symlinked_vault(board, monkeypatch):
    if os.name != "posix":
        pytest.skip("symlink semantics")
    with kb.connect_closing() as conn:
        task = _card(conn, 4, spec=_spec(_http_probe()))
        real = board / "elsewhere"
        real.mkdir()
        (board / "secrets" / "infotributos").symlink_to(real, target_is_directory=True)
        with pytest.raises(delivery.WorkflowError, match="symlinked"):
            delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="x")
        assert not list(real.iterdir())


def test_configured_probe_env_wins_over_the_vault(board, monkeypatch):
    (board / "secrets" / "maint.env").write_text("PROFILE_COOKIE=do-config\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "project_config", _project(probe_env="maint.env"))
    seen = {}
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: seen.update(cookie=env.get("PROFILE_COOKIE")) or {"status": 200, "mitigated": None, "value": "pacote_ativo"})
    with kb.connect_closing() as conn:
        task = _card(conn, 5, spec=_spec(_http_probe()))
        delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="do-cofre")
        delivery.run_probes(conn, task.id, task.current_run_id, criterion="AC-01")
        assert seen["cookie"] == "do-config"


def test_vault_credentials_only_travel_to_the_destination_or_probe_hosts(board, monkeypatch):
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: pytest.fail("credential must not leave the destination"))
    with kb.connect_closing() as conn:
        task = _card(conn, 6, spec=_spec(_http_probe()))
        delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="sid=x")
        state, _observed, error = delivery._execute_probe(conn, task.id, _http_probe("https://coleta.example.net/api"))
        assert state == "INDETERMINADO" and "only sent to the delivery destination" in error


def test_mandatory_http_probes_must_include_the_destination(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 7)
        with pytest.raises(delivery.WorkflowError, match="must measure the delivery destination"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(_http_probe("https://coleta.example.net/api")), author="worker", evidence={"source": "worker"})
        spec = _spec(_http_probe())
        spec["criteria"].append({"id": "AC-02", "text": "Portal de suporte responde", "mandatory": True,
                                 "probe": {"kind": "http", "url": "https://suporte.example.net/health", "json_path": "status", "expect": {"equals": "ok"}}})
        assert delivery.save_spec(conn, task.id, task.current_run_id, spec, author="worker", evidence={"source": "worker"}) == 1


def test_probe_on_a_local_host_measures_a_copy_not_the_destination(board, monkeypatch):
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: pytest.fail("a local copy must not be measured"))
    local = _http_probe("http://127.0.0.1:13152/api/auth/profile-summary")
    with kb.connect_closing() as conn:
        task = _card(conn, 8)
        with pytest.raises(delivery.WorkflowError, match="local host"):
            delivery.save_spec(conn, task.id, task.current_run_id, _spec(local), author="worker", evidence={"source": "worker"})
        delivery.save_spec(conn, task.id, task.current_run_id, _spec(_http_probe()), author="worker", evidence={"source": "worker"})
        state, _observed, error = delivery._execute_probe(conn, task.id, local)
        assert state == "INDETERMINADO" and "local host" in error and "infotributos.example.com" in error
        delivery.probe_env_command(conn, task.id, set_name="PROBE_DATABASE_URL", value="postgresql://leitor:x@127.0.0.1:5432/app")
        state, _observed, error = delivery._execute_probe(conn, task.id, {"kind": "sql", "query": "select 1", "expect": {"scalar": 1}})
        assert state == "INDETERMINADO" and "local host" in error
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.20", "app.127.0.0.1.nip.io", "host.docker.internal", "169.254.169.254"):
        assert delivery._local_host(host), host
    for host in ("infotributos.15.229.26.202.nip.io", "app.concursaai.com", "187.127.60.126"):
        assert not delivery._local_host(host), host


def test_names_resolving_to_private_addresses_and_libpq_dsns_count_as_local(board, monkeypatch, dns):
    dns["interno.example.com"] = ["10.0.0.9"]
    dns["banco.example.com"] = ["15.229.1.1"]
    assert delivery._local_host("interno.example.com")
    assert not delivery._local_host("banco.example.com")
    assert delivery._dsn_hosts("host=127.0.0.1 dbname=app user=leitor") == ["127.0.0.1"]
    assert delivery._dsn_hosts("postgresql:///app?host=/var/run/postgresql") == ["localhost"]
    assert delivery._dsn_hosts("postgresql://leitor:x@banco.example.com:5432/app") == ["banco.example.com"]
    with kb.connect_closing() as conn:
        task = _card(conn, 9, spec=_spec(_http_probe()))
        delivery.probe_env_command(conn, task.id, set_name="PROBE_DATABASE_URL", value="host=127.0.0.1 dbname=app user=leitor")
        state, _observed, error = delivery._execute_probe(conn, task.id, {"kind": "sql", "query": "select 1", "expect": {"scalar": 1}})
        assert state == "INDETERMINADO" and "local host" in error


def test_local_host_is_allowed_when_the_project_lists_it(board, monkeypatch):
    monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["127.0.0.1"]))
    with kb.connect_closing() as conn:
        task = _card(conn, 10)
        assert delivery._local_probe_problem(conn, task.id, "127.0.0.1") is None


# --- destino fora do ar ---------------------------------------------------------------------------------------------------------------

def test_destination_check_never_probes_internal_addresses_nor_follows_redirects(monkeypatch):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(delivery, "_resolve_host", lambda host: [])
        assert delivery._destination_reachable("http://169.254.169.254/latest/meta-data") is False
        assert delivery._destination_reachable(f"http://127.0.0.1:{server.server_port}/") is False and hits == []
        monkeypatch.setattr(delivery, "_local_host", lambda host: False)
        assert delivery._destination_reachable(f"http://127.0.0.1:{server.server_port}/") is True
        assert hits == ["/"]
    finally:
        server.shutdown()


def test_unreachable_destination_parks_and_needs_two_answers_to_come_back(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 11, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        assert "Destino sem resposta" in delivery.completion_refusal_note(conn, task.id)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        blocked = kb.get_task(conn, task.id)
        assert blocked.status == "blocked" and blocked.block_kind == "transient"
        reason = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0])["reason"]
        assert TARGET in reason and "quem opera o destino" in reason
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')", (task.id,)).fetchone()[0] == 0

        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=5: pytest.fail("rechecked before 10 min"))
        assert delivery.sweep_destination_waits(conn) == []

        _shift(conn, task.id, next_check_at=0)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked" and _state(conn, task.id)["destination_wait"]["up_at"]

        _shift(conn, task.id, next_check_at=0, up_at=int(time.time()) - delivery.DESTINATION_CONFIRM_SECONDS - 1)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]
        assert kb.get_task(conn, task.id).status == "ready"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,))]
        assert "nfos_destination_wait" in kinds and "nfos_destination_back" in kinds


def test_destination_down_for_an_hour_asks_the_principal_to_reach_its_operator_once(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 12, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
        assert (task.id, "escalated") in delivery.sweep_destination_waits(conn)
        pending = conn.execute("SELECT question FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchall()
        assert len(pending) == 1 and "quem opera esse destino" in pending[0][0] and "endereço atual" in pending[0][0]
        assert delivery._auto_continue_answer("impediment", pending[0][0]) is None
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_destination_escalated'", (task.id,)).fetchone()[0] == 1
        delivery.sweep_destination_waits(conn)
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchone()[0] == 1


def test_destination_wait_never_releases_a_block_it_does_not_own(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 13, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        assert kb.unblock_task(conn, task.id)
        assert kb.block_task(conn, task.id, reason="Janela de manutenção do provedor em andamento", kind="transient")
        _shift(conn, task.id, next_check_at=0)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked"
        assert "destination_wait" not in _state(conn, task.id)


def test_round_one_wait_without_block_id_is_adopted(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 14, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        st = _state(conn, task.id)
        for key in ("block_event_id", "next_check_at"):
            st["destination_wait"].pop(key, None)
        st["destination_wait"]["checked_at"] = 0
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task.id))
        conn.commit()
        delivery.sweep_destination_waits(conn)
        assert _state(conn, task.id)["destination_wait"].get("block_event_id")
        assert kb.get_task(conn, task.id).status == "blocked"


def test_sweep_checks_at_most_a_few_destinations_per_pass(board, monkeypatch):
    calls = []
    with kb.connect_closing() as conn:
        for n in range(15, 20):
            task = _card(conn, n, spec=_spec(_http_probe()))
            _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=5: calls.append(target) or False)
        first = delivery.sweep_destination_waits(conn)
        assert len(calls) == delivery.DESTINATION_MAX_CHECKS_PER_SWEEP
        assert len(first) == delivery.DESTINATION_MAX_CHECKS_PER_SWEEP


def test_a_human_answer_after_the_measurement_lets_the_worker_act_before_parking(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 20, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        with kb.write_txn(conn):
            kb._append_event(conn, task.id, "nfos_human_answered", {"answer": "O novo ip é 15.229.26.203", "author": "Jhonatan"})
        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=5: pytest.fail("must not park after a human answer"))
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "ready"


def test_second_wait_on_the_same_card_follows_the_recurrence_block(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 21, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _shift(conn, task.id, next_check_at=0)
        delivery.sweep_destination_waits(conn)
        _shift(conn, task.id, next_check_at=0, up_at=int(time.time()) - delivery.DESTINATION_CONFIRM_SECONDS - 1)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]

        conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (task.current_run_id, task.id))  # novo run mede de novo
        conn.commit()
        _park(conn, task, monkeypatch)
        st = _state(conn, task.id)
        st["destination_check"]["next_check_at"] = 0
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task.id))
        conn.commit()
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        loop_id = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected')", (task.id,)).fetchone()[0]
        assert _state(conn, task.id)["destination_wait"]["block_event_id"] == loop_id

        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _shift(conn, task.id, next_check_at=0)
        delivery.sweep_destination_waits(conn)
        _shift(conn, task.id, next_check_at=0, up_at=int(time.time()) - delivery.DESTINATION_CONFIRM_SECONDS - 1)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]


# --- revisão 3 (rejulgamento 7e702b57) --------------------------------------------------------------------------------------------------

def test_question_part_keeps_the_whole_first_line_when_it_already_asks():
    assert delivery._human_is_maintenance("PERGUNTA para Maikol: você pode me ajudar? A credencial da sonda de medição não está no cofre")
    assert not delivery._human_is_maintenance("PERGUNTA para Jhonatan: pode religar a VPS do TEST?\nA sonda do NFOS e o probe_env seguem como estão.")


def test_escalation_waits_for_an_open_decision_instead_of_burning_it(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 22, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES('dec_outro',?,?,?,?,?,?,?)",
                     (task.id, task.current_run_id, "impediment", "Outro assunto?", "{}", 1, int(time.time())))
        conn.commit()
        _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
        assert (task.id, "escalated") not in delivery.sweep_destination_waits(conn)
        assert not _state(conn, task.id)["destination_wait"].get("escalated_at")
        conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer='ok',author='Principal',resolved_at=? WHERE id='dec_outro'", (int(time.time()),))
        conn.commit()
        assert (task.id, "escalated") in delivery.sweep_destination_waits(conn)


def test_every_waiting_card_gets_checked_across_sweeps(board, monkeypatch):
    with kb.connect_closing() as conn:
        tasks = []
        for n in range(23, 28):
            task = _card(conn, n, spec=_spec(_http_probe()))
            _park(conn, task, monkeypatch)
            tasks.append(task)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        parked = delivery.sweep_destination_waits(conn) + delivery.sweep_destination_waits(conn)
        assert sorted(parked) == sorted((t.id, "wait") for t in tasks)
        for task in tasks:
            _shift(conn, task.id, next_check_at=0, checked_at=0)
        for _ in range(2):
            delivery.sweep_destination_waits(conn)
        assert all(_state(conn, t.id)["destination_wait"]["checked_at"] > 0 for t in tasks)


def test_destination_back_withdraws_the_escalation_question(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 28, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
        assert (task.id, "escalated") in delivery.sweep_destination_waits(conn)
        decision_id = _state(conn, task.id)["destination_wait"]["escalation_decision"]
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _shift(conn, task.id, next_check_at=0)
        delivery.sweep_destination_waits(conn)
        _shift(conn, task.id, next_check_at=0, up_at=int(time.time()) - delivery.DESTINATION_CONFIRM_SECONDS - 1)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]
        row = delivery.get_decision(conn, decision_id)
        assert row["status"] == "superseded" and json.loads(row["context"])["superseded_reason"] == "destination_back"
        assert kb.get_task(conn, task.id).status == "ready"


def test_legacy_wait_is_not_adopted_by_a_newer_foreign_block_nor_without_block_events(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 29, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        st = _state(conn, task.id)
        st["destination_wait"].pop("block_event_id")
        conn.execute("UPDATE nfos_workflows SET state_json=? WHERE task_id=?", (json.dumps(st), task.id))
        conn.commit()
        assert kb.unblock_task(conn, task.id)
        assert kb.block_task(conn, task.id, reason="Janela de manutenção do provedor em andamento", kind="transient")
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _shift(conn, task.id, next_check_at=0)
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked" and "destination_wait" not in _state(conn, task.id)

        other = _card(conn, 30, spec=_spec(_http_probe()))
        _park(conn, other, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        delivery.sweep_destination_waits(conn)
        assert kb.get_task(conn, other.id).status == "blocked"
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected')", (other.id,))
        conn.commit()
        _shift(conn, other.id, next_check_at=0, block_event_id=None)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        delivery.sweep_destination_waits(conn)
        assert kb.get_task(conn, other.id).status == "blocked" and "destination_wait" not in _state(conn, other.id)


# --- revisão 4 (rejulgamento 06d586e4) --------------------------------------------------------------------------------------------------

def _escalate(conn, task, monkeypatch):
    """Card estacionado com o destino fora do ar há mais de 1 h e a pergunta de escalada aberta."""
    _park(conn, task, monkeypatch)
    monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
    assert (task.id, "wait") in delivery.sweep_destination_waits(conn)
    _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
    assert (task.id, "escalated") in delivery.sweep_destination_waits(conn)
    return _state(conn, task.id)["destination_wait"]["escalation_decision"]


def _confirmed_up(conn, task_id):
    _shift(conn, task_id, next_check_at=0, up_at=int(time.time()) - delivery.DESTINATION_CONFIRM_SECONDS - 1)


def test_leading_markers_do_not_hide_who_is_asked():
    assert delivery._human_is_maintenance("- PERGUNTA para Maikol: credencial da sonda?")
    assert delivery._human_is_maintenance("> **PERGUNTA para Maikol**: a sonda precisa de cookie?")
    assert delivery._human_is_maintenance("1. PERGUNTA para Maikol: credencial da sonda?")
    assert not delivery._human_is_maintenance("1. PERGUNTA para Jhonatan: pode religar a VPS do TEST?")


def test_destination_back_keeps_the_wait_when_the_card_does_not_leave_the_block(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 31, spec=_spec(_http_probe()))
        decision_id = _escalate(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        real_unblock = kb.unblock_task

        def locked(conn, task_id):
            raise RuntimeError("database is locked")

        for broken in (lambda conn, task_id: False, locked):
            monkeypatch.setattr(kb, "unblock_task", broken)
            _confirmed_up(conn, task.id)
            assert delivery.sweep_destination_waits(conn) == []
            assert kb.get_task(conn, task.id).status == "blocked"
            assert _state(conn, task.id)["destination_wait"]["escalation_decision"] == decision_id
            assert delivery.get_decision(conn, decision_id)["status"] == "pending"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_destination_back'", (task.id,)).fetchone()[0] == 0
        monkeypatch.setattr(kb, "unblock_task", real_unblock)
        _confirmed_up(conn, task.id)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]
        assert kb.get_task(conn, task.id).status == "ready" and "destination_wait" not in _state(conn, task.id)
        assert delivery.get_decision(conn, decision_id)["status"] == "superseded"


def test_a_crash_between_blocking_and_recording_the_block_is_adopted_by_target(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 32, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        real_block = kb.block_task

        def block_then_crash(conn, task_id, **kw):
            assert real_block(conn, task_id, **kw)
            raise RuntimeError("gateway stopped")

        monkeypatch.setattr(kb, "block_task", block_then_crash)
        assert delivery.sweep_destination_waits(conn) == []
        monkeypatch.setattr(kb, "block_task", real_block)
        assert kb.get_task(conn, task.id).status == "blocked"
        wait = _state(conn, task.id)["destination_wait"]
        assert wait["target"] == TARGET and wait.get("block_event_id") is None
        _shift(conn, task.id, next_check_at=0)
        delivery.sweep_destination_waits(conn)
        assert _state(conn, task.id)["destination_wait"]["block_event_id"]
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _shift(conn, task.id, next_check_at=0)
        delivery.sweep_destination_waits(conn)
        _confirmed_up(conn, task.id)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]


def test_legacy_adoption_needs_the_block_of_the_same_destination(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 33, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        assert kb.unblock_task(conn, task.id)
        assert kb.block_task(conn, task.id, reason=f"Destino {TARGET}.br sem resposta desde 14/09 13:54Z.", kind="transient")
        _shift(conn, task.id, block_event_id=None, next_check_at=0)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked" and "destination_wait" not in _state(conn, task.id)


def test_escalation_decision_and_its_link_are_written_together(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 34, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
        real_store = delivery._store_destination_wait

        def store_fails(conn, task_id, wait):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(delivery, "_store_destination_wait", store_fails)
        assert delivery.sweep_destination_waits(conn) == []
        monkeypatch.setattr(delivery, "_store_destination_wait", real_store)
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 0
        assert (task.id, "escalated") in delivery.sweep_destination_waits(conn)
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 1


def test_open_escalation_without_link_is_adopted_and_a_burned_one_is_retried(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 35, spec=_spec(_http_probe()))
        _park(conn, task, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert delivery.sweep_destination_waits(conn) == [(task.id, "wait")]
        conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES('dec_orfa',?,?,?,?,?,?,?)",
                     (task.id, task.current_run_id, "impediment", "Destino sem resposta?", json.dumps({"destination_wait": True, "target": TARGET}),
                      1, int(time.time())))
        conn.commit()
        _shift(conn, task.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60)
        assert (task.id, "escalated") not in delivery.sweep_destination_waits(conn)
        assert _state(conn, task.id)["destination_wait"]["escalation_decision"] == "dec_orfa"
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 1

        other = _card(conn, 36, spec=_spec(_http_probe()))
        _park(conn, other, monkeypatch)
        assert delivery.sweep_destination_waits(conn) == [(other.id, "wait")]
        _shift(conn, other.id, since=int(time.time()) - delivery.DESTINATION_ESCALATE_SECONDS - 60, escalated_at=int(time.time()) - 600)
        assert (other.id, "escalated") in delivery.sweep_destination_waits(conn)
        assert _state(conn, other.id)["destination_wait"]["escalation_decision"]


def test_dropped_wait_withdraws_a_pending_escalation_but_keeps_a_question_already_sent(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 37, spec=_spec(_http_probe()))
        decision_id = _escalate(conn, task, monkeypatch)
        assert kb.unblock_task(conn, task.id)
        assert kb.block_task(conn, task.id, reason="Janela de manutenção do provedor em andamento", kind="transient")
        delivery.sweep_destination_waits(conn)
        row = delivery.get_decision(conn, decision_id)
        assert row["status"] == "superseded" and json.loads(row["context"])["superseded_reason"] == "destination_wait_dropped"
        assert "destination_wait" not in _state(conn, task.id) and kb.get_task(conn, task.id).status == "blocked"

        other = _card(conn, 38, spec=_spec(_http_probe()))
        asked = _escalate(conn, other, monkeypatch)
        delivery.resolve_decision(conn, asked, action="human", author="Principal",
                                  answer="PERGUNTA para Jhonatan: o servidor do TEST foi desligado? Qual é o endereço atual?")
        assert kb.unblock_task(conn, other.id)
        assert kb.block_task(conn, other.id, reason="Janela de manutenção do provedor em andamento", kind="transient")
        delivery.sweep_destination_waits(conn)
        assert delivery.get_decision(conn, asked)["status"] == "human"
        assert "destination_wait" not in _state(conn, other.id)


def test_principal_changes_on_the_escalation_releases_the_card_and_continue_keeps_waiting(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 39, spec=_spec(_http_probe()))
        decision_id = _escalate(conn, task, monkeypatch)
        delivery.resolve_decision(conn, decision_id, action="continue", answer="Segue a espera do destino.", author="Principal")
        _shift(conn, task.id, next_check_at=0)
        assert delivery.sweep_destination_waits(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked" and _state(conn, task.id)["destination_wait"]["escalation_decision"] == decision_id
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 1

        other = _card(conn, 40, spec=_spec(_http_probe()))
        asked = _escalate(conn, other, monkeypatch)
        delivery.resolve_decision(conn, asked, action="changes", author="Principal",
                                  answer="O TEST mudou para https://novo.example.com; corrija spec, deploy e medição.")
        assert delivery.sweep_destination_waits(conn) == [(other.id, "released")]
        assert kb.get_task(conn, other.id).status == "ready" and "destination_wait" not in _state(conn, other.id)
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_destination_wait_released'", (other.id,)).fetchone()[0] == 1


def test_question_sent_to_the_operator_keeps_the_wait_and_destination_back_withdraws_it(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 41, spec=_spec(_http_probe()))
        decision_id = _escalate(conn, task, monkeypatch)
        delivery.resolve_decision(conn, decision_id, action="human", author="Principal",
                                  answer="PERGUNTA para Jhonatan: o servidor do TEST foi desligado?")
        delivery.reconcile_human_answers(conn)
        blocked = kb.get_task(conn, task.id)
        assert blocked.status == "blocked" and blocked.block_kind == "transient"
        assert _state(conn, task.id)["destination_wait"]["escalation_decision"] == decision_id
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(True))
        _confirmed_up(conn, task.id)
        assert delivery.sweep_destination_waits(conn) == [(task.id, "back")]
        row = delivery.get_decision(conn, decision_id)
        assert row["status"] == "superseded" and json.loads(row["context"])["superseded_reason"] == "destination_back"
        assert kb.get_task(conn, task.id).status == "ready"


def test_card_back_in_the_queue_ends_its_wait_and_the_pending_escalation(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _card(conn, 42, spec=_spec(_http_probe()))
        decision_id = _escalate(conn, task, monkeypatch)
        assert kb.unblock_task(conn, task.id)
        monkeypatch.setattr(delivery, "_destination_reachable", lambda target, timeout=5: pytest.fail("no check for a card that left the wait"))
        delivery.sweep_destination_waits(conn)
        assert "destination_wait" not in _state(conn, task.id)
        row = delivery.get_decision(conn, decision_id)
        assert row["status"] == "superseded" and json.loads(row["context"])["superseded_reason"] == "destination_wait_dropped"


def test_textual_next_check_still_orders_the_sweep(board, monkeypatch):
    with kb.connect_closing() as conn:
        first = _card(conn, 43, spec=_spec(_http_probe()))
        _park(conn, first, monkeypatch)
        second = _card(conn, 44, spec=_spec(_http_probe()))
        _park(conn, second, monkeypatch)
        monkeypatch.setattr(delivery, "_destination_reachable", _reachable(False))
        assert len(delivery.sweep_destination_waits(conn)) == 2
        _shift(conn, first.id, next_check_at=int(time.time()) - 5, checked_at=1)
        _shift(conn, second.id, next_check_at="0", checked_at=1)
        monkeypatch.setattr(delivery, "DESTINATION_MAX_CHECKS_PER_SWEEP", 1)
        delivery.sweep_destination_waits(conn)
        assert _state(conn, second.id)["destination_wait"]["checked_at"] > 1
        assert _state(conn, first.id)["destination_wait"]["checked_at"] == 1
