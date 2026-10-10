"""WAIT_RULE_20261010: toda espera é um registro com dono, predicado, prazo final e teto de tentativas.

Maikol, 10/10/2026: "O NFOS tem que ser autonomo, eu não quero ser babá de IA". A auditoria daquele dia achou onze estados em
que o card parava sem saída que o motor alcançasse. O primeiro a entrar no registro único é a espera do pedido na fila do
laboratório: antes, passadas 12 h, ela virava uma decisão pendente do Principal sem conclusão, com lembrete a cada 15 min
enquanto o card ficasse bloqueado. Agora a espera vence, o Principal recebe a decisão uma vez com lembretes contados e, sem
decisão dele, o motor responde e o card segue sem o pedido.
"""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from tests.hermes_cli.test_nfos_lab_wait import RECEIPT, _card, _decision, _kinds, _retain, board, broker  # noqa: F401

HOUR = 3600
TICK = 300


class _Waits:
    """O registro único, carregado no uso: na árvore sem a regra o teste falha no que afirma, não na coleta."""

    def __getattr__(self, name):
        from hermes_cli import nfos_waits
        return getattr(nfos_waits, name)


waits = _Waits()


@pytest.fixture
def clock(monkeypatch):
    """Relógio do motor: `advance(segundos)` leva o tick seguinte para depois."""
    now = [float(int(time.time()))]
    monkeypatch.setattr(time, "time", lambda: now[0])

    def advance(seconds):
        now[0] += seconds
        return int(now[0])

    return advance


def _rows(conn, task_id):
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM nfos_waits WHERE task_id=? ORDER BY created_at,rowid", (task_id,))]
    except sqlite3.OperationalError:
        return []


def _requests(conn, task_id):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' ORDER BY id", (task_id,))]


