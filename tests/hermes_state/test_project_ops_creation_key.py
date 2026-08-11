from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from tui_gateway import server


class FakeTransport:
    user_id = "local-owner"
    provider = "local"
    display_name = "Owner"

    def write(self, _frame: dict) -> bool:
        return True


@pytest.fixture(autouse=True)
def clean_live_sessions():
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()
    yield
    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()


def _create(tmp_path, creation_key: str, *, profile: str | None = None) -> dict:
    params = {
        "source": "project_ops",
        "cwd": str(tmp_path),
        "title": "Recovered topic",
        "persist": True,
        "creation_key": creation_key,
    }
    if profile is not None:
        params["profile"] = profile
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": creation_key,
            "method": "session.create",
            "params": params,
        },
        FakeTransport(),
    )
    assert response is not None
    return response


def test_project_ops_creation_key_resolves_same_session_after_restart(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db_holder = {"db": SessionDB(db_path=db_path)}
    monkeypatch.setattr(server, "_get_db", lambda: db_holder["db"])
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server, "_schedule_session_cap_enforcement", lambda *_args, **_kwargs: None
    )
    first = _create(tmp_path, "operation-ambiguous-1")
    first_stored = first["result"]["stored_session_id"]
    assert db_holder["db"].get_session(first_stored) is not None

    with server._sessions_lock:
        server._sessions.clear()
        server._session_observers.clear()
    db_holder["db"].close()
    db_holder["db"] = SessionDB(db_path=db_path)

    second = _create(tmp_path, "operation-ambiguous-1")

    assert second["result"]["stored_session_id"] == first_stored
    assert db_holder["db"].get_session(first_stored) is not None
    db_holder["db"].close()


def test_project_ops_creation_key_reuses_live_runtime_and_one_build(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    builds: list[str] = []
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid, *_args, **_kwargs: builds.append(sid))
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    try:
        first = _create(tmp_path, "operation-live-retry")
        second = _create(tmp_path, "operation-live-retry")

        assert second["result"]["session_id"] == first["result"]["session_id"]
        assert second["result"]["stored_session_id"] == first["result"]["stored_session_id"]
        assert list(server._sessions) == [first["result"]["session_id"]]
        assert builds == [first["result"]["session_id"]]
    finally:
        db.close()


def test_project_ops_creation_key_concurrent_create_has_one_runtime_and_build(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    builds: list[str] = []
    barrier = threading.Barrier(2)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid, *_args, **_kwargs: builds.append(sid))
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    def create() -> dict:
        barrier.wait(timeout=2)
        return _create(tmp_path, "operation-concurrent")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _index: create(), range(2)))

        session_ids = {response["result"]["session_id"] for response in responses}
        assert len(session_ids) == 1
        assert set(server._sessions) == session_ids
        assert builds == [next(iter(session_ids))]
    finally:
        db.close()


def test_same_creation_key_is_scoped_by_canonical_profile_home(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    launch_home = tmp_path / ".hermes"
    coder_home = launch_home / "profiles" / "coder"
    launch_home.mkdir()
    coder_home.mkdir(parents=True)
    launch_db = SessionDB(db_path=launch_home / "state.db")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    monkeypatch.setattr(server, "_get_db", lambda: launch_db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    try:
        launch = _create(tmp_path, "operation-profile-scope")
        coder = _create(tmp_path, "operation-profile-scope", profile="coder")

        assert "result" in coder, coder
        assert coder["result"]["stored_session_id"] == launch["result"]["stored_session_id"]
        assert coder["result"]["session_id"] != launch["result"]["session_id"]
        assert len(server._sessions) == 2
        assert server._sessions[launch["result"]["session_id"]]["profile_home"] is None
        assert Path(server._sessions[coder["result"]["session_id"]]["profile_home"]).resolve() == coder_home.resolve()
    finally:
        launch_db.close()


def test_current_profile_alias_creates_and_resumes_in_launch_state_db(
    tmp_path, monkeypatch
):
    from hermes_state import SessionDB

    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    launch_db = SessionDB(db_path=launch_home / "state.db")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    monkeypatch.setattr(server, "_get_db", lambda: launch_db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    try:
        created = _create(
            tmp_path,
            "operation-current-profile",
            profile="current",
        )

        assert "result" in created, created
        stored_session_id = created["result"]["stored_session_id"]
        assert launch_db.get_session(stored_session_id) is not None
        assert created["result"]["info"]["profile_name"] == "default"
        assert server._sessions[created["result"]["session_id"]]["profile_home"] is None

        with server._sessions_lock:
            server._sessions.clear()
            server._session_observers.clear()

        token = server.bind_transport(FakeTransport())
        try:
            resumed = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": "resume-current-profile",
                    "method": "session.resume",
                    "params": {
                        "session_id": stored_session_id,
                        "profile": "current",
                    },
                }
            )
        finally:
            server.reset_transport(token)

        assert "result" in resumed, resumed
        assert resumed["result"]["resumed"] == stored_session_id
        assert resumed["result"]["info"]["profile_name"] == "default"
        assert server._sessions[resumed["result"]["session_id"]]["profile_home"] is None
        assert launch_db.get_session(stored_session_id) is not None
        assert not (launch_home / "profiles" / "current").exists()
    finally:
        launch_db.close()


@pytest.mark.parametrize(
    ("requested_profile", "expected_code"),
    [("", 4006), ("missing", 4007)],
)
def test_explicit_invalid_profile_fails_without_runtime_or_launch_db_row(
    tmp_path, monkeypatch, requested_profile, expected_code
):
    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: (_ for _ in ()).throw(AssertionError("launch state.db must not open")),
    )

    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "invalid-profile",
            "method": "session.create",
            "params": {
                "source": "project_ops",
                "persist": True,
                "creation_key": "invalid-profile-operation",
                "profile": requested_profile,
            },
        },
        FakeTransport(),
    )

    assert response["error"]["code"] == expected_code
    assert server._sessions == {}
    assert not (launch_home / "state.db").exists()


def test_resume_with_explicit_missing_profile_fails_before_launch_db(
    tmp_path, monkeypatch
):
    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: (_ for _ in ()).throw(AssertionError("launch state.db must not open")),
    )
    token = server.bind_transport(FakeTransport())
    try:
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "invalid-resume-profile",
                "method": "session.resume",
                "params": {
                    "session_id": "stored-session",
                    "profile": "missing",
                },
            }
        )
    finally:
        server.reset_transport(token)

    assert response["error"]["code"] == 4007
    assert server._sessions == {}
    assert not (launch_home / "state.db").exists()
