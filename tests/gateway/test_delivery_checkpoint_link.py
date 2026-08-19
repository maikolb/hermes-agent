from __future__ import annotations


def test_delivery_ledger_recovery_preserves_durable_session_id(tmp_path, monkeypatch):
    from gateway import delivery_ledger as ledger

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: db_path)
    monkeypatch.setattr(ledger, "_owner_stamp", lambda: (101, 202))

    ledger.record_obligation(
        obligation_id="ob-checkpoint-link",
        session_key="telegram:chat:thread:user",
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        content="owed response",
        session_id="durable-session-id",
    )

    monkeypatch.setattr(ledger, "_owner_alive", lambda *args: False)
    rows = ledger.sweep_recoverable(
        deliverable_platforms={"telegram"},
    )

    assert len(rows) == 1
    assert rows[0]["obligation_id"] == "ob-checkpoint-link"
    assert rows[0]["session_id"] == "durable-session-id"
