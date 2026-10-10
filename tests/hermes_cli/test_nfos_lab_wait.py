"""LAB_WAIT_20261008: pedido do laboratório na fila do broker espera sem o Principal. A espera é uma decisão que cede o turno, não acorda
o Principal nem gera lembretes; o runtime confere o recibo com orçamento e responde continue quando o pedido sai da fila; depois de 12 h, ou
com o recibo ilegível, a decisão vai ao Principal uma vez."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

RECEIPT = "t-c553fe15-p12"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    monkeypatch.setattr(runtime, "project_config",
                        lambda board, config=None: {"enabled": True, "board": board, "project_id": "concursa-ai"})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


@pytest.fixture
def broker(monkeypatch):
    """Recibos do broker por id; a contagem de leituras mostra o orçamento da varredura."""
    receipts, reads = {}, []

    def read(receipt, timeout=90):
        reads.append(receipt)
        timeouts.append(timeout)
        value = receipts.get(receipt, {"status": "queued", "reason": "capacity"})
        return dict(value) if value is not None else None

    timeouts = []
    monkeypatch.setattr(delivery, "_lab_receipt", read)
    read.timeouts = timeouts
    return receipts, reads


def _card(conn, n):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(700 + n)},
        text=f"Erro ao extrair disciplinas do cargo {n}.",
        project={"board": "concursa-ai", "profile": "default", "delivery_type": "code"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=16)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    return kb.get_task(conn, task.id)


def _decision(conn, decision_id):
    row = delivery.get_decision(conn, decision_id)
    return row, json.loads(row["context"])


def _shift(conn, decision_id, **fields):
    row, context = _decision(conn, decision_id)
    context["lab_wait"].update(fields)
    conn.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(context), decision_id))
    conn.commit()


def _kinds(conn, task_id):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,))]


def test_queued_receipt_holds_the_turn_without_waking_the_principal(board, broker):
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT, reason="TDD do p6 aguardando vaga")
        assert out["waiting"] is True and "Encerre o turno" in out["next"]
        row, context = _decision(conn, out["decision_id"])
        assert row["status"] == "pending" and row["kind"] == "impediment"
        assert context["lab_wait"]["receipt"] == RECEIPT and context["lab_wait"]["hold"] is True
        assert "TDD do p6" in row["question"]
        # A espera cede o turno e segura o card como qualquer decisão aberta.
        assert kb._nfos_pending_decision(conn, task.id, task.current_run_id) == out["decision_id"]
        assert kb._nfos_decision_open(conn, task.id)
        # Sem pedido ao Principal, fora da lista dele e sem lembrete mesmo depois de 1 h.
        assert "nfos_lab_wait" in _kinds(conn, task.id) and "nfos_principal_requested" not in _kinds(conn, task.id)
        assert out["decision_id"] not in [d["id"] for d in delivery.pending_decisions(conn)]
        conn.execute("UPDATE nfos_decisions SET created_at=? WHERE id=?", (int(time.time()) - 3600, out["decision_id"]))
        conn.commit()
        assert delivery.nudge_open_decisions(conn, task.id) == []
        assert "nfos_principal_requested" not in _kinds(conn, task.id)
        assert kb.get_task(conn, task.id).block_kind != "awaiting_principal"
        # O mesmo recibo na mesma execução reaproveita a espera.
        again = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        assert again["decision_id"] == out["decision_id"]
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 1


def test_receipt_out_of_queue_answers_continue_and_releases_the_card(board, broker):
    receipts, reads = broker
    with kb.connect_closing() as conn:
        task = _card(conn, 2)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        assert delivery.sweep_lab_waits(conn) == [], "não reconfere antes de 5 min"
        _shift(conn, out["decision_id"], next_check_at=0)
        assert delivery.sweep_lab_waits(conn) == [(out["decision_id"], "wait")]
        assert _decision(conn, out["decision_id"])[1]["lab_wait"]["next_check_at"] > time.time() + 200

        _shift(conn, out["decision_id"], next_check_at=0)
        receipts[RECEIPT] = {"status": "ok", "result": {"task": "grupo-cs0013"}}
        assert delivery.sweep_lab_waits(conn) == [(out["decision_id"], "released")]
        row, context = _decision(conn, out["decision_id"])
        assert row["status"] == "resolved" and row["action"] == "continue"
        assert RECEIPT in row["answer"] and "não reenvie" in row["answer"] and "runtime do NFOS" in row["answer"]
        assert context["lab_wait"]["hold"] is False and context["lab_wait"]["status"] == "ok"
        assert not kb._nfos_decision_open(conn, task.id)
        assert "nfos_lab_wait_released" in _kinds(conn, task.id)
        assert delivery.sweep_lab_waits(conn) == [], "espera resolvida não volta à varredura"


def test_sweep_respects_its_budget_and_serves_the_oldest_first(board, broker):
    receipts, reads = broker
    with kb.connect_closing() as conn:
        waits = []
        for n in range(4):
            task = _card(conn, 10 + n)
            waits.append(delivery.lab_wait(conn, task.id, task.current_run_id, f"t-lote{n}-p6")["decision_id"])
        for i, decision_id in enumerate(waits):
            _shift(conn, decision_id, next_check_at=i)
        reads.clear()
        delivery._lab_receipt.timeouts.clear()
        assert len(delivery.sweep_lab_waits(conn)) == delivery.LAB_WAIT_MAX_CHECKS_PER_SWEEP
        assert reads == ["t-lote0-p6", "t-lote1-p6", "t-lote2-p6"]
        assert set(delivery._lab_receipt.timeouts) == {delivery.LAB_WAIT_READ_TIMEOUT_SECONDS}, "leitura curta no tick do despacho"
        reads.clear()
        assert delivery.sweep_lab_waits(conn) == [(waits[3], "wait")] and reads == ["t-lote3-p6"]


def test_sweep_stops_reading_when_its_time_budget_is_spent(board, broker, monkeypatch):
    receipts, reads = broker
    with kb.connect_closing() as conn:
        task = _card(conn, 20)
        decision_id = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)["decision_id"]
        _shift(conn, decision_id, next_check_at=0)
        reads.clear()
        monkeypatch.setattr(delivery, "LAB_WAIT_SWEEP_SECONDS", 0)
        assert delivery.sweep_lab_waits(conn) == [] and reads == []


def test_receipt_already_out_of_queue_does_not_wait(board, broker):
    receipts, _ = broker
    receipts[RECEIPT] = {"status": "error", "error": "commit ausente no espelho"}
    with kb.connect_closing() as conn:
        task = _card(conn, 3)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        assert out["waiting"] is False and out["status"] == "error" and "sem reenviar" in out["next"]
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 0


def test_request_the_laboratory_does_not_know_is_resent_instead_of_awaited(board, broker):
    """LAB_RECEIPT_UNKNOWN_20261010: sem recibo e sem pedido na caixa do broker, o pedido não chegou (CS-0019, 09/10/2026)."""
    receipts, _ = broker
    receipts[RECEIPT] = {"id": RECEIPT, "status": "unknown"}
    with kb.connect_closing() as conn:
        task = _card(conn, 31)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        assert out["waiting"] is False and out["status"] == "unknown"
        assert "Reenvie com o mesmo ID" in out["next"] and "sem reenviar" not in out["next"]
        assert conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=?", (task.id,)).fetchone()[0] == 0


def test_wait_on_a_request_that_turns_out_unknown_is_released_to_resend(board, broker):
    receipts, _ = broker
    with kb.connect_closing() as conn:
        task = _card(conn, 32)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        assert out["waiting"] is True
        _shift(conn, out["decision_id"], next_check_at=0)
        receipts[RECEIPT] = {"id": RECEIPT, "status": "unknown"}
        assert delivery.sweep_lab_waits(conn) == [(out["decision_id"], "released")]
        row, context = _decision(conn, out["decision_id"])
        assert row["status"] == "resolved" and row["action"] == "continue"
        assert RECEIPT in row["answer"] and "Reenvie com o mesmo ID" in row["answer"] and "não reenvie" not in row["answer"]
        assert context["lab_wait"]["hold"] is False and context["lab_wait"]["status"] == "unknown"
        assert "nfos_principal_requested" not in _kinds(conn, task.id), "o worker reenvia; o Principal não é acordado"


def test_bad_or_unreadable_receipt_and_foreign_run_refuse_to_wait(board, broker):
    receipts, _ = broker
    with kb.connect_closing() as conn:
        task = _card(conn, 4)
        with pytest.raises(delivery.WorkflowError, match="id do pedido"):
            delivery.lab_wait(conn, task.id, task.current_run_id, "../pedido")
        receipts[RECEIPT] = None
        with pytest.raises(delivery.WorkflowError, match="ilegível"):
            delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        receipts.pop(RECEIPT)
        with pytest.raises(delivery.OwnershipConflict):
            delivery.lab_wait(conn, task.id, (task.current_run_id or 0) + 1, RECEIPT)


def test_twelve_hours_in_queue_or_unreadable_receipt_go_to_the_principal_once(board, broker):
    receipts, _ = broker
    with kb.connect_closing() as conn:
        old = _card(conn, 5)
        late = delivery.lab_wait(conn, old.id, old.current_run_id, "t-antigo1-p6")["decision_id"]
        _shift(conn, late, since=int(time.time()) - delivery.LAB_WAIT_ESCALATE_SECONDS - 60)
        lost = _card(conn, 6)
        blind = delivery.lab_wait(conn, lost.id, lost.current_run_id, "t-sumido1-p1")["decision_id"]
        receipts["t-sumido1-p1"] = None
        assert (late, "escalated") in delivery.sweep_lab_waits(conn)
        for _ in range(delivery.LAB_WAIT_MAX_READ_ERRORS):
            _shift(conn, blind, next_check_at=0)
            result = delivery.sweep_lab_waits(conn)
        assert (blind, "escalated") in result
        for decision_id, task in ((late, old), (blind, lost)):
            row, context = _decision(conn, decision_id)
            assert row["status"] == "pending" and context["lab_wait"]["hold"] is False
            assert decision_id in [d["id"] for d in delivery.pending_decisions(conn)]
            requested = [r for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'",
                                                 (task.id,))]
            assert len(requested) == 1
        assert delivery.sweep_lab_waits(conn) == [], "a escalada acontece uma vez"


def _wake(board, task_id, delivery_id):
    return {"db_path": str(board / "kanban.db"), "principal_task_id": task_id, "delivery_id": delivery_id}


def _escalated_wait(conn, n, receipt):
    task = _card(conn, n)
    decision_id = delivery.lab_wait(conn, task.id, task.current_run_id, receipt)["decision_id"]
    _shift(conn, decision_id, since=int(time.time()) - delivery.LAB_WAIT_ESCALATE_SECONDS - 60)
    assert (decision_id, "escalated") in delivery.sweep_lab_waits(conn)
    return task, decision_id


def test_held_lab_wait_does_not_hold_the_principal_turn_until_it_escalates(board, broker):
    """PRINCIPAL_TURN_BOUND_20261009: em 09/10, das 18:26 às 19:09 UTC, o turno do Principal acordado pelo t_8ed13ed7 ficou esperando
    a espera do laboratório (dec_1d51bb77) com 43 cutucões e o quadro inteiro parou. A espera é do runtime, não do turno."""
    from agent import kanban_stop
    with kb.connect_closing() as conn:
        task = _card(conn, 40)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
    assert kanban_stop._principal_continuation(_wake(board, task.id, "wake-lab-1")) is None
    with kb.connect_closing() as conn:
        _shift(conn, out["decision_id"], since=int(time.time()) - delivery.LAB_WAIT_ESCALATE_SECONDS - 60)
        assert (out["decision_id"], "escalated") in delivery.sweep_lab_waits(conn)
    nudge = kanban_stop._principal_continuation(_wake(board, task.id, "wake-lab-2"))
    assert nudge and out["decision_id"] in nudge, "escalada, a decisão volta a ser do Principal e o turno continua nela"


def test_principal_continuation_is_bounded_per_wake(board, broker, monkeypatch):
    """PRINCIPAL_TURN_BOUND_20261009: o Principal é uma sessão só. Um despertar segura o turno por tempo limitado; depois a decisão
    continua pendente e volta para a fila justa, em vez de parar os outros cards e pagar uma chamada de contexto cheio por cutucão."""
    from agent import kanban_stop
    clock = [1000.0]
    monkeypatch.setattr(kanban_stop, "_now", lambda: clock[0])
    with kb.connect_closing() as conn:
        task, decision_id = _escalated_wait(conn, 41, "t-limite1-p6")
    wake = _wake(board, task.id, "wake-limite-1")
    assert decision_id in kanban_stop._principal_continuation(wake)
    clock[0] += kanban_stop._PRINCIPAL_CONTINUATION_SECONDS - 1
    assert kanban_stop._principal_continuation(wake), "dentro da janela o turno continua"
    clock[0] += 2
    assert kanban_stop._principal_continuation(wake) is None, "passada a janela, o turno pode encerrar"
    with kb.connect_closing() as conn:
        assert delivery.get_decision(conn, decision_id)["status"] == "pending"
        assert decision_id in [d["id"] for d in delivery.pending_decisions(conn)], "a decisão segue na fila do Principal"
    assert kanban_stop._principal_continuation(_wake(board, task.id, "wake-limite-2")), "o próximo despertar tem a própria janela"


def _overdue_waits(conn, count, start):
    """Esperas do laboratório vencidas em cards distintos, a mais atrasada primeiro."""
    waits = []
    for n in range(count):
        task = _card(conn, start + n)
        waits.append(delivery.lab_wait(conn, task.id, task.current_run_id, f"t-tick{start + n}-p6")["decision_id"])
    for i, decision_id in enumerate(waits):
        _shift(conn, decision_id, next_check_at=i)
    return waits


def test_one_reconciliation_reads_the_broker_within_one_sweep_budget(board, broker):
    """LAB_SWEEP_PER_TICK_20261010: o teto de consultas ao broker é do tick. A reconciliação aplica respostas humanas duas vezes
    e cada passada fazia a própria varredura do laboratório, com o próprio teto."""
    receipts, reads = broker
    with kb.connect_closing() as conn:
        _overdue_waits(conn, 12, 50)
        reads.clear()
        runtime.reconcile_runtime(conn)
        assert len(reads) == delivery.LAB_WAIT_MAX_CHECKS_PER_SWEEP


def test_a_tick_that_reconciles_twice_sweeps_the_laboratory_once(board, broker, monkeypatch):
    """O tick reconcilia de novo quando encerra uma execução; essa segunda reconciliação não consulta o broker outra vez."""
    receipts, reads = broker
    with kb.connect_closing() as conn:
        _overdue_waits(conn, 12, 70)
        monkeypatch.setattr(kb, "enforce_max_runtime", lambda conn, **kwargs: ["execucao-encerrada"])
        reads.clear()
        kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None, board="concursa-ai")
        assert len(reads) == delivery.LAB_WAIT_MAX_CHECKS_PER_SWEEP


@pytest.mark.usefixtures('owner_questions_allowed')  # NO_OWNER_QUESTIONS_UNIVERSAL_20261010: pergunta com action='human' puro
def test_applying_a_human_answer_does_not_sweep_the_laboratory(board, broker):
    """O comando resume grava a resposta de uma pessoa e a aplica; consultar o laboratório é do tick do despacho."""
    receipts, reads = broker
    with kb.connect_closing() as conn:
        _overdue_waits(conn, 2, 90)
        task = _card(conn, 95)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment", question="Qual regra vale?", context={})
        delivery.resolve_decision(conn, decision, action="human", answer="PERGUNTA para Maikol: A ou B?", author="Principal")
        reads.clear()
        delivery.resume_after_answer(conn, task.id, answer="Use A", source={"platform": "telegram", "message_id": "955"})
        assert reads == []
