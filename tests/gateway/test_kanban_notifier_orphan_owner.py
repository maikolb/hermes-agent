"""NOTIFY_FOREIGN_OWNER_20261016: subscriptions owned by a profile this gateway does not serve.

16/10/2026, Concursa cut-over: 227 subscriptions kept the owner `hermes-project-factory`, which has no gateway on the new host.
The listing filters by owner in SQL, so nothing claimed them, nothing was logged, and the Principal was never woken for the 19
cards with a pending decision. The notifier does not touch them (the owner may be a gateway that is restarting or a multiplexed
profile; the cut-over procedure migrates the owner with the SQL of CONTORNO-CURSOR.md). It says so: a WARNING with the count at
most every 10 min per owner, and `kanban_foreign_owner_subs` in the runtime status."""
import asyncio
import logging

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

OLD_OWNER = "hermes-project-factory"

pytestmark = pytest.mark.usefixtures("kanban_executor_catalog")


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._active_profile_name = lambda: "concursa"
    runner._kanban_dispatcher_lock_handle = None
    return runner


def _tick(monkeypatch, runner):
    runner._running = True  # cada volta para o laço ao fim; a próxima o religa
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    async def run():
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)
    asyncio.run(run())


@pytest.fixture
def status_writes(monkeypatch):
    import gateway.status as status
    writes = []
    monkeypatch.setattr(status, "write_runtime_status", lambda **kw: writes.append(kw))
    return writes


def _board(tmp_path, monkeypatch, owner=OLD_OWNER):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "foreign-owner.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="cartao antigo", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", notifier_profile=owner)
        kb.block_task(conn, task_id, reason="evento que o dono ausente nao entregou", kind="needs_input")
    finally:
        conn.close()
    return task_id


def _sub(task_id):
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id)[0]
    finally:
        conn.close()


def test_a_foreign_owner_is_left_alone_and_said_once_in_the_log_and_in_the_status(tmp_path, monkeypatch, caplog, status_writes):
    task_id = _board(tmp_path, monkeypatch)
    before = _sub(task_id)
    adapter, runner = RecordingAdapter(), None
    runner = _runner(adapter)
    with caplog.at_level(logging.WARNING):
        _tick(monkeypatch, runner)
        _tick(monkeypatch, runner)  # 2 voltas, 1 aviso
    after = _sub(task_id)
    assert (after["notifier_profile"], after["last_event_id"]) == (OLD_OWNER, before["last_event_id"])
    assert adapter.sent == []
    warned = [r.getMessage() for r in caplog.records if "not served by this gateway" in r.getMessage()]
    assert len(warned) == 1 and "1 subscription(s)" in warned[0] and OLD_OWNER in warned[0]
    assert status_writes == [{"kanban_foreign_owner_subs": {OLD_OWNER: 1}}]  # só quando muda


def test_the_warning_comes_back_after_ten_minutes_and_the_status_clears_when_the_owner_is_migrated(
        tmp_path, monkeypatch, caplog, status_writes):
    task_id = _board(tmp_path, monkeypatch)
    runner = _runner(RecordingAdapter())
    with caplog.at_level(logging.WARNING):
        _tick(monkeypatch, runner)
        for key in runner._kanban_foreign_owner_warned:
            runner._kanban_foreign_owner_warned[key] -= 601
        _tick(monkeypatch, runner)
    assert len([r for r in caplog.records if "not served by this gateway" in r.getMessage()]) == 2
    conn = kb.connect()
    try:
        conn.execute("UPDATE kanban_notify_subs SET notifier_profile='concursa' WHERE task_id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    _tick(monkeypatch, runner)  # o procedimento de corte migrou o dono: a métrica zera
    assert status_writes[-1] == {"kanban_foreign_owner_subs": {}}


def test_nothing_is_said_when_every_owner_is_served(tmp_path, monkeypatch, caplog, status_writes):
    _board(tmp_path, monkeypatch, owner="concursa")
    with caplog.at_level(logging.WARNING):
        _tick(monkeypatch, _runner(RecordingAdapter()))
    assert not [r for r in caplog.records if "not served by this gateway" in r.getMessage()]
    assert status_writes == [{"kanban_foreign_owner_subs": {}}]  # a primeira volta do processo escreve o total, vazio


def test_a_new_process_clears_the_status_the_old_one_left(tmp_path, monkeypatch, status_writes):
    """Reinício depois da migração do dono: o total vazio da primeira volta é escrito, o arquivo velho não sobrevive."""
    _board(tmp_path, monkeypatch, owner="concursa")
    _tick(monkeypatch, _runner(RecordingAdapter()))
    assert status_writes == [{"kanban_foreign_owner_subs": {}}]
