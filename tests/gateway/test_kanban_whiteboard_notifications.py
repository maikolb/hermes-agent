"""Notifier convergence uses current state and ordinary operator wording."""
import asyncio
from tests.gateway.test_kanban_notifier import kb, RecordingAdapter, _make_runner, _run_one_notifier_tick


def test_reserved_card_does_not_claim_process_started(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Report", assignee="default", requires_repo=False)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.claim_task(conn, tid)
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.sent) == 1
    assert "reservado" in adapter.sent[0]["text"]
    assert "started" not in adapter.sent[0]["text"]


def test_activity_completion_does_not_announce_product_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Conversation", assignee="default", task_role="activity", requires_repo=False)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, result="Conversation ended")
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []
