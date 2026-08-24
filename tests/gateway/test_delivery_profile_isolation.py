"""Durable delivery recovery stays inside the producing profile namespace."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as ledger
from gateway.config import Platform
from gateway.run import GatewayRunner


def _record(home, oid, content):
    ledger.record_obligation(
        obligation_id=oid,
        session_key=f"agent:{oid}:slack:channel:C1",
        platform="slack",
        chat_id="C1",
        thread_id=None,
        content=content,
        storage_home=home,
    )
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=999999999, "
            "owner_started_at=1 WHERE obligation_id=?",
            (oid,),
        )


def _state(home, oid):
    with sqlite3.connect(home / "state.db") as conn:
        return conn.execute(
            "SELECT state, attempts FROM delivery_obligations "
            "WHERE obligation_id=?",
            (oid,),
        ).fetchone()


def _adapter():
    adapter = MagicMock()
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, error="")
    )
    return adapter


def _runner(default_adapter, worker_adapter):
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner.adapters = {Platform.SLACK: default_adapter}
    runner._profile_adapters = {"worker": {Platform.SLACK: worker_adapter}}
    runner._active_profile_name = lambda: "default"
    store = MagicMock()
    store.clear_resume_pending = AsyncMock()
    store._store = None
    runner.session_store = None
    runner._async_session_store = store
    return runner


@pytest.mark.asyncio
async def test_recovery_uses_each_profiles_own_db_and_adapter(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    worker_home = tmp_path / "profiles" / "worker"
    default_home.mkdir(parents=True)
    worker_home.mkdir(parents=True)
    _record(default_home, "default-ob", "default answer")
    _record(worker_home, "worker-ob", "worker answer")
    default_adapter = _adapter()
    worker_adapter = _adapter()
    runner = _runner(default_adapter, worker_adapter)
    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda _config: [
            ("default", default_home),
            ("worker", worker_home),
        ],
    )

    recovered = await runner._redeliver_pending_obligations()

    assert recovered == 2
    assert default_adapter.send.await_args.kwargs["content"] == "default answer"
    assert worker_adapter.send.await_args.kwargs["content"] == "worker answer"
    assert _state(default_home, "default-ob") == ("delivered", 1)
    assert _state(worker_home, "worker-ob") == ("delivered", 1)


@pytest.mark.asyncio
async def test_disconnect_after_claim_defers_and_refunds_attempt(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    default_home.mkdir()
    _record(default_home, "default-ob", "owed answer")
    default_adapter = _adapter()
    runner = _runner(default_adapter, _adapter())
    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda _config: [("default", default_home)],
    )

    claimed = await runner._claim_pending_obligations()
    assert len(claimed) == 1
    runner.adapters.clear()

    assert await runner._redeliver_claimed_obligations(claimed) == 0
    assert _state(default_home, "default-ob") == ("deferred", 0)
    default_adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_profile_reads_its_own_delivery_ledger_flag(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    default_home = (tmp_path / "default").resolve()
    worker_home = (tmp_path / "profiles" / "worker").resolve()
    default_home.mkdir(parents=True)
    worker_home.mkdir(parents=True)
    _record(worker_home, "worker-only-ob", "worker answer")
    default_adapter = _adapter()
    worker_adapter = _adapter()
    runner = _runner(default_adapter, worker_adapter)
    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda _config: [
            ("default", default_home),
            ("worker", worker_home),
        ],
    )
    monkeypatch.setattr(
        "gateway.delivery_ledger.ledger_enabled",
        lambda: get_hermes_home().resolve() == worker_home,
    )

    recovered = await runner._redeliver_pending_obligations()

    assert recovered == 1
    default_adapter.send.assert_not_awaited()
    worker_adapter.send.assert_awaited_once()
    assert _state(worker_home, "worker-only-ob") == ("delivered", 1)