def _yield_turn(conn, task):
    """O worker registrou a espera e encerrou o turno: a execução fecha e o card volta à fila, seguro pela decisão aberta."""
    conn.execute("UPDATE task_runs SET status='released', outcome='released', ended_at=? WHERE id=?",
                 (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()


def _tick(conn, task_id):
    """O que o despacho faz a cada passada com um card que tem decisão aberta."""
    swept = delivery.sweep_lab_waits(conn)
    delivery.nudge_open_decisions(conn, task_id)
    return swept


def _queued_wait(conn, n, receipt=RECEIPT):
    task = _card(conn, n)
    out = delivery.lab_wait(conn, task.id, task.current_run_id, receipt, reason="TDD do p6 aguardando vaga")
    _yield_turn(conn, task)
    return task, out["decision_id"]


def _silent_principal(conn, clock, task_id, seconds):
    """O tempo passa, o despacho roda a cada 5 min e ninguém responde."""
    for _ in range(seconds // TICK):
        clock(TICK)
        _tick(conn, task_id)


def test_a_queue_wait_is_one_record_with_owner_predicate_deadline_and_attempt_cap(board, broker, clock):
    with kb.connect_closing() as conn:
        task = _card(conn, 201)
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        rows = _rows(conn, task.id)
        assert len(rows) == 1, "a espera do worker é uma linha do registro único"
        wait = rows[0]
        assert (wait["task_id"], wait["run_id"], wait["episode"]) == (task.id, task.current_run_id, 0)
        assert wait["reason"] == "lab_queue" and wait["status"] == "open" and wait["decision_id"] == out["decision_id"]
        assert json.loads(wait["executor"])["kind"] == "lab" and json.loads(wait["executor"])["name"]
        assert json.loads(wait["predicate"]) == {"kind": "lab_receipt", "receipt": RECEIPT}
        assert wait["next_check_at"] == wait["created_at"] + delivery.LAB_WAIT_RECHECK_SECONDS
        assert wait["deadline_at"] == wait["created_at"] + delivery.LAB_WAIT_ESCALATE_SECONDS
        assert wait["attempts"] == 0 and wait["max_attempts"] > 0
        # O relógio saiu do JSON da decisão: lá fica o que o worker declarou.
        saved = _decision(conn, out["decision_id"])[1]["lab_wait"]
        assert saved["receipt"] == RECEIPT and saved["hold"] is True and "next_check_at" not in saved
        shown = waits.card_waits(conn, task.id)
        assert [w["id"] for w in shown] == [wait["id"]] and shown[0]["predicate"]["receipt"] == RECEIPT
        assert "nfos_wait_declared" in _kinds(conn, task.id)


def test_the_registry_refuses_a_wait_it_could_not_finish(board, broker):
    lab = {"kind": "lab", "name": "broker do concursa-lab"}
    receipt = {"kind": "lab_receipt", "receipt": RECEIPT}
    with kb.connect_closing() as conn:
        task = _card(conn, 202)
        refused = [
            dict(reason="esperando alguma coisa", executor=lab, predicate=receipt),
            dict(reason="lab_queue", executor={"kind": "principal", "name": "Principal"}, predicate=receipt),
            dict(reason="lab_queue", executor={"kind": "lab", "name": " "}, predicate=receipt),
            dict(reason="lab_queue", executor=lab, predicate={"kind": "decision_resolved", "decision_id": "dec_x"}),
            dict(reason="lab_queue", executor=lab, predicate={"kind": "lab_receipt", "receipt": "../pedido"}),
            dict(reason="lab_queue", executor=lab, predicate="o laboratório voltar"),
        ]
        for case in refused:
            with pytest.raises(delivery.WorkflowError):
                waits.declare(conn, task.id, task.current_run_id, **case)
        with pytest.raises(delivery.WorkflowError):
            waits.declare(conn, "t_inexistente", None, reason="lab_queue", executor=lab, predicate=receipt)
        assert _rows(conn, task.id) == []
        # A própria tabela não guarda espera sem prazo final nem sem teto de tentativas.
        waits.ensure(conn)
        columns = "id,task_id,episode,reason,executor,predicate,next_check_at,created_at,updated_at"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO nfos_waits({columns},max_attempts) VALUES('w1',?,0,'lab_queue','{{}}','{{}}',1,1,1,3)", (task.id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO nfos_waits({columns},deadline_at) VALUES('w2',?,0,'lab_queue','{{}}','{{}}',1,1,1,9)", (task.id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO nfos_waits({columns},deadline_at,max_attempts) VALUES('w3',?,0,'lab_queue','{{}}','{{}}',1,1,1,1,3)",
                         (task.id,))


def _other_objects(conn):
    return conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name<>'nfos_waits' ORDER BY type,name").fetchall()


def test_the_table_arrives_on_a_live_board_without_touching_what_is_there(board, broker):
    """A regra chega a um quadro em produção criando só a tabela dela, quantas vezes for chamada. E a decisão de uma espera
    registrada guarda o que a release anterior lê: a volta de um pacote reinicia o gateway nela, com a tabela já no banco."""
    with kb.connect_closing() as conn:
        task = _card(conn, 212)
        before = [tuple(r) for r in _other_objects(conn)]
        assert _rows(conn, task.id) == []
        out = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)
        waits.ensure(conn)
        waits.ensure(conn)
        assert [tuple(r) for r in _other_objects(conn)] == before, "nenhuma tabela, índice ou gatilho que já existia mudou"
        assert len(_rows(conn, task.id)) == 1
        saved = _decision(conn, out["decision_id"])[1]["lab_wait"]
        assert saved["hold"] is True and saved["receipt"] == RECEIPT and isinstance(saved["since"], int)
        assert "kind" not in saved, "sem kind, a varredura da release anterior trata a espera como recibo"


def test_queue_wait_that_expires_ends_with_the_card_released_instead_of_pending_forever(board, broker, clock):
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 203)
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        assert (decision_id, "escalated") in _tick(conn, task.id)
        assert len(_requests(conn, task.id)) == 1, "vencida, a decisão vai ao Principal uma vez"
        row, context = _decision(conn, decision_id)
        assert row["status"] == "pending" and context["lab_wait"]["hold"] is False
        assert decision_id in [d["id"] for d in delivery.pending_decisions(conn)]

        _silent_principal(conn, clock, task.id, 2 * HOUR)
        requests = _requests(conn, task.id)
        assert len(requests) == 1 + delivery.DECISION_MAX_REMINDERS, "o pedido e os lembretes contados, nada além"
        row, context = _decision(conn, decision_id)
        assert row["status"] == "resolved" and row["action"] == "continue", "sem decisão do Principal, o motor responde"
        assert "espera vencida" in row["answer"] and RECEIPT in row["answer"] and "continuação" in row["answer"]
        assert context["wait_rule"]["resolved_by"] == "runtime"
        assert not kb._nfos_decision_open(conn, task.id), "nada mais segura o card"
        assert kb.get_task(conn, task.id).status == "ready" and kb.get_task(conn, task.id).block_kind != "awaiting_principal"
        states = [(w["reason"], w["status"], w["outcome"]) for w in _rows(conn, task.id)]
        assert states == [("lab_queue", "expired", "principal_handoff"), ("principal_decision", "expired", "returned_to_worker")]
        assert {"nfos_wait_expired", "nfos_lab_wait_escalated"} <= set(_kinds(conn, task.id))

        _silent_principal(conn, clock, task.id, 4 * HOUR)
        assert len(_requests(conn, task.id)) == len(requests), "espera encerrada não acorda mais ninguém"


def test_principal_answer_within_the_cap_is_his_and_stops_the_reminders(board, broker, clock):
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 204)
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        _tick(conn, task.id)
        _silent_principal(conn, clock, task.id, delivery.DECISION_REMINDER_AFTER + TICK)
        assert len(_requests(conn, task.id)) == 2, "um lembrete depois de 10 min"
        delivery.resolve_decision(conn, decision_id, action="continue", answer="CONTINUE: reenvie o pedido com outro ID.",
                                  author="Principal")
        _silent_principal(conn, clock, task.id, 2 * HOUR)
        assert len(_requests(conn, task.id)) == 2
        row, context = _decision(conn, decision_id)
        assert row["answer"].startswith("CONTINUE: reenvie") and "wait_rule" not in context
        handed = [w for w in _rows(conn, task.id) if w["reason"] == "principal_decision"][0]
        assert (handed["status"], handed["outcome"], handed["attempts"]) == ("satisfied", "resumed", 1)
        assert json.loads(handed["evidence"])["action"] == "continue"


def test_expired_wait_is_not_declared_again_in_the_same_round_of_the_card(board, broker, clock):
    predicate = {"kind": "lab_receipt", "receipt": RECEIPT}
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 205)
        assert waits.refusal(conn, task.id, reason="lab_queue", predicate=predicate) is None
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        _tick(conn, task.id)
        _silent_principal(conn, clock, task.id, 2 * HOUR)
        with pytest.raises(delivery.WorkflowError, match="já venceu neste card"):
            delivery.lab_wait(conn, task.id, 999, RECEIPT)
        assert len(_rows(conn, task.id)) == 2 and kb._nfos_decision_open(conn, task.id) is False
        other = {"kind": "lab_receipt", "receipt": "t-novo1234-p6"}
        assert waits.refusal(conn, task.id, reason="lab_queue", predicate=other) is None, "pedido novo é outra espera"
        # Card concluído e reaberto começa outra rodada: a espera vencida da rodada anterior não vale contra ela.
        kb._append_event(conn, task.id, "status", {"status": "ready", "previous_status": "done"})
        conn.commit()
        assert waits.episode(conn, task.id) == 1
        assert waits.refusal(conn, task.id, reason="lab_queue", predicate=predicate) is None


def test_a_step_applies_once_even_when_two_ticks_read_the_same_wait(board, broker, clock, monkeypatch):
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 206)
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        _tick(conn, task.id)
        handed = waits.open_for_decision(conn, decision_id)
        assert handed["reason"] == "principal_decision"
        now = clock(delivery.DECISION_REMINDER_AFTER + 1)
        from hermes_cli import nfos_waits as registry
        check, raced, inner = registry._decision_resolved, [], []

        def racing_check(conn_, wait, budget=None):
            if not raced:  # outro tick aplica o mesmo passo enquanto este ainda confere
                raced.append(True)
                inner.append(registry.step(conn_, wait["id"], now=now))
            return check(conn_, wait)

        monkeypatch.setattr(registry, "_decision_resolved", racing_check)
        assert waits.step(conn, handed["id"], now=now) is None, "o passo que leu a revisão antiga não grava"
        assert inner == ["wait"]
        after = waits.get(conn, handed["id"])
        assert after["attempts"] == 1 and after["revision"] == handed["revision"] + 1
        assert len(_requests(conn, task.id)) == 2, "um pedido e um lembrete, não dois"


