"""LEAK_FIX_20260914: a barra de progresso de um card nunca publica no tópico de outro card."""
import asyncio

from gateway import kanban_watchers as kw
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": dict(metadata or {})})
        return SendResult(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, chat_id, message_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "edit": message_id, "text": text, "metadata": dict(metadata or {})})
        return SendResult(success=True, message_id=message_id)


def test_each_subscription_gets_its_own_topic_in_the_same_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "topics.db"))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)  # sem perfil real na máquina de teste
    kb.init_db()
    conn = kb.connect()
    try:
        cards = []
        for title, thread in (("Validade do plano", "3234"), ("Cargo sem disciplinas", "41"), ("Lote de clientes", "7122")):
            tid = kb.create_task(conn, title=title, assignee="worker")
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="-1004309874643", thread_id=thread)
            kb._append_event(conn, tid, kind="claimed")
            cards.append((tid, thread))
    finally:
        conn.close()
    calls = []

    async def capture(kind, sub, board, adapter, metadata):
        calls.append((sub["task_id"], sub.get("thread_id"), dict(metadata or {})))
        return True

    monkeypatch.setattr(kw, "_kanban_progress_bar", capture)
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(_Adapter())))
    assert sorted(task for task, _, _ in calls) == sorted(tid for tid, _ in cards)
    for task, thread, metadata in calls:
        assert metadata.get("thread_id") == thread, (task, thread, metadata)


def test_progress_bar_ignores_a_topic_that_is_not_its_subscription(monkeypatch):
    monkeypatch.setattr(kw, "_progress_state", lambda board, task_id: (None, 1, 0, 30, "M", "Cargo sem disciplinas", "", 1))
    monkeypatch.setattr(kw, "_progress_meta_get", lambda board, sub: {})
    monkeypatch.setattr(kw, "_progress_meta_set", lambda board, sub, **fields: None)
    adapter = _Adapter()
    sub = {"task_id": "t_ff7b8ccc", "platform": "telegram", "chat_id": "-1004309874643", "thread_id": "41"}
    assert asyncio.run(kw._kanban_progress_bar("claimed", sub, "concursa-ai", adapter, {"thread_id": "3234", "message_thread_id": "3234"}))
    assert adapter.sent[-1]["metadata"].get("thread_id") == "41" and "message_thread_id" not in adapter.sent[-1]["metadata"]

    no_topic = dict(sub, thread_id=None, task_id="t_dm")
    assert asyncio.run(kw._kanban_progress_bar("claimed", no_topic, "concursa-ai", adapter, {"thread_id": "3234"}))
    assert "thread_id" not in adapter.sent[-1]["metadata"]
