"""Real SQLite tests of reservation, interrupted delivery and fenced ACK."""
import os
import subprocess
import sys
import time

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="A report", assignee="default", requires_repo=False)
        sub = dict(task_id=tid, platform="telegram", chat_id="test", thread_id="")
        kb.add_notify_sub(conn, **sub, delivery_mode="notify+wake")
        kb.complete_task(conn, tid, summary="Done")
    return sub


def test_process_death_after_claim_preserves_delivery(delivery):
    code = (
        "import os; from hermes_cli import kanban_db as kb; c=kb.connect(); "
        f"kb.claim_unseen_events_for_sub(c, **{delivery!r}, claim_token='dead'); "
        "os._exit(0)"
    )
    child = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 0, child.stderr
    with kb.connect_closing() as conn:
        _, pending = kb.unseen_events_for_sub(conn, **delivery)
        old, cursor, events = kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="new")
        assert pending and events and cursor > old
        assert kb.advance_notify_cursor(conn, **delivery, new_cursor=cursor, claim_token="new")
        assert not kb.unseen_events_for_sub(conn, **delivery)[1]


def test_concurrent_reservation_and_stale_ack(delivery):
    with kb.connect_closing() as a, kb.connect_closing() as b:
        old, cursor, events = kb.claim_unseen_events_for_sub(a, **delivery, claim_token="a")
        assert events
        assert kb.claim_unseen_events_for_sub(b, **delivery, claim_token="b")[2] == []
        # Timeout replaces only the reservation, retaining the same obligation.
        a.execute("UPDATE kanban_notify_claims SET lease_until=0")
        a.commit()
        first = kb.get_notify_claim(a, **delivery)
        assert kb.claim_unseen_events_for_sub(b, **delivery, claim_token="b")[2]
        assert kb.get_notify_claim(b, **delivery)["delivery_id"] == first["delivery_id"]
        assert not kb.advance_notify_cursor(a, **delivery, new_cursor=cursor, claim_token="a")
        assert not kb.rewind_notify_cursor(a, **delivery, claimed_cursor=cursor,
                                          old_cursor=old, claim_token="a")
        assert kb.advance_notify_cursor(b, **delivery, new_cursor=cursor, claim_token="b")


def test_text_receipt_survives_retry_and_does_not_consume_wake(delivery):
    with kb.connect_closing() as conn:
        old, cursor, _ = kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="a")
        receipt = kb.get_notify_claim(conn, **delivery)
        kb.update_notify_receipt(conn, delivery_id=receipt["delivery_id"], notified=True)
        kb.rewind_notify_cursor(conn, **delivery, claimed_cursor=cursor, old_cursor=old, claim_token="a")
        assert kb.unseen_events_for_sub(conn, **delivery)[1]
        assert kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="b")[2]
        retry = kb.get_notify_claim(conn, **delivery)
        assert retry["notified"] and not retry["wake_accepted"]
        assert retry["delivery_id"] == receipt["delivery_id"]


def test_new_event_waits_behind_reserved_range(delivery):
    with kb.connect_closing() as conn:
        old, cursor, _ = kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="a")
        # A subsequent material event must not change an in-flight delivery identity.
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                     (delivery["task_id"], "blocked", "{}", int(time.time())))
        conn.commit()
        kb.rewind_notify_cursor(conn, **delivery, claimed_cursor=cursor, old_cursor=old, claim_token="a")
        assert kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="b")[1] == cursor
        assert kb.advance_notify_cursor(conn, **delivery, new_cursor=cursor, claim_token="b")
        assert kb.unseen_events_for_sub(conn, **delivery)[1][-1].id > cursor


def test_explicit_unsubscribe_removes_pending_receipt(delivery):
    with kb.connect_closing() as conn:
        kb.claim_unseen_events_for_sub(conn, **delivery, claim_token="a")
        kb.remove_notify_sub(conn, **delivery)
        assert kb.get_notify_claim(conn, **delivery) is None