def test_wait_recorded_before_the_registry_is_adopted_with_its_decision_start_and_read_errors(board, broker, clock):
    with kb.connect_closing() as conn:
        task = _card(conn, 207)
        since = int(time.time())
        legacy = {"receipt": RECEIPT, "hold": True, "since": since, "checked_at": since, "next_check_at": since + 300,
                  "status": "queued", "reason": "capacity", "errors": 2}
        old = delivery._register_lab_wait(conn, task.id, task.current_run_id, "Laboratório: o pedido está na fila do broker.", legacy,
                                          same=lambda saved: False, event={"receipt": RECEIPT}, next_text="Encerre o turno.")
        _yield_turn(conn, task)
        assert _rows(conn, task.id) == []
        clock(2 * HOUR)
        assert _tick(conn, task.id) == [(old["decision_id"], "wait")]
        wait = waits.open_for_decision(conn, old["decision_id"])
        assert wait["decision_id"] == old["decision_id"] and wait["created_at"] == since
        assert wait["deadline_at"] == since + delivery.LAB_WAIT_ESCALATE_SECONDS, "o prazo conta do início de verdade"
        assert wait["origin"]["adopted_from"] == "nfos_decisions.context.lab_wait" and wait["attempts"] == 1
        assert _decision(conn, old["decision_id"])[1]["lab_wait"]["since"] == since, "a decisão antiga fica como estava"
        clock(TICK)
        assert _tick(conn, task.id) == [(old["decision_id"], "wait")] and len(_rows(conn, task.id)) == 1, "adotada uma vez"


def test_old_wait_for_a_request_that_already_expired_in_the_registry_is_answered_not_left_without_owner(board, broker, clock):
    """Volta de pacote: a release anterior não conhece a recusa e registra de novo a espera do pedido que já venceu."""
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 213)
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        _tick(conn, task.id)
        _silent_principal(conn, clock, task.id, 2 * HOUR)
        since = int(time.time())
        conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (task.current_run_id, task.id))
        conn.execute("UPDATE task_runs SET status='running', outcome=NULL, ended_at=NULL WHERE id=?", (task.current_run_id,))
        conn.commit()
        legacy = {"receipt": RECEIPT, "hold": True, "since": since, "next_check_at": since + 300, "status": "queued"}
        old = delivery._register_lab_wait(conn, task.id, task.current_run_id, "Laboratório: o pedido está na fila do broker.", legacy,
                                          same=lambda saved: False, event={"receipt": RECEIPT}, next_text="Encerre o turno.")
        _yield_turn(conn, task)
        clock(TICK)
        _tick(conn, task.id)
        row, context = _decision(conn, old["decision_id"])
        assert row["status"] == "resolved" and "já venceu neste card" in row["answer"]
        assert context["lab_wait"]["hold"] is False and context["wait_rule"]["outcome"] == "refused"
        assert not kb._nfos_decision_open(conn, task.id) and len(_rows(conn, task.id)) == 2


