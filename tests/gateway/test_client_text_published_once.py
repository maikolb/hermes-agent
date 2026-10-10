"""CLIENT_TEXT_ONCE_20261010: mensagem que foi ao chat de quem pediu não sai de novo.

O notificador publica a mensagem e depois grava o que precisa (recibo, progresso da entrega, cursor da assinatura).
Uma dessas gravações falhando fazia o cursor recuar, e o tique seguinte, cinco segundos depois, publicava a mesma
mensagem outra vez, sem limite. Vale para a devolutiva ao cliente (CLIENT_DELIVERY_20260915) e para o aviso do prazo
(REQUESTER_REMINDER_20261010). O evento também pode voltar depois de um reinício do gateway.
"""
import asyncio
import sqlite3

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb

CLIENT = ({"platform": "telegram", "chat_id": "origin-chat", "thread_id": None}, True)
REMINDER = ("PERGUNTA para Jhonatan: qual competência conferir?\n\nSe não recebermos sua resposta até 12/10 às 09:00, seguimos "
            "com o que já temos e fechamos como entrega parcial. Se você responder depois, retomamos o pedido.")


def _locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


def _board(tmp_path, monkeypatch, name):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)  # sem perfil real na máquina de teste


def _delivery(tmp_path, monkeypatch, name):
    """Card do cliente já concluído, com a assinatura do chat de onde o pedido veio."""
    _board(tmp_path, monkeypatch, name)
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: CLIENT)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Cargo 301 sem disciplinas", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin-chat")
        assert kb.complete_task(conn, tid, summary="Tempo total: n/a")
    return tid


def _reminder(tmp_path, monkeypatch, name):
    """Card com o aviso do prazo já pedido pelo motor para o tópico de origem."""
    _board(tmp_path, monkeypatch, name)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Folha de agosto", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin-chat", thread_id="8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "nfos_requester_reminder", {
                "decision_id": "dec_abc", "due_at": 1791806400, "text": REMINDER, "recipient": "Jhonatan",
                "origin": {"platform": "telegram", "chat_id": "origin-chat", "thread_id": "8"}})
    return tid


def _gateway(monkeypatch):
    """Um gateway que fica no ar entre os tiques, e a forma de reiniciá-lo."""
    from tests.gateway.test_kanban_notifier import EditableRecordingAdapter, _make_runner, _run_one_notifier_tick
    adapter = EditableRecordingAdapter()
    state = {"runner": _make_runner(adapter)}

    def tick():
        state["runner"]._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, state["runner"]))

    def restart():
        state["runner"] = _make_runner(adapter)

    return adapter, tick, restart


def _events(tid, kind):
    with kb.connect_closing() as conn:
        return conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchone()[0]


def _rewind(tid):
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET last_event_id=0 WHERE task_id=?", (tid,))


def test_delivery_is_not_published_again_when_the_progress_of_the_notification_cannot_be_written(tmp_path, monkeypatch):
    tid = _delivery(tmp_path, monkeypatch, "delivery-progress")
    adapter, tick, _ = _gateway(monkeypatch)
    monkeypatch.setattr("gateway.wake.record_notify_progress", _locked)
    for _ in range(3):
        tick()
    assert [message["text"] for message in adapter.sent] == ["Concluído: Cargo 301 sem disciplinas"]
    assert _events(tid, "nfos_client_delivery_published") == 1


def test_delivery_attachments_are_not_uploaded_again_with_a_delivery_that_is_already_out(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    _delivery(tmp_path, monkeypatch, "delivery-artifacts")
    adapter, tick, _ = _gateway(monkeypatch)
    uploads = []

    async def upload(self, **kwargs):
        uploads.append(kwargs["chat_id"])

    monkeypatch.setattr(GatewayRunner, "_deliver_kanban_artifacts", upload)
    monkeypatch.setattr("gateway.wake.record_notify_progress", _locked)
    for _ in range(3):
        tick()
    assert uploads == ["origin-chat"] and len(adapter.sent) == 1


def test_delivery_is_not_published_again_when_its_event_comes_back_after_a_restart(tmp_path, monkeypatch):
    tid = _delivery(tmp_path, monkeypatch, "delivery-restart")
    adapter, tick, restart = _gateway(monkeypatch)
    tick()
    _rewind(tid)  # o gateway caiu entre publicar e avançar o cursor da assinatura
    restart()
    tick()
    assert len(adapter.sent) == 1 and _events(tid, "nfos_client_delivery_published") == 1


def test_a_card_that_closes_again_gets_its_new_delivery(tmp_path, monkeypatch):
    tid = _delivery(tmp_path, monkeypatch, "delivery-twice")
    adapter, tick, _ = _gateway(monkeypatch)
    tick()
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "completed", {"summary": "Segunda entrega."})
    tick()
    assert len(adapter.sent) == 2 and _events(tid, "nfos_client_delivery_published") == 2


def test_reminder_is_not_published_again_while_nothing_about_it_can_be_written(tmp_path, monkeypatch):
    """O envio deu certo, o recibo não gravou e o progresso também não: a mensagem não sai de novo."""
    tid = _reminder(tmp_path, monkeypatch, "reminder-locked")
    adapter, tick, _ = _gateway(monkeypatch)
    monkeypatch.setattr(kw, "_record_requester_reminder", _locked)
    monkeypatch.setattr("gateway.wake.record_notify_progress", _locked)
    for _ in range(3):
        tick()
    assert [message["text"] for message in adapter.sent] == [REMINDER]
    assert _events(tid, "nfos_requester_reminder_delivered") == 0


def test_reminder_with_its_receipt_is_not_published_again_when_the_progress_cannot_be_written(tmp_path, monkeypatch):
    tid = _reminder(tmp_path, monkeypatch, "reminder-progress")
    adapter, tick, restart = _gateway(monkeypatch)
    monkeypatch.setattr("gateway.wake.record_notify_progress", _locked)
    for _ in range(3):
        tick()
        restart()  # nem a memória do processo sobra: quem segura é o recibo gravado
    assert [message["text"] for message in adapter.sent] == [REMINDER]
    assert _events(tid, "nfos_requester_reminder_delivered") == 1
