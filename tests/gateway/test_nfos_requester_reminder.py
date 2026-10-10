"""REQUESTER_REMINDER_20261010: o aviso do prazo de uma pergunta ao solicitante chega ao tópico do card.

O prazo da pergunta (REQUESTER_DEADLINE_20261010) só vence onde o aviso chega a quem tem de responder. Em card nascido
num chat a pergunta vem pelo texto do Principal e o evento blocked vira edição da barra de progresso, que não notifica
ninguém. Aqui o notificador publica o aviso que o motor pediu como mensagem nova, só no tópico de origem, e grava o
recibo que o motor exige para contar o prazo. A barra de progresso e a rotação de workers ficam como estavam.
"""
import asyncio
import json
import sqlite3

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb

TEXT = ("PERGUNTA para Jhonatan: qual competência conferir?\n\nSe não recebermos sua resposta até 12/10 às 09:00, seguimos com "
        "o que já temos e fechamos como entrega parcial. Se você responder depois, retomamos o pedido.")
ORIGIN = {"platform": "telegram", "chat_id": "origin-chat", "thread_id": "8"}
QUIET = {"kanban": {"notify_kinds": ["completed", "blocked", "model_fallback"]}}


def _setup(tmp_path, monkeypatch, name, *, status="blocked", other_chats=(), text=TEXT):
    from tests.gateway.test_kanban_notifier import EditableRecordingAdapter, _make_runner, _run_one_notifier_tick
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)  # sem perfil real na máquina de teste
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Folha de agosto", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin-chat", thread_id="8")
        for chat in other_chats:
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id=chat)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            kb._append_event(conn, tid, "nfos_requester_reminder",
                             {"decision_id": "dec_abc", "due_at": 1791806400, "text": text, "recipient": "Jhonatan", "origin": ORIGIN})
    finally:
        conn.close()
    adapter = EditableRecordingAdapter()
    return tid, adapter, lambda: asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))


def _events(tid, kind):
    conn = kb.connect()
    try:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]
    finally:
        conn.close()


def test_reminder_reaches_the_origin_topic_once_as_a_new_message_with_a_receipt(tmp_path, monkeypatch):
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "topic")
    tick()
    tick()  # segundo tick: o evento já foi entregue, nada se repete
    (sent,) = adapter.sent
    assert sent["text"] == TEXT and sent["chat_id"] == "origin-chat" and sent["metadata"].get("thread_id") == "8"
    assert adapter.edited == []
    (receipt,) = _events(tid, "nfos_requester_reminder_delivered")
    assert receipt == {"decision_id": "dec_abc", "due_at": 1791806400, "message_id": sent["message_id"],
                       "platform": "telegram", "chat_id": "origin-chat", "thread_id": "8"}


def test_reminder_is_the_second_message_the_client_chat_contract_lets_through(tmp_path, monkeypatch):
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "client")
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: (dict(ORIGIN), True))
    tick()
    assert [message["text"] for message in adapter.sent] == [TEXT]
    assert len(_events(tid, "nfos_requester_reminder_delivered")) == 1
    assert _events(tid, "client_publication_suppressed") == []
    assert kw._REQUESTER_REMINDER_KIND not in kw._CLIENT_SILENT_KINDS


def test_quiet_mode_keeps_the_reminder_and_still_drops_the_other_kinds():
    assert kw._notify_kind_allowed("nfos_requester_reminder", lambda: QUIET) is True
    assert kw._notify_kind_allowed("claimed", lambda: QUIET) is False
    assert kw._notify_kind_allowed("nfos_principal_requested", lambda: QUIET) is False


def test_reminder_goes_only_to_the_chat_the_request_came_from(tmp_path, monkeypatch):
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "two-chats", other_chats=("ops-chat",))
    tick()
    assert [message["chat_id"] for message in adapter.sent] == ["origin-chat"]
    assert len(_events(tid, "nfos_requester_reminder_delivered")) == 1


def test_question_already_answered_is_not_reminded(tmp_path, monkeypatch):
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "answered", status="running")
    tick()
    assert adapter.sent == [] and _events(tid, "nfos_requester_reminder_delivered") == []


def test_reminder_without_text_publishes_nothing_and_leaves_no_receipt(tmp_path, monkeypatch):
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "empty", text="  ")
    tick()
    assert adapter.sent == [] and _events(tid, "nfos_requester_reminder_delivered") == []


def test_progress_bar_does_not_take_the_reminder(tmp_path, monkeypatch):
    from tests.gateway.test_kanban_notifier import EditableRecordingAdapter
    adapter = EditableRecordingAdapter()
    sub = {"task_id": "t_x", "platform": "telegram", "chat_id": "origin-chat", "thread_id": "8"}
    assert asyncio.run(kw._kanban_progress_bar("nfos_requester_reminder", sub, None, adapter, {})) is False
    assert adapter.sent == [] and adapter.edited == []


def test_receipt_that_cannot_be_written_never_publishes_the_reminder_again(tmp_path, monkeypatch):
    """REMINDER_NOT_RESENT_20261010: a mensagem já está no chat de quem pediu. O recibo que não grava não a repete."""
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "no-receipt")

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kw, "_record_requester_reminder", locked)
    for _ in range(3):
        tick()
    assert [message["text"] for message in adapter.sent] == [TEXT]
    assert _events(tid, "nfos_requester_reminder_delivered") == []


def test_reminder_with_a_receipt_is_not_published_again_when_its_event_comes_back(tmp_path, monkeypatch):
    """O gateway pode cair entre publicar e avançar o cursor da assinatura: o evento volta, a mensagem não."""
    tid, adapter, tick = _setup(tmp_path, monkeypatch, "replayed")
    tick()
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET last_event_id=0 WHERE task_id=?", (tid,))
    finally:
        conn.close()
    tick()
    assert [message["text"] for message in adapter.sent] == [TEXT]
    assert len(_events(tid, "nfos_requester_reminder_delivered")) == 1
