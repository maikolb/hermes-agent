"""NOTIFY_ORPHAN_OWNER_20261016: subscriptions of a profile that no gateway runs for.

16/10/2026, Concursa cut-over: 227 subscriptions kept the owner `hermes-project-factory`, which has no gateway on the new
host. The gateway's listing filters by owner in SQL, so nothing claimed them, nothing was logged, and the Principal was never
woken for the 19 cards with a pending decision. They now pass to the running gateway's profile with the cursor at the last event
of each task, and the count goes to the log as a warning."""
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


def _board(tmp_path, monkeypatch, *, owner_profile_exists=False):
    # O detector é o real: HERMES_HOME próprio, com ou sem a pasta do perfil dono.
    from hermes_cli import profiles
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "profiles" / "worker").mkdir(parents=True)  # o executor dos cartões
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "home" / "profiles")  # a pasta de perfis é esta
    if owner_profile_exists:
        (tmp_path / "home" / "profiles" / OLD_OWNER).mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "orphan-owner.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="cartao antigo", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", notifier_profile=OLD_OWNER)
        kb.block_task(conn, task_id, reason="evento antigo, ninguem entregou", kind="needs_input")  # antes da adocao
    finally:
        conn.close()
    return task_id


def _sub(task_id):
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id)[0], conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def test_subscriptions_of_an_owner_that_is_no_profile_of_the_host_pass_to_the_running_profile_without_replaying_history(
        tmp_path, monkeypatch, caplog):
    task_id = _board(tmp_path, monkeypatch)
    adapter = RecordingAdapter()
    with caplog.at_level(logging.WARNING):
        _tick(monkeypatch, _runner(adapter))
    sub, last_event = _sub(task_id)
    assert (sub["notifier_profile"], sub["last_event_id"]) == ("concursa", last_event)
    assert adapter.sent == []  # o evento antigo não volta ao chat
    warned = [r.getMessage() for r in caplog.records if "had 1 subscription(s) owned by" in r.getMessage()]
    assert warned and OLD_OWNER in warned[0] and "1 passed to profile concursa" in warned[0]
    conn = kb.connect()
    try:
        kb.complete_task(conn, task_id, summary="evento novo")
    finally:
        conn.close()
    _tick(monkeypatch, _runner(adapter))
    assert len(adapter.sent) == 1 and "cartao antigo" in adapter.sent[0]["text"]  # o que vem depois chega


def test_an_owner_that_is_a_profile_of_the_host_is_left_alone_and_the_warning_is_rate_limited(tmp_path, monkeypatch, caplog):
    """Gateway reiniciando (lock some), multiplex ou outro gateway: o perfil existe, as linhas não mudam. Aviso contado a cada 10 min."""
    task_id = _board(tmp_path, monkeypatch, owner_profile_exists=True)
    before, _ = _sub(task_id)
    adapter = RecordingAdapter()
    with caplog.at_level(logging.WARNING):
        _tick(monkeypatch, _runner(adapter))
        _tick(monkeypatch, _runner(adapter))
    after, _ = _sub(task_id)
    assert after["notifier_profile"] == OLD_OWNER and after["last_event_id"] == before["last_event_id"]
    assert adapter.sent == []
    assert len([r for r in caplog.records if "not served by this gateway" in r.getMessage() and "1 subscription(s)" in r.getMessage()]) >= 1


def test_a_subscription_with_a_claim_in_flight_is_not_adopted(tmp_path, monkeypatch):
    task_id = _board(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        kb.claim_unseen_events_for_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", claim_token="t")
        assert kb.adopt_orphan_notify_subs(conn, owners=[OLD_OWNER], new_owner="concursa") == 0
    finally:
        conn.close()
    assert _sub(task_id)[0]["notifier_profile"] == OLD_OWNER


def test_a_claim_whose_lease_ran_out_does_not_hold_the_adoption(tmp_path, monkeypatch):
    task_id = _board(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        kb.claim_unseen_events_for_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", claim_token="t")
        conn.execute("UPDATE kanban_notify_claims SET lease_until = 1 WHERE task_id = ?", (task_id,))
        conn.commit()
        assert kb.adopt_orphan_notify_subs(conn, owners=[OLD_OWNER], new_owner="concursa") == 1
    finally:
        conn.close()
