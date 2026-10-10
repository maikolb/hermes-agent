"""LAB_TRANSPORT_WAIT_20261009: laboratório fora do ar espera sem o Principal.

Em 09/10/2026 o canal do concursa-lab parou ("exec request failed on channel 0") e o Principal pausou seis cards do Concursa
para reparo. A pausa pedia a ele um reparo que ele não alcança: cada resposta voltava a pendente e o lembrete o acordava a cada
15 min por card. Agora o worker registra `lab-wait --transporte`, a pausa com resume_when='lab_available' vira a mesma espera,
o runtime confere `concursa-lab status` e o card volta sozinho quando o laboratório responde."""
import json
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_runtime as runtime
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_lab_wait import _card, _decision, _kinds, _shift, _wake, board  # noqa: F401
from tests.hermes_cli.test_nfos_maintenance_pause import running  # noqa: F401
from tests.hermes_cli.test_nfos_principal_acceptance import assessment, task_context  # noqa: F401


@pytest.fixture
def lab(monkeypatch):
    """Canal do laboratório: state[0] é o que `concursa-lab status` responde (True no ar, False saída 255, None sem resposta)."""
    state, probes = [False], []

    def probe(timeout=delivery.LAB_WAIT_READ_TIMEOUT_SECONDS):
        probes.append(timeout)
        return state[0]

    monkeypatch.setattr(delivery, "_lab_transport_up", probe)
    return state, probes


def _requested(conn, task_id):
    return conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'",
                        (task_id,)).fetchone()[0]


def test_lab_down_holds_the_turn_without_the_principal_and_releases_when_it_answers(board, lab):
    state, probes = lab
    with kb.connect_closing() as conn:
        task = _card(conn, 50)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, None, reason="roteiro do p6 sem canal", transport=True)
        assert out["waiting"] is True and out["transport"] is True and "Encerre o turno" in out["next"]
        row, context = _decision(conn, out["decision_id"])
        assert row["status"] == "pending" and context["lab_wait"]["kind"] == "transport" and context["lab_wait"]["hold"] is True
        assert "Laboratório fora do ar" in row["question"] and "roteiro do p6" in row["question"]
        assert kb._nfos_pending_decision(conn, task.id, task.current_run_id) == out["decision_id"]
        assert out["decision_id"] not in [d["id"] for d in delivery.pending_decisions(conn)]
        conn.execute("UPDATE nfos_decisions SET created_at=? WHERE id=?", (int(time.time()) - 3600, out["decision_id"]))
        conn.commit()
        assert delivery.nudge_open_decisions(conn, task.id) == [] and _requested(conn, task.id) == 0
        again = delivery.lab_wait(conn, task.id, task.current_run_id, None, transport=True)
        assert again["decision_id"] == out["decision_id"], "a mesma espera na mesma execução"
    from agent import kanban_stop
    assert kanban_stop._principal_continuation(_wake(board, task.id, "wake-transporte-1")) is None
    with kb.connect_closing() as conn:
        assert delivery.sweep_lab_waits(conn) == [], "não confere antes de 5 min"
        _shift(conn, out["decision_id"], next_check_at=0)
        assert delivery.sweep_lab_waits(conn) == [(out["decision_id"], "wait")]
        assert _decision(conn, out["decision_id"])[1]["lab_wait"]["next_check_at"] > time.time() + 200
        state[0] = True
        _shift(conn, out["decision_id"], next_check_at=0)
        assert delivery.sweep_lab_waits(conn) == [(out["decision_id"], "released")]
        row, context = _decision(conn, out["decision_id"])
        assert row["status"] == "resolved" and row["action"] == "continue" and "voltou a responder" in row["answer"]
        assert context["lab_wait"]["hold"] is False and context["lab_wait"]["status"] == "disponivel"
        assert not kb._nfos_decision_open(conn, task.id) and "nfos_lab_wait_released" in _kinds(conn, task.id)
        assert _requested(conn, task.id) == 0
        assert set(probes) == {delivery.LAB_WAIT_READ_TIMEOUT_SECONDS}, "consulta curta no tick do despacho"


