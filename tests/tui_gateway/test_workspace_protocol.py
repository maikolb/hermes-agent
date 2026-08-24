"""Contract tests for the public ``hermes serve`` workspace protocol.

The adapter is exercised with in-memory legacy handlers. No agent is built and
no model/provider call is possible in this file.
"""

from __future__ import annotations

import threading

from tui_gateway.workspace_protocol import (
    WorkspaceProtocolAdapter,
    additional_contract_events,
    build_event_params,
    initialize_session_protocol,
)


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": code, "message": message},
    }


class FakeLegacyGateway:
    def __init__(self):
        self.sessions = {}
        self.events = []
        self.prompt_calls = []
        self.redirect_calls = []
        self.interrupt_calls = []
        self.redirect_statuses = []
        self.methods = {
            "prompt.submit": self.prompt_submit,
            "session.close": self.session_close,
            "session.create": self.session_create,
            "session.interrupt": self.session_interrupt,
            "session.redirect": self.session_redirect,
            "session.resume": self.session_resume,
        }
        self.original_create = self.methods["session.create"]
        self.adapter = WorkspaceProtocolAdapter(
            sessions=self.sessions,
            methods=self.methods,
            ok=_ok,
            err=_err,
            emit=self.emit,
        )
        self.adapter.install()

    @staticmethod
    def _session_record(stored_id):
        return {
            "history_lock": threading.Lock(),
            "running": False,
            "session_key": stored_id,
        }

    def emit(self, event, session_id, payload=None):
        self.events.append((event, session_id, payload or {}))

    def session_create(self, rid, params):
        session_id = "live-created"
        self.sessions[session_id] = self._session_record("stored-created")
        return _ok(
            rid,
            {
                "info": {"lazy": True},
                "message_count": 0,
                "messages": [],
                "session_id": session_id,
                "stored_session_id": "stored-created",
            },
        )

    def session_resume(self, rid, params):
        session_id = "live-resumed"
        self.sessions[session_id] = self._session_record(params["session_id"])
        return _ok(
            rid,
            {
                "messages": [],
                "resumed": params["session_id"],
                "session_id": session_id,
                "session_key": params["session_id"],
            },
        )

    def session_close(self, rid, params):
        self.sessions.pop(params["session_id"], None)
        return _ok(rid, {"closed": True})

    def prompt_submit(self, rid, params):
        self.prompt_calls.append(dict(params))
        session = self.sessions[params["session_id"]]
        session["running"] = True
        return _ok(rid, {"status": "streaming"})

    def session_redirect(self, rid, params):
        self.redirect_calls.append(dict(params))
        status = self.redirect_statuses.pop(0) if self.redirect_statuses else "redirected"
        return _ok(rid, {"status": status, "text": params["text"]})

    def session_interrupt(self, rid, params):
        self.interrupt_calls.append(dict(params))
        return _ok(rid, {"status": "interrupted"})


def _created_gateway():
    gateway = FakeLegacyGateway()
    create = gateway.methods["session.create"]("create-1", {"source": "portal"})
    return gateway, create["result"]["session_id"]


def _start(gateway, session_id, *, rid="start-1", key="idem-1", turn_id="turn-1"):
    return gateway.methods["turn.start"](
        rid,
        {
            "attachments": [],
            "idempotency_key": key,
            "session_id": session_id,
            "text": "do the work",
            "turn_id": turn_id,
        },
    )


def test_session_create_keeps_legacy_result_and_emits_ready():
    gateway = FakeLegacyGateway()
    expected = gateway.original_create("legacy", {"source": "portal"})
    gateway.sessions.clear()

    actual = gateway.methods["session.create"]("legacy", {"source": "portal"})

    assert actual == expected
    assert gateway.events == [
        ("session.ready", "live-created", {"stored_session_id": "stored-created"})
    ]


def test_workspace_capabilities_freezes_public_v1_surface():
    gateway = FakeLegacyGateway()

    result = gateway.methods["workspace.capabilities"]("caps-1", {})["result"]

    assert result["contract"] == "hermes.workspace"
    assert result["contract_version"] == "1.0"
    assert "turn.redirect" in result["methods"]
    assert "todo.snapshot" in result["events"]


def test_session_resume_adds_stable_identity_without_building_provider():
    gateway = FakeLegacyGateway()

    response = gateway.methods["session.resume"](
        "resume-1",
        {
            "cwd": "/srv/workspace",
            "profile": "team",
            "session_id": "stored-7",
            "source": "workspace-portal",
        },
    )

    assert response["result"]["session_id"] == "live-resumed"
    assert response["result"]["stored_session_id"] == "stored-7"
    assert response["result"]["state"] == "ready"
    assert gateway.events[-1] == (
        "session.ready",
        "live-resumed",
        {"stored_session_id": "stored-7", "resumed": True},
    )


def test_turn_start_is_idempotent_and_rejects_key_reuse_with_new_payload():
    gateway, session_id = _created_gateway()

    first = _start(gateway, session_id)
    replay = _start(gateway, session_id, rid="start-2")
    conflict = gateway.methods["turn.start"](
        "start-3",
        {
            "attachments": [],
            "idempotency_key": "idem-1",
            "session_id": session_id,
            "text": "different work",
            "turn_id": "turn-1",
        },
    )

    assert first["result"] == {
        "acknowledged_idempotency_key": "idem-1",
        "session_id": session_id,
        "status": "running",
        "turn_id": "turn-1",
    }
    assert replay["result"]["replayed"] is True
    assert len(gateway.prompt_calls) == 1
    assert conflict["error"]["code"] == 4091


