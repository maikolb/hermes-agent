"""Exercise explicit unlimited outage recovery through the existing gateway."""
import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from gateway.run import GatewayRunner
import pytest
from gateway.config import Platform
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def test_unclean_recovery_honors_unlimited_config(monkeypatch):
    monkeypatch.setenv('HERMES_AUTO_CONTINUE_FRESHNESS','0')
    store=SimpleNamespace(recover_interrupted_turns=AsyncMock(return_value=1),
                          suspend_recently_active=AsyncMock(return_value=0))
    runner=SimpleNamespace(async_session_store=store)
    assert asyncio.run(GatewayRunner._recover_unclean_sessions(runner))==(1,0)
    store.recover_interrupted_turns.assert_awaited_once_with(max_age_seconds=0)


@pytest.mark.asyncio
async def test_old_pending_turn_is_scheduled_once_with_unlimited_recovery(monkeypatch):
    monkeypatch.setenv('HERMES_AUTO_CONTINUE_FRESHNESS','0')
    runner,adapter=make_restart_runner()
    source=make_restart_source(chat_id='continuity-test')
    old=datetime.now()-timedelta(days=7)
    entry=SessionEntry(session_key='agent:main:telegram:dm:continuity-test',
        session_id='saved-worker',created_at=old,updated_at=old,origin=source,
        platform=Platform.TELEGRAM,chat_type='dm',resume_pending=True,
        resume_reason='restart_interrupted',last_resume_marked_at=old)
    runner.session_store._entries={entry.session_key:entry}
    gate=asyncio.Event()
    async def handle(event):
        await gate.wait()
    adapter.handle_message=handle
    assert runner._schedule_resume_pending_sessions()==1
    assert runner._schedule_resume_pending_sessions()==0
    gate.set()
    await asyncio.sleep(0.05)