def test_lab_answering_at_registration_does_not_wait(board, lab):
    state, _ = lab
    state[0] = True
    with kb.connect_closing() as conn:
        task = _card(conn, 51)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, None, transport=True)
        assert out["waiting"] is False and "siga sem esperar" in out["next"]
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 0


def test_one_probe_per_sweep_serves_every_wait_and_an_answer_releases_them_all(board, lab):
    state, probes = lab
    with kb.connect_closing() as conn:
        waits = []
        for n in range(3):
            task = _card(conn, 60 + n)
            waits.append(delivery.lab_wait(conn, task.id, task.current_run_id, None, transport=True)["decision_id"])
            _shift(conn, waits[-1], next_check_at=n)
        probes.clear()
        assert sorted(delivery.sweep_lab_waits(conn)) == sorted((w, "wait") for w in waits) and len(probes) == 1
        state[0] = None
        _shift(conn, waits[0], next_check_at=0)
        assert delivery.sweep_lab_waits(conn) == [(waits[0], "wait")], "sem resposta é laboratório fora do ar"
        assert _decision(conn, waits[0])[1]["lab_wait"]["status"] == "sem_resposta"
        state[0] = True
        _shift(conn, waits[1], next_check_at=0)
        probes.clear()
        assert sorted(delivery.sweep_lab_waits(conn)) == sorted((w, "released") for w in waits), "o laboratório respondeu: todas saem"
        assert len(probes) == 1


def test_lab_down_for_more_than_twelve_hours_still_waits_without_the_principal(board, lab):
    with kb.connect_closing() as conn:
        task = _card(conn, 52)
        decision_id = delivery.lab_wait(conn, task.id, task.current_run_id, None, transport=True)["decision_id"]
        _shift(conn, decision_id, since=int(time.time()) - delivery.LAB_WAIT_ESCALATE_SECONDS - 60, next_check_at=0)
        assert delivery.sweep_lab_waits(conn) == [(decision_id, "wait")]
        assert _decision(conn, decision_id)[1]["lab_wait"]["hold"] is True and _requested(conn, task.id) == 0


def test_cli_registers_the_transport_wait(board, lab, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        task = _card(conn, 53)
    monkeypatch.setattr(sys, "argv", ["nfos", "lab-wait", "--transporte", "--task", task.id, "--run", str(task.current_run_id)])
    delivery.main()
    out = json.loads(capsys.readouterr().out)
    assert out["waiting"] is True and out["transport"] is True
    with kb.connect_closing() as conn:
        assert _decision(conn, out["decision_id"])[1]["lab_wait"]["kind"] == "transport"


def _pause_for_lab(conn, task, artifact, process, args, monkeypatch, *, exited=True):
    """Pausa do Principal porque o laboratório caiu, com as decisões anteriores do card já resolvidas."""
    repair.pause_for_repair(conn, task.id, **dict(args, reason="Canal SSH do laboratório indisponível"),
                            resume_when="lab_available", apply=True)
    if exited:
        real_now = time.time()
        with monkeypatch.context() as clock:
            clock.setattr(runtime.time, "time", lambda: real_now + 30)
            runtime.reconcile_terminal_workers(conn)
        process.wait(timeout=5)
    for (decision_id,) in conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')",
                                       (task.id,)).fetchall():
        delivery.resolve_decision(conn, decision_id, action="continue", answer="Reviewed", author="Principal",
                                  assessment=assessment(artifact))


def _pause_wait(conn, task):
    return conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='pending' "
                        "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?", (task.id, task.current_run_id)).fetchall()