def test_old_wait_the_registry_cannot_adopt_for_another_reason_is_left_alone(board, broker, clock):
    """Só a recusa de espera vencida responde a decisão. Recibo antigo fora do formato não vira resposta nem libera o card."""
    with kb.connect_closing() as conn:
        task = _card(conn, 214)
        since = int(time.time())
        legacy = {"receipt": "../recibo fora do formato", "hold": True, "since": since, "next_check_at": since, "status": "queued"}
        old = delivery._register_lab_wait(conn, task.id, task.current_run_id, "Laboratório: o pedido está na fila do broker.", legacy,
                                          same=lambda saved: False, event={"receipt": "x"}, next_text="Encerre o turno.")
        _yield_turn(conn, task)
        clock(TICK)
        _tick(conn, task.id)
        row, context = _decision(conn, old["decision_id"])
        assert row["status"] == "pending" and context["lab_wait"]["hold"] is True and "wait_rule" not in context
        assert _rows(conn, task.id) == []


def test_decision_already_handed_to_the_principal_before_the_registry_gets_its_counted_reminders(board, broker, clock):
    with kb.connect_closing() as conn:
        task = _card(conn, 208)
        since = int(time.time()) - 13 * HOUR
        legacy = {"receipt": RECEIPT, "hold": False, "since": since, "status": "queued", "escalated_at": since + 12 * HOUR,
                  "escalated_because": "fila há mais de 12 h"}
        old = delivery._register_lab_wait(conn, task.id, task.current_run_id, "Laboratório: o pedido está na fila do broker.", legacy,
                                          same=lambda saved: False, event={"receipt": RECEIPT}, next_text="Encerre o turno.")
        context = _decision(conn, old["decision_id"])[1]
        context["reminders"] = [since + 12 * HOUR + 600, since + 12 * HOUR + 1500]
        conn.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(context), old["decision_id"]))
        _yield_turn(conn, task)
        _tick(conn, task.id)
        handed = waits.open_for_decision(conn, old["decision_id"])
        assert (handed["reason"], handed["attempts"], handed["executor"]["kind"]) == ("principal_decision", 2, "principal")
        _silent_principal(conn, clock, task.id, 2 * HOUR)
        assert len(_requests(conn, task.id)) == 1, "faltava um lembrete do teto de três"
        assert _decision(conn, old["decision_id"])[0]["status"] == "resolved" and not kb._nfos_decision_open(conn, task.id)


def test_wait_of_a_closed_card_is_withdrawn_without_reading_the_broker(board, broker, clock):
    receipts, reads = broker
    with kb.connect_closing() as conn:
        task, decision_id = _queued_wait(conn, 209)
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (task.id,))
        conn.commit()
        reads.clear()
        clock(TICK + 1)
        assert _tick(conn, task.id) == [] and reads == []
        wait = _rows(conn, task.id)[0]
        assert (wait["status"], wait["outcome"]) == ("withdrawn", "card_closed") and wait["closed_at"]


def test_runtime_answer_of_an_expired_wait_does_not_release_a_retention(board, broker, clock):
    """A resposta que o motor grava quando o Principal não decide fala só da espera vencida: um blocked/transient posto
    por outro motivo no mesmo card segue de pé, como a resposta automática da fila (LAB_ANSWER_KEEPS_RETENTION_20261010)."""
    with kb.connect_closing() as conn:
        task = _card(conn, 210)
        decision_id = delivery.lab_wait(conn, task.id, task.current_run_id, RECEIPT)["decision_id"]
        _retain(conn, task)
        clock(delivery.LAB_WAIT_ESCALATE_SECONDS + 60)
        _tick(conn, task.id)
        _silent_principal(conn, clock, task.id, 2 * HOUR)
        row, context = _decision(conn, decision_id)
        assert row["status"] == "resolved" and context["wait_rule"]["resolved_by"] == "runtime"
        assert delivery.sweep_awaiting_principal(conn) == []
        assert kb.get_task(conn, task.id).status == "blocked"