def test_redirects_append_in_sequence_and_deduplicate_by_message_id():
    gateway, session_id = _created_gateway()
    _start(gateway, session_id)

    first = gateway.methods["turn.redirect"](
        "redirect-1",
        {
            "message_id": "message-10",
            "sequence": 10,
            "session_id": session_id,
            "text": "first correction",
            "turn_id": "turn-1",
        },
    )
    second = gateway.methods["turn.redirect"](
        "redirect-2",
        {
            "message_id": "message-11",
            "sequence": 11,
            "session_id": session_id,
            "text": "second correction",
            "turn_id": "turn-1",
        },
    )
    replay = gateway.methods["turn.redirect"](
        "redirect-3",
        {
            "message_id": "message-10",
            "sequence": 10,
            "session_id": session_id,
            "text": "first correction",
            "turn_id": "turn-1",
        },
    )
    info = gateway.methods["turn.info"](
        "info-1", {"session_id": session_id, "turn_id": "turn-1"}
    )

    assert first["result"]["disposition"] == "redirected"
    assert second["result"]["disposition"] == "redirected"
    assert replay["result"]["replayed"] is True
    assert [item["sequence"] for item in info["result"]["redirects"]] == [10, 11]
    assert [item["text"] for item in info["result"]["redirects"]] == [
        "first correction",
        "second correction",
    ]
    assert len(gateway.redirect_calls) == 2


def test_redirect_rejects_out_of_order_sequence_without_overwrite():
    gateway, session_id = _created_gateway()
    _start(gateway, session_id)
    gateway.methods["turn.redirect"](
        "redirect-1",
        {
            "message_id": "message-20",
            "sequence": 20,
            "session_id": session_id,
            "text": "accepted",
            "turn_id": "turn-1",
        },
    )

    response = gateway.methods["turn.redirect"](
        "redirect-2",
        {
            "message_id": "message-19",
            "sequence": 19,
            "session_id": session_id,
            "text": "late",
            "turn_id": "turn-1",
        },
    )
    info = gateway.methods["turn.info"](
        "info-1", {"session_id": session_id, "turn_id": "turn-1"}
    )

    assert response["error"]["code"] == 4094
    assert [item["sequence"] for item in info["result"]["redirects"]] == [20]


def test_redirect_completion_race_starts_deterministic_successor_turn():
    gateway, session_id = _created_gateway()
    _start(gateway, session_id)
    gateway.redirect_statuses.append("rejected")

    response = gateway.methods["turn.redirect"](
        "redirect-race",
        {
            "message_id": "race-message",
            "sequence": 2,
            "session_id": session_id,
            "text": "continue after race",
            "turn_id": "turn-1",
        },
    )
    info = gateway.methods["turn.info"]("info-current", {"session_id": session_id})

    assert response["result"]["disposition"] == "queued_after_race"
    assert response["result"]["next_turn_id"]
    assert info["result"]["turn_id"] == response["result"]["next_turn_id"]
    assert info["result"]["status"] == "running"
    assert len(gateway.prompt_calls) == 2


def test_turn_cancel_is_scoped_idempotent_and_visible_through_info():
    gateway, session_id = _created_gateway()
    _start(gateway, session_id)

    cancelling = gateway.methods["turn.cancel"](
        "cancel-1",
        {"reason": "user requested", "session_id": session_id, "turn_id": "turn-1"},
    )
    repeated = gateway.methods["turn.cancel"](
        "cancel-2",
        {"session_id": session_id, "turn_id": "turn-1"},
    )
    info = gateway.methods["turn.info"](
        "info-1", {"session_id": session_id, "turn_id": "turn-1"}
    )

    assert cancelling["result"]["status"] == "cancelling"
    assert repeated["result"]["status"] == "cancelling"
    assert info["result"]["status"] == "cancelling"
    assert gateway.interrupt_calls == [{"session_id": session_id}]


def test_event_envelope_is_ordered_and_tracks_terminal_turn_state():
    session = FakeLegacyGateway._session_record("stored")
    initialize_session_protocol(session)
    state = session["_workspace_protocol"]
    state["active_turn_id"] = "turn-1"
    state["turns"]["turn-1"] = {
        "redirects": [],
        "session_id": "live",
        "status": "running",
        "turn_id": "turn-1",
    }

    delta = build_event_params("message.delta", "live", {"text": "a"}, session)
    completed = build_event_params(
        "turn.completed", "live", {"status": "complete", "turn_id": "turn-1"}, session
    )

    assert delta["type"] == "message.delta"
    assert delta["event"] == "message.delta"
    assert delta["event_id"] == "live:1"
    assert delta["turn_id"] == "turn-1"
    assert completed["sequence"] == 2
    assert state["turns"]["turn-1"]["status"] == "completed"
    assert state["active_turn_id"] is None


def test_event_aliases_and_companion_snapshots_are_explicit():
    session = FakeLegacyGateway._session_record("stored")

    legacy = build_event_params("tool.start", "live", {"name": "todo"}, session)
    failure = build_event_params("error", "live", {"message": "boom"}, session)
    terminal = additional_contract_events("message.complete", {"status": "error"})
    todo = additional_contract_events(
        "tool.complete",
        {"name": "todo", "todos": [{"content": "test", "status": "pending"}]},
    )

    assert legacy["type"] == "tool.start"
    assert legacy["event"] == "tool.started"
    assert failure["type"] == "error"
    assert failure["event"] == "turn.failed"
    assert terminal == [("turn.failed", {"status": "error"})]
    assert todo == [
        (
            "todo.snapshot",
            {"items": [{"content": "test", "status": "pending"}], "tool_id": ""},
        )
    ]


def test_real_server_registers_the_complete_public_method_surface():
    from tui_gateway import server

    required = {
        "session.create",
        "session.resume",
        "turn.cancel",
        "turn.info",
        "turn.redirect",
        "turn.start",
        "workspace.capabilities",
    }

    assert required <= set(server._methods)