def test_lab_pause_waits_natively_and_resumes_the_card_when_the_lab_answers(running, monkeypatch, lab):
    from agent import kanban_stop
    from gateway.wake import current_notify_receipt
    state, _ = lab
    conn, task, artifact, process, args = running
    _pause_for_lab(conn, task, artifact, process, args, monkeypatch)
    pause = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0])
    assert pause["maintenance_pause"]["resume_when"] == "lab_available"
    requested = _requested(conn, task.id)
    assert delivery.sweep_awaiting_principal(conn) == []
    rows = _pause_wait(conn, task)
    assert len(rows) == 1, "uma obrigação da pausa, que é a espera"
    wait = dict(rows[0])
    context = json.loads(wait["context"])
    assert context["lab_wait"]["kind"] == "transport" and context["lab_wait"]["hold"] is True
    assert context["lab_wait"]["pause_run_id"] == task.current_run_id
    assert context["maintenance_recovery"]["resume_route"] == "lab_available"
    assert "Execute o reparo" not in wait["question"] and "Laboratório fora do ar" in wait["question"]
    assert kb.get_task(conn, task.id).status == "blocked" and repair.maintenance_pause_pending(conn, task.id)
    # Nem pedido, nem lembrete, nem fila, nem turno do Principal, mesmo depois do intervalo dos lembretes.
    now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(delivery.time, "time", lambda: now + delivery.DECISION_REMINDER_GAP + 1)
        runtime.reconcile_runtime(conn)
        runtime.reconcile_runtime(conn)
    assert _requested(conn, task.id) == requested
    assert wait["id"] not in [d["id"] for d in delivery.pending_decisions(conn)]
    token = current_notify_receipt.set({"db_path": conn.execute("PRAGMA database_list").fetchone()[2],
                                        "principal_task_id": task.id, "delivery_id": "wake-pausa-lab"})
    try:
        assert kanban_stop._principal_continuation(None) is None
    finally:
        current_notify_receipt.reset(token)
    # Laboratório ainda fora: a pausa segue; quando ele responde, a pausa termina e o card volta à fila.
    _shift(conn, wait["id"], next_check_at=0)
    assert delivery.sweep_lab_waits(conn) == [(wait["id"], "wait")] and repair.maintenance_pause_pending(conn, task.id)
    state[0] = True
    _shift(conn, wait["id"], next_check_at=0)
    assert delivery.sweep_lab_waits(conn) == [(wait["id"], "released")]
    assert not repair.maintenance_pause_pending(conn, task.id)
    meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0])
    assert meta["maintenance_pause"]["repair_kind"] == "lab_available"
    assert meta["maintenance_pause"]["repaired_by"] == "NFOS automation" and meta["maintenance_pause"]["repair_evidence"]
    row = delivery.get_decision(conn, wait["id"])
    assert row["status"] == "resolved" and row["action"] == "continue"
    assert delivery.sweep_awaiting_principal(conn) == [task.id]
    assert kb.claim_task(conn, task.id).id == task.id
    assert _requested(conn, task.id) == requested, "o Principal não foi acordado em nenhum passo"


def test_explanatory_answer_keeps_the_lab_pause_waiting_without_a_loop(running, monkeypatch, lab):
    conn, task, artifact, process, args = running
    _pause_for_lab(conn, task, artifact, process, args, monkeypatch)
    delivery.sweep_awaiting_principal(conn)
    wait_id = _pause_wait(conn, task)[0]["id"]
    delivery.resolve_decision(conn, wait_id, action="continue", answer="O laboratório está fora do ar", author="Principal")
    row, context = _decision(conn, wait_id)
    assert row["status"] == "pending" and context["lab_wait"]["hold"] is True, "a obrigação da pausa continua e segue segura"
    requested = _requested(conn, task.id)
    now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(delivery.time, "time", lambda: now + delivery.DECISION_REMINDER_GAP + 1)
        runtime.reconcile_runtime(conn)
    assert _requested(conn, task.id) == requested


def test_lab_pause_waits_for_the_previous_worker_to_exit(running, monkeypatch, lab):
    state, _ = lab
    conn, task, artifact, process, args = running
    _pause_for_lab(conn, task, artifact, process, args, monkeypatch, exited=False)
    delivery.sweep_awaiting_principal(conn)
    wait_id = _pause_wait(conn, task)[0]["id"]
    state[0] = True
    _shift(conn, wait_id, next_check_at=0)
    assert delivery.sweep_lab_waits(conn) == [(wait_id, "pause_not_ready")]
    row, context = _decision(conn, wait_id)
    assert row["status"] == "pending" and context["lab_wait"]["hold"] is True
    assert repair.maintenance_pause_pending(conn, task.id), "nada muda enquanto o executor anterior não sai"
    real_now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(runtime.time, "time", lambda: real_now + 30)
        runtime.reconcile_terminal_workers(conn)
    process.wait(timeout=5)
    _shift(conn, wait_id, next_check_at=0)
    assert delivery.sweep_lab_waits(conn) == [(wait_id, "released")]
    assert not repair.maintenance_pause_pending(conn, task.id)


