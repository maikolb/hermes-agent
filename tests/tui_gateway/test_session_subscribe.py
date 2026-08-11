from __future__ import annotations

import threading

import pytest

from tui_gateway import server


class FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.frames: list[dict] = []

    def write(self, frame: dict) -> bool:
        if self.fail:
            raise RuntimeError("closed peer")
        self.frames.append(frame)
        return True

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def clean_live_registries():
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()
    yield
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()


def _subscribe(session_id: str, transport: FakeTransport, request_id: str) -> dict:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session.subscribe",
            "params": {"session_id": session_id},
        },
        transport,
    )
    assert response is not None
    return response


def _event(session_id: str) -> dict:
    return server._event_frame("message.delta", session_id, {"text": "hello"})


def test_two_subscribers_receive_one_identical_session_event_each():
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}

    first = _subscribe("live-1", primary, "sub-1")
    second = _subscribe("live-1", observer, "sub-2")

    assert first["result"]["subscribed"] is True
    assert first["result"]["already_subscribed"] is False
    assert second["result"]["observer_count"] == 2

    frame = _event("live-1")
    assert server.write_json(frame) is True
    assert primary.frames == [frame]
    assert observer.frames == [frame]


def test_subscribe_is_idempotent_and_primary_observer_is_deduplicated():
    primary = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}

    _subscribe("live-1", primary, "sub-1")
    repeated = _subscribe("live-1", primary, "sub-2")
    frame = _event("live-1")
    server.write_json(frame)

    assert repeated["result"]["already_subscribed"] is True
    assert repeated["result"]["observer_count"] == 1
    assert primary.frames == [frame]


def test_failed_observer_does_not_block_healthy_peers():
    primary = FakeTransport()
    failed = FakeTransport(fail=True)
    healthy = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}
    _subscribe("live-1", failed, "sub-1")
    _subscribe("live-1", healthy, "sub-2")

    frame = _event("live-1")
    assert server.write_json(frame) is True
    assert primary.frames == [frame]
    assert healthy.frames == [frame]


def test_disconnect_cleanup_removes_every_observer_membership():
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}
    server._sessions["live-2"] = {"transport": primary}
    _subscribe("live-1", observer, "sub-1")
    _subscribe("live-2", observer, "sub-2")

    assert server.unregister_session_observer_transport(observer) == 2
    assert server._session_observers == {}

    server.write_json(_event("live-1"))
    assert observer.frames == []


def test_primary_disconnect_promotes_a_surviving_observer():
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {
        "transport": primary,
        "close_on_disconnect": False,
    }
    _subscribe("live-1", observer, "sub-1")

    server.unregister_session_observer_transport(primary)
    assert server._close_sessions_for_transport(primary) == (0, 0)

    frame = _event("live-1")
    server.write_json(frame)
    assert server._sessions["live-1"]["transport"] is observer
    assert observer.frames == [frame]


def test_disconnect_subscribe_reaper_interleaving_keeps_new_observer(monkeypatch):
    """A subscribe landing during disconnect must defeat the queued reaper."""
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {
        "transport": primary,
        "close_on_disconnect": False,
        "running": False,
    }
    schedule_entered = threading.Barrier(2)
    subscribe_finished = threading.Barrier(2)

    def blocked_schedule(session_id: str) -> None:
        assert session_id == "live-1"
        schedule_entered.wait(timeout=2)
        subscribe_finished.wait(timeout=2)

    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", blocked_schedule)
    disconnect = threading.Thread(
        target=server._close_sessions_for_transport,
        args=(primary,),
        daemon=True,
    )
    disconnect.start()
    schedule_entered.wait(timeout=2)

    subscribed = _subscribe("live-1", observer, "sub-race")
    subscribe_finished.wait(timeout=2)
    disconnect.join(timeout=2)

    assert not disconnect.is_alive()
    assert subscribed["result"]["subscribed"] is True
    assert server._sessions["live-1"]["transport"] is observer
    assert server._close_session_by_id(
        "live-1",
        end_reason="test_reaper",
        predicate=server._ws_session_is_orphaned,
    ) is False
    assert "live-1" in server._sessions


