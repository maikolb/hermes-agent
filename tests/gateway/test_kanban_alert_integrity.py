"""Regression for real queue age and administrative recovery notifications."""
import asyncio,json,time
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from gateway.kanban_watchers import GatewayKanbanWatchersMixin, _read_worker_trace_summary
from tests.gateway.test_kanban_notifier import RecordingAdapter,_make_runner,_run_one_notifier_tick


@pytest.fixture
def card(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'board.db'))
    with kb.connect_closing() as c:
        tid=kb.create_task(c,title='Existing report',assignee='default',requires_repo=False)
        c.execute('UPDATE tasks SET created_at=? WHERE id=?',(int(time.time())-400000,tid))
        c.commit()
        kb.block_task(c,tid,reason='Missing source evidence',kind='needs_input')
        kb.unblock_task(c,tid)
    return tid


def test_old_card_uses_latest_unblock_and_alert_event_for_dedupe(card,tmp_path):
    runner=GatewayKanbanWatchersMixin()
    now=int(time.time())
    settings={'threshold':180,'default_assignee':'default'}
    with kb.connect_closing() as c:
        c.execute('UPDATE tasks SET workspace_path=? WHERE id=?',(str(tmp_path/'missing'),card))
        c.commit()
    assert runner._ready_watchdog_collect('default',now,settings)==[]
    with kb.connect_closing() as c:
        c.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='unblocked'",(now-600,card))
        c.commit()
    alerts=runner._ready_watchdog_collect('default',now,settings)
    assert len(alerts)==1 and alerts[0][3]==10
    runner._ready_watchdog_mark('default',card,alerts[0][2],[])
    assert runner._ready_watchdog_collect('default',now+60,settings)==[]


def test_healthy_ready_card_does_not_imply_stalled_dispatcher(card):
    runner=GatewayKanbanWatchersMixin()
    with kb.connect_closing() as c:
        row=c.execute('SELECT * FROM tasks WHERE id=?',(card,)).fetchone()
        assert runner._ready_watchdog_reason(row,'default',conn=c)==''


def test_reassessment_does_not_remove_live_worker_or_create_exit():
    runner=GatewayKanbanWatchersMixin()
    task=SimpleNamespace(id='existing',status='running',current_run_id=7,created_at=1,
                         worker_started_at=1,started_at=1,title='Existing work')
    sub={'platform':'telegram','chat_id':'test','thread_id':'','notifier_profile':'default'}
    row={'board':'default','task':task,'sub':sub,'bootstrap':True,'events':[
        SimpleNamespace(kind='blocked',payload={'reassessment_requested':True})]}
    runner._kanban_focus_apply_rows([row])
    assert any(task.id in bucket for bucket in runner._kanban_worker_focus_active.values())
    assert runner._kanban_worker_focus_exits=={}


def test_real_block_cause_wins_over_internal_operator_comment(card):
    with kb.connect_closing() as c:
        kb.block_task(c,card,reason='Missing source evidence',kind='needs_input')
        kb.add_comment(c,card,'operator-continuity','INTERNAL recovery instructions')
        with kb.write_txn(c):
            kb._append_event(c,card,'blocked',{'reassessment_requested':True,'summary':'INTERNAL recovery instructions'})
    assert _read_worker_trace_summary('default',card,'blocked')=='Missing source evidence'


def test_native_notifier_keeps_reassessment_brief_without_internal_prompt(card,monkeypatch):
    with kb.connect_closing() as c:
        kb.block_task(c,card,reason='Missing source evidence',kind='needs_input')
        kb.add_notify_sub(c,task_id=card,platform='telegram',chat_id='chat-1')
        with kb.write_txn(c):
            kb._append_event(c,card,'blocked',{'reassessment_requested':True,
                'summary':'INTERNAL recovery instructions','reason':'INTERNAL reasoning'})
    adapter=RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch,_make_runner(adapter)))
    assert len(adapter.sent)==1
    text=adapter.sent[0]['text']
    assert 'em reavaliação' in text and 'INTERNAL' not in text and 'Worker bloqueado' not in text