def test_existing_repair_obligation_becomes_the_wait_when_the_pause_waits_for_the_lab(running, monkeypatch, lab):
    """As pausas de 09/10 já tinham a obrigação "Execute o reparo"; marcar resume_when converte sem perder o histórico."""
    from tests.hermes_cli.test_nfos_maintenance_pause import _paused_and_exited
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    delivery.sweep_awaiting_principal(conn)
    old = _pause_wait(conn, task)[0]["id"]
    assert "Execute o reparo" in delivery.get_decision(conn, old)["question"]
    run = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
    metadata = json.loads(run[0])
    metadata["maintenance_pause"]["resume_when"] = "lab_available"
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), task.current_run_id))
    delivery.sweep_awaiting_principal(conn)
    rows = _pause_wait(conn, task)
    assert len(rows) == 1 and rows[0]["id"] != old
    assert json.loads(rows[0]["context"])["lab_wait"]["hold"] is True
    old_row, old_context = _decision(conn, old)
    assert old_row["status"] == "superseded" and old_context["superseded_by"] == rows[0]["id"]
    assert old_context["superseded_reason"] == "lab_transport_wait"
    delivery.sweep_awaiting_principal(conn)
    assert len(_pause_wait(conn, task)) == 1, "a conversão acontece uma vez"


@pytest.mark.parametrize("returncode,stdout,expected", [
    (0, '{"scope": "Concursa-Isolado", "services": {"web": {"http": 200}, "admin": {"http": 200}}}\n', True),
    (1, '{"scope": "Concursa-Isolado", "services": {"web": {"error_type": "URLError"}, "admin": {"http": 200}}}\n', True),
    # LAB_APPS_DOWN_20261010: o programa rodou, mas nenhum app do laboratório respondeu por HTTP.
    (1, '{"scope": "Concursa-Isolado", "services": {"web": {"error_type": "TimeoutError"}, "admin": {"error_type": "TimeoutError"}}}\n', False),
    (1, '{"scope": "Concursa-Isolado", "services": {"web": {"error_type": "HTTPError"}, "admin": {"error_type": "URLError"}}}\n', False),
    (1, '{"scope": "Concursa-Isolado", "services": {}}\n', False),
    (1, '{"scope": "Concursa-Isolado"}\n', False),
    (75, "Execucao ocupada. Pedido e checkpoint preservados; nenhuma operacao executada.\n", True),
    (255, "", False),
    (1, "", False),
    (126, "bash: fork: retry: Resource temporarily unavailable\n", False),
])
def test_lab_counts_as_up_only_when_its_status_program_ran(monkeypatch, returncode, stdout, expected):
    """09/10/2026: o sshd aceitava a conexão e o contêiner de controle, sem PID livre, não criava o processo. No ar é o
    programa de status ter rodado lá e devolvido o JSON dele; 255 ou saída sem o JSON é fora do ar. Saída 75 é o
    laboratório respondendo "execução ocupada": o status sem tarefa disputa a trava e o canal atende (às 23:06 UTC o
    t_2515758e ficou 45 min na espera por isso). Em 10/10/2026 o canal respondia e os apps não (rede entre contêineres
    caída por 36 min): com os dois apps sem resposta HTTP também é fora do ar."""
    import subprocess
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("timeout")))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("NFOS_CONCURSA_LAB", "/opt/fake/concursa-lab")
    assert delivery._lab_transport_up(timeout=7) is expected
    assert calls == [(["/opt/fake/concursa-lab", "status"], 7)]


def test_lab_without_an_answer_is_unknown(monkeypatch):
    import subprocess

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", run)
    assert delivery._lab_transport_up(timeout=1) is None


def test_pause_refuses_an_unknown_resume_condition(running):
    conn, task, _, _, args = running
    with pytest.raises(delivery.WorkflowError, match="resume_when"):
        repair.pause_for_repair(conn, task.id, **args, resume_when="whenever", apply=True)
    assert kb.get_task(conn, task.id).status == "running"
