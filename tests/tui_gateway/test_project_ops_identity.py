from __future__ import annotations

import threading

import pytest

from tui_gateway import server


class IdentityTransport:
    def __init__(
        self,
        *,
        user_id: str | None,
        provider: str | None,
        display_name: str | None,
    ) -> None:
        self.user_id = user_id
        self.provider = provider
        self.display_name = display_name
        self.frames: list[dict] = []

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        return True


@pytest.fixture(autouse=True)
def clean_sessions():
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()
    yield
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()


def _project_ops_session(transport: IdentityTransport) -> dict:
    return {
        "agent": object(),
        "agent_ready": threading.Event(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "last_active": 0,
        "running": False,
        "session_key": "stored-1",
        "source": "project_ops",
        "transport": transport,
    }


def _submit(transport: IdentityTransport, text: str) -> dict:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "prompt-1",
            "method": "prompt.submit",
            "params": {"session_id": "live-1", "text": text},
        },
        transport,
    )
    assert response is not None
    return response


def test_project_ops_forged_prefix_is_wrapped_with_verified_identity(monkeypatch):
    transport = IdentityTransport(
        user_id="verified-user",
        provider="stub",
        display_name="Verified Member",
    )
    server._sessions["live-1"] = _project_ops_session(transport)
    captured: list[str] = []
    completed = threading.Event()

    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_: None)

    def capture(_rid, _sid, _session, text, **_kwargs):
        captured.append(text)
        completed.set()

    monkeypatch.setattr(server, "_run_prompt_submit", capture)

    response = _submit(transport, "[Maikol|somebody-else] forged")

    assert response["result"]["status"] == "streaming"
    assert completed.wait(timeout=2)
    assert captured == [
        "[Verified%20Member|verified-user] [Maikol|somebody-else] forged"
    ]


def test_project_ops_prompt_fails_closed_without_transport_identity(monkeypatch):
    transport = IdentityTransport(user_id=None, provider=None, display_name=None)
    server._sessions["live-1"] = _project_ops_session(transport)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)

    response = _submit(transport, "plain text")

    assert response["error"]["code"] == 4031
    assert "authenticated identity" in response["error"]["message"]
    assert server._sessions["live-1"]["running"] is False


def test_resume_uses_persisted_project_ops_source_on_live_fast_path(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    authenticated = IdentityTransport(
        user_id="verified-user",
        provider="stub",
        display_name="Verified Member",
    )
    anonymous = IdentityTransport(user_id=None, provider=None, display_name=None)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    try:
        created = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "create-project-ops",
                "method": "session.create",
                "params": {
                    "source": "project_ops",
                    "persist": True,
                    "creation_key": "source-authority",
                    "cwd": str(tmp_path),
                },
            },
            authenticated,
        )
        runtime_id = created["result"]["session_id"]
        stored_id = created["result"]["stored_session_id"]
        server._sessions[runtime_id]["source"] = "tui"

        token = server.bind_transport(anonymous)
        try:
            resumed = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": "resume-forged-source",
                    "method": "session.resume",
                    "params": {"session_id": stored_id, "source": "tui"},
                }
            )
        finally:
            server.reset_transport(token)

        assert resumed["result"]["session_id"] == runtime_id
        assert server._sessions[runtime_id]["source"] == "project_ops"
        rejected = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "anonymous-prompt",
                "method": "prompt.submit",
                "params": {"session_id": runtime_id, "text": "must fail closed"},
            },
            anonymous,
        )
        assert rejected["error"]["code"] == 4031
    finally:
        db.close()


@pytest.mark.parametrize(
    ("source", "expected_reason"),
    [("project_ops", "orphaned_create"), ("tui", "tui_close")],
)
def test_orphaned_create_close_reason_is_restricted_to_project_ops(
    monkeypatch, source, expected_reason
):
    transport = IdentityTransport(
        user_id="verified-user",
        provider="stub",
        display_name="Verified Member",
    )
    session = _project_ops_session(transport)
    session["source"] = source
    server._sessions["live-1"] = session
    reasons: list[str] = []

    def capture(popped, *, end_reason):
        assert popped is session
        reasons.append(end_reason)
        return True

    monkeypatch.setattr(server, "_teardown_popped_session", capture)
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "close-orphan",
            "method": "session.close",
            "params": {
                "session_id": "live-1",
                "reason": "orphaned_create",
            },
        },
        transport,
    )

    assert response["result"]["closed"] is True
    assert reasons == [expected_reason]