def test_unsubscribe_is_idempotent_and_stops_delivery():
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}
    _subscribe("live-1", observer, "sub-1")

    first = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "unsub-1",
            "method": "session.unsubscribe",
            "params": {"session_id": "live-1"},
        },
        observer,
    )
    repeated = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "unsub-2",
            "method": "session.unsubscribe",
            "params": {"session_id": "live-1"},
        },
        observer,
    )

    assert first["result"]["was_subscribed"] is True
    assert repeated["result"]["was_subscribed"] is False
    server.write_json(_event("live-1"))
    assert observer.frames == []


def test_unknown_session_subscription_is_an_error_without_registry_state():
    response = _subscribe("missing", FakeTransport(), "sub-1")

    assert response["error"]["code"] == 4007
    assert server._session_observers == {}


def test_non_subscriber_session_keeps_singular_delivery_behavior():
    primary = FakeTransport()
    unrelated = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}

    frame = _event("live-1")
    assert server.write_json(frame) is True
    assert primary.frames == [frame]
    assert unrelated.frames == []


def test_session_teardown_drops_observer_registry_state():
    primary = FakeTransport()
    observer = FakeTransport()
    server._sessions["live-1"] = {"transport": primary}
    _subscribe("live-1", observer, "sub-1")

    popped = server._pop_session_by_id("live-1")

    assert popped is not None
    assert "live-1" not in server._session_observers


def test_ttl_and_lru_reapers_keep_session_with_registered_observer(monkeypatch):
    primary = FakeTransport()
    primary._closed = True
    observer = FakeTransport()
    now = 10_000.0
    session = {
        "transport": primary,
        "running": False,
        "created_at": 0.0,
        "last_active": 0.0,
        "lazy": True,
    }
    server._sessions["live-1"] = session
    server._session_observers["live-1"] = {id(observer): observer}
    monkeypatch.setattr(server, "_SESSION_TTL_S", 1.0)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *_: False)

    assert server._session_is_evictable("live-1", session, now) is False
    assert server._session_is_lru_evictable("live-1", session) is False


def test_reapers_keep_disconnect_window_until_observer_promotion(monkeypatch):
    primary = FakeTransport()
    primary._closed = True
    observer = FakeTransport()
    session = {
        "transport": primary,
        "close_on_disconnect": False,
        "running": False,
        "created_at": 0.0,
        "last_active": 0.0,
        "lazy": True,
    }
    server._sessions["live-1"] = session
    server._session_observers["live-1"] = {id(observer): observer}
    monkeypatch.setattr(server, "_SESSION_TTL_S", 1.0)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *_: False)

    assert server._session_is_evictable("live-1", session, 10_000.0) is False
    assert server._session_is_lru_evictable("live-1", session) is False
    assert server._close_sessions_for_transport(primary) == (0, 0)
    assert session["transport"] is observer
    assert server._session_is_evictable("live-1", session, 10_000.0) is False
    assert server._session_is_lru_evictable("live-1", session) is False


def test_explicit_persist_create_is_immediately_resumable(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    transport = FakeTransport()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server, "_schedule_session_cap_enforcement", lambda *_args, **_kwargs: None
    )
    try:
        response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "create-1",
                "method": "session.create",
                "params": {
                    "source": "project_ops",
                    "cwd": str(tmp_path),
                    "title": "Shared topic",
                    "persist": True,
                    "creation_key": "operation-1",
                },
            },
            transport,
        )

        stored_id = response["result"]["stored_session_id"]
        assert db.get_session(stored_id)["source"] == "project_ops"
        assert db.get_session_title(stored_id) == "Shared topic"
    finally:
        db.close()
