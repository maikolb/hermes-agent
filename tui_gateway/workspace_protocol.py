"""Public ``hermes serve`` workspace protocol adapter.

The dashboard/TUI protocol predates the Workspace Portal and intentionally
uses UI-oriented method names (``prompt.submit``, ``session.redirect`` and
``session.interrupt``).  This module adds the stable turn-oriented vocabulary
without moving conversation ownership into the WebSocket transport.  All live
state remains attached to the existing session record; this adapter only
validates, serializes and delegates to the established session/agent paths.

The adapter is deliberately provider-agnostic.  Its tests use in-memory
handlers and no method in this module calls a model provider directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Callable, MutableMapping
import uuid


_STATE_KEY = "_workspace_protocol"
_LOCK_KEY = "_workspace_protocol_lock"
_STATE_INIT_LOCK = threading.Lock()

_EVENT_ALIASES = {
    "error": "turn.failed",
    "message.start": "turn.started",
    "message.complete": "message.completed",
    "session.info": "agent.status",
    "status.update": "agent.status",
    "tool.start": "tool.started",
    "tool.complete": "tool.completed",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_and_lock(session: MutableMapping[str, Any]) -> tuple[dict, threading.RLock]:
    """Return the session-owned protocol state and its dedicated lock."""
    lock = session.get(_LOCK_KEY)
    state = session.get(_STATE_KEY)
    if isinstance(lock, type(threading.RLock())) and isinstance(state, dict):
        return state, lock

    # Session dictionaries can be reached concurrently by the WS reader,
    # model-run thread and completion callbacks.  Initialize the two fields as
    # one unit without borrowing history_lock (many emitters already hold it).
    with _STATE_INIT_LOCK:
        lock = session.get(_LOCK_KEY)
        if not hasattr(lock, "acquire") or not hasattr(lock, "release"):
            lock = threading.RLock()
            session[_LOCK_KEY] = lock
        state = session.get(_STATE_KEY)
        if not isinstance(state, dict):
            state = {
                "active_turn_id": None,
                "event_sequence": 0,
                "idempotency": {},
                "queued_turn_id": None,
                "redirects": {},
                "turns": {},
            }
            session[_STATE_KEY] = state
    return state, lock


def initialize_session_protocol(session: MutableMapping[str, Any]) -> None:
    """Idempotently attach public-protocol state to a live session."""
    _state_and_lock(session)


def _turn_snapshot_locked(state: dict, session: MutableMapping[str, Any]) -> dict:
    turn_id = state.get("active_turn_id")
    turn = dict((state.get("turns") or {}).get(turn_id) or {}) if turn_id else {}
    if turn:
        status = str(turn.get("status") or "idle")
        if session.get("running") and status not in {"cancelling", "cancelled"}:
            status = "running"
        turn["status"] = status
    return {
        "session_id": str(turn.get("session_id") or ""),
        "turn_id": turn_id,
        "status": str(turn.get("status") or "idle"),
        "queued_turn_id": state.get("queued_turn_id"),
        "redirects": [dict(item) for item in turn.get("redirects", [])],
        "started_at": turn.get("started_at"),
        "updated_at": turn.get("updated_at"),
    }


def turn_snapshot(session: MutableMapping[str, Any], session_id: str) -> dict:
    state, lock = _state_and_lock(session)
    with lock:
        snapshot = _turn_snapshot_locked(state, session)
        snapshot["session_id"] = session_id
        return snapshot


def _normalized_event_name(event: str) -> str:
    return _EVENT_ALIASES.get(event, event)


def _terminal_event_name(payload: dict | None) -> str:
    status = str((payload or {}).get("status") or "complete").lower()
    if status in {"interrupted", "cancelled", "canceled"}:
        return "turn.cancelled"
    if status in {"error", "failed", "failure"}:
        return "turn.failed"
    return "turn.completed"


def additional_contract_events(event: str, payload: dict | None) -> list[tuple[str, dict]]:
    """Return normalized companion events required by the public contract."""
    if event == "message.complete":
        return [(_terminal_event_name(payload), dict(payload or {}))]
    if event == "tool.complete" and str((payload or {}).get("name") or "") == "todo":
        todos = (payload or {}).get("todos")
        if isinstance(todos, list):
            return [
                (
                    "todo.snapshot",
                    {
                        "items": todos,
                        "tool_id": str((payload or {}).get("tool_id") or ""),
                    },
                )
            ]
    return []


def build_event_params(
    event: str,
    session_id: str,
    payload: dict | None,
    session: MutableMapping[str, Any] | None,
) -> dict:
    """Build a resumable normalized event envelope inside JSON-RPC params.

    ``type`` remains the original dashboard event for backward compatibility;
    new clients consume ``event`` and the ordered envelope fields.
    """
    normalized = _normalized_event_name(event)
    turn_id = None
    sequence = 1
    if session is not None:
        state, lock = _state_and_lock(session)
        with lock:
            state["event_sequence"] = int(state.get("event_sequence") or 0) + 1
            sequence = state["event_sequence"]
            payload_turn_id = str((payload or {}).get("turn_id") or "").strip()
            turn_id = payload_turn_id or state.get("active_turn_id")
            turn = (state.get("turns") or {}).get(turn_id) if turn_id else None
            if isinstance(turn, dict):
                now = _utc_now()
                if normalized == "turn.started":
                    turn["status"] = "running"
                elif normalized == "turn.redirected":
                    turn["status"] = "running"
                elif normalized == "turn.cancelled":
                    turn["status"] = "cancelled"
                elif normalized == "turn.failed":
                    turn["status"] = "failed"
                elif normalized == "turn.completed":
                    turn["status"] = "completed"
                turn["updated_at"] = now

                if normalized in {"turn.cancelled", "turn.failed", "turn.completed"}:
                    queued_turn_id = state.get("queued_turn_id")
                    if queued_turn_id:
                        state["active_turn_id"] = queued_turn_id
                        state["queued_turn_id"] = None
                    else:
                        state["active_turn_id"] = None

    occurred_at = _utc_now()
    params: dict[str, Any] = {
        "event_id": f"{session_id or 'global'}:{sequence}",
        "event": normalized,
        "occurred_at": occurred_at,
        "sequence": sequence,
        "session_id": session_id,
        "type": event,
    }
    if payload is not None:
        params["payload"] = dict(payload)
    if turn_id:
        params["turn_id"] = turn_id
    return params


class WorkspaceProtocolAdapter:
    """Install stable session/turn methods over the legacy gateway methods."""

    def __init__(
        self,
        *,
        sessions: MutableMapping[str, MutableMapping[str, Any]],
        methods: MutableMapping[str, Callable[[Any, dict], dict]],
        ok: Callable[[Any, dict], dict],
        err: Callable[[Any, int, str], dict],
        emit: Callable[[str, str, dict | None], None],
    ) -> None:
        self._sessions = sessions
        self._methods = methods
        self._ok = ok
        self._err = err
        self._emit = emit
        self._legacy: dict[str, Callable[[Any, dict], dict]] = {}
        self._installed = False

    def install(self) -> None:
        """Register the protocol once, preserving existing method behavior."""
        if self._installed:
            return
        required = (
            "prompt.submit",
            "session.create",
            "session.interrupt",
            "session.redirect",
            "session.resume",
        )
        missing = [name for name in required if name not in self._methods]
        if missing:
            raise RuntimeError(f"workspace protocol missing legacy methods: {', '.join(missing)}")
        self._legacy = {name: self._methods[name] for name in required}
        if "session.close" in self._methods:
            self._legacy["session.close"] = self._methods["session.close"]

        self._methods["session.create"] = self.session_create
        self._methods["session.resume"] = self.session_resume
        if "session.close" in self._legacy:
            self._methods["session.close"] = self.session_close
        self._methods["turn.start"] = self.turn_start
        self._methods["turn.redirect"] = self.turn_redirect
        self._methods["turn.cancel"] = self.turn_cancel
        self._methods["turn.info"] = self.turn_info
        self._methods["workspace.capabilities"] = self.workspace_capabilities
        self._installed = True

    def workspace_capabilities(self, rid: Any, params: dict) -> dict:
        return self._ok(
            rid,
            {
                "contract": "hermes.workspace",
                "contract_version": "1.0",
                "events": [
                    "session.ready", "session.closed", "turn.started", "turn.redirected",
                    "turn.cancelled", "turn.completed", "turn.failed", "reasoning.delta",
                    "message.delta", "message.completed", "tool.started", "tool.completed",
                    "agent.status", "todo.snapshot",
                ],
                "methods": [
                    "session.create", "session.resume", "turn.start", "turn.redirect",
                    "turn.cancel", "turn.info", "workspace.capabilities",
                ],
            },
        )

    def _session(self, rid: Any, session_id: str) -> tuple[MutableMapping[str, Any] | None, dict | None]:
        session = self._sessions.get(session_id)
        if session is None:
            return None, self._err(rid, 4001, "session not found")
        initialize_session_protocol(session)
        return session, None

    @staticmethod
    def _result(response: dict) -> dict | None:
        result = response.get("result") if isinstance(response, dict) else None
        return result if isinstance(result, dict) else None

    def session_create(self, rid: Any, params: dict) -> dict:
        response = self._legacy["session.create"](rid, params)
        result = self._result(response)
        if result is not None:
            session_id = str(result.get("session_id") or "")
            session = self._sessions.get(session_id)
            if session is not None:
                initialize_session_protocol(session)
                self._emit(
                    "session.ready",
                    session_id,
                    {"stored_session_id": str(result.get("stored_session_id") or "")},
                )
        # Exact legacy result is returned: session.create remains compatible.
        return response

    def session_resume(self, rid: Any, params: dict) -> dict:
        response = self._legacy["session.resume"](rid, params)
        result = self._result(response)
        if result is None:
            return response
        session_id = str(result.get("session_id") or "")
        session = self._sessions.get(session_id)
        if session is None:
            return response
        initialize_session_protocol(session)
        result.setdefault(
            "stored_session_id",
            str(result.get("session_key") or result.get("resumed") or ""),
        )
        result.setdefault("state", "ready")
        self._emit(
            "session.ready",
            session_id,
            {"stored_session_id": result["stored_session_id"], "resumed": True},
        )
        return response

    def session_close(self, rid: Any, params: dict) -> dict:
        session_id = str(params.get("session_id") or "")
        response = self._legacy["session.close"](rid, params)
        if "error" not in response:
            self._emit("session.closed", session_id, {})
        return response

    def _attach(self, rid: Any, session_id: str, attachments: list[Any]) -> dict | None:
        """Stage supported public attachment shapes through existing handlers."""
        for index, attachment in enumerate(attachments):
            attach_rid = f"{rid}:attachment:{index}"
            if isinstance(attachment, str):
                method_name = "image.attach"
                attachment_params = {"path": attachment}
            elif isinstance(attachment, dict) and attachment.get("path"):
                method_name = "image.attach"
                attachment_params = {"path": attachment.get("path")}
            elif isinstance(attachment, dict) and (
                attachment.get("data") or attachment.get("base64")
            ):
                method_name = "image.attach_bytes"
                attachment_params = dict(attachment)
                if "data" not in attachment_params:
                    attachment_params["data"] = attachment_params.pop("base64")
            else:
                return self._err(rid, 4002, f"unsupported attachment at index {index}")
            handler = self._methods.get(method_name)
            if handler is None:
                return self._err(rid, 4010, f"{method_name} is unavailable")
            attachment_params["session_id"] = session_id
            response = handler(attach_rid, attachment_params)
            if "error" in response:
                return response
        return None

    def turn_start(self, rid: Any, params: dict) -> dict:
        session_id = str(params.get("session_id") or "").strip()
        turn_id = str(params.get("turn_id") or "").strip()
        text = params.get("text")
        idempotency_key = str(params.get("idempotency_key") or "").strip()
        attachments = params.get("attachments") or []
        if not session_id:
            return self._err(rid, 4006, "session_id required")
        if not turn_id:
            return self._err(rid, 4002, "turn_id required")
        if not isinstance(text, str) or not text.strip():
            return self._err(rid, 4002, "text is required")
        if not idempotency_key:
            return self._err(rid, 4002, "idempotency_key required")
        if not isinstance(attachments, list):
            return self._err(rid, 4002, "attachments must be an array")
        session, error = self._session(rid, session_id)
        if error is not None or session is None:
            return error  # type: ignore[return-value]

        fingerprint = _fingerprint(
            {"attachments": attachments, "text": text, "turn_id": turn_id}
        )
        state, lock = _state_and_lock(session)
        with lock:
            prior = (state.get("idempotency") or {}).get(idempotency_key)
            if isinstance(prior, dict):
                if prior.get("fingerprint") != fingerprint:
                    return self._err(rid, 4091, "idempotency key reused with different turn payload")
                cached = prior.get("response")
                if isinstance(cached, dict):
                    replay = dict(cached)
                    replay_turn = (state.get("turns") or {}).get(turn_id)
                    if isinstance(replay_turn, dict):
                        replay["status"] = str(replay_turn.get("status") or replay.get("status") or "idle")
                    replay["replayed"] = True
                    return self._ok(rid, replay)
                replay = _turn_snapshot_locked(state, session)
                replay.update(
                    {
                        "acknowledged_idempotency_key": idempotency_key,
                        "replayed": True,
                        "turn_id": turn_id,
                    }
                )
                return self._ok(rid, replay)
            if session.get("running") or state.get("active_turn_id"):
                return self._err(rid, 4092, "session already has an active turn")
            now = _utc_now()
            turn = {
                "idempotency_key": idempotency_key,
                "redirects": [],
                "session_id": session_id,
                "started_at": now,
                "status": "starting",
                "turn_id": turn_id,
                "updated_at": now,
            }
            state.setdefault("turns", {})[turn_id] = turn
            state["active_turn_id"] = turn_id
            state.setdefault("idempotency", {})[idempotency_key] = {
                "fingerprint": fingerprint,
                "response": None,
                "turn_id": turn_id,
            }

        attach_error = self._attach(rid, session_id, attachments)
        if attach_error is not None:
            with lock:
                turn["status"] = "failed"
                state["active_turn_id"] = None
                state["idempotency"][idempotency_key]["response"] = {
                    "acknowledged_idempotency_key": idempotency_key,
                    "status": "failed",
                    "turn_id": turn_id,
                }
            return attach_error

        response = self._legacy["prompt.submit"](
            rid, {"session_id": session_id, "text": text}
        )
        result = self._result(response)
        with lock:
            if result is None:
                turn["status"] = "failed"
                state["active_turn_id"] = None
                public_result = {
                    "acknowledged_idempotency_key": idempotency_key,
                    "session_id": session_id,
                    "status": "failed",
                    "turn_id": turn_id,
                }
            else:
                turn["status"] = "running"
                turn["updated_at"] = _utc_now()
                public_result = {
                    "acknowledged_idempotency_key": idempotency_key,
                    "session_id": session_id,
                    "status": "running",
                    "turn_id": turn_id,
                }
            state["idempotency"][idempotency_key]["response"] = dict(public_result)
        if result is None:
            self._emit(
                "turn.failed",
                session_id,
                {"turn_id": turn_id, "message": str((response.get("error") or {}).get("message") or "turn start failed")},
            )
            return response
        return self._ok(rid, public_result)

    def turn_redirect(self, rid: Any, params: dict) -> dict:
        session_id = str(params.get("session_id") or "").strip()
        turn_id = str(params.get("turn_id") or "").strip()
        message_id = str(params.get("message_id") or "").strip()
        text = str(params.get("text") or "").strip()
        try:
            sequence = int(params.get("sequence"))
        except (TypeError, ValueError):
            return self._err(rid, 4002, "sequence must be an integer")
        if not all((session_id, turn_id, message_id, text)):
            return self._err(rid, 4002, "session_id, turn_id, message_id and text are required")
        session, error = self._session(rid, session_id)
        if error is not None or session is None:
            return error  # type: ignore[return-value]
        fingerprint = _fingerprint({"sequence": sequence, "text": text, "turn_id": turn_id})
        state, lock = _state_and_lock(session)
        with lock:
            prior = state.setdefault("redirects", {}).get(message_id)
            if isinstance(prior, dict):
                if prior.get("fingerprint") != fingerprint:
                    return self._err(rid, 4091, "message_id reused with different redirect payload")
                cached = prior.get("response")
                if isinstance(cached, dict):
                    replay = dict(cached)
                    replay["replayed"] = True
                    return self._ok(rid, replay)
            active_turn_id = state.get("active_turn_id")
            if active_turn_id != turn_id:
                return self._err(rid, 4093, "turn is not active")
            turn = state.setdefault("turns", {}).get(turn_id)
            if not isinstance(turn, dict):
                return self._err(rid, 4093, "turn is not active")
            redirects = turn.setdefault("redirects", [])
            last_sequence = redirects[-1]["sequence"] if redirects else None
            if last_sequence is not None and sequence <= int(last_sequence):
                return self._err(rid, 4094, "redirect sequence must be strictly increasing")
            entry = {
                "message_id": message_id,
                "sequence": sequence,
                "status": "accepted",
                "text": text,
            }
            redirects.append(entry)
            state["redirects"][message_id] = {
                "fingerprint": fingerprint,
                "response": None,
            }

        response = self._legacy["session.redirect"](
            rid, {"session_id": session_id, "text": text}
        )
        result = self._result(response)
        legacy_status = str((result or {}).get("status") or "")
        disposition = "redirected"
        followup_started = False
        next_turn_id: str | None = None

        if legacy_status == "queued":
            disposition = "queued_after_race"
        elif result is None or legacy_status == "rejected":
            # The live response can finish between state validation and
            # redirect(). Delegate the correction to prompt.submit, whose
            # existing claim-under-lock path either starts or queues it.
            response = self._legacy["prompt.submit"](
                rid, {"session_id": session_id, "text": text}
            )
            result = self._result(response)
            if result is None:
                with lock:
                    entry["status"] = "failed"
                return response
            legacy_status = str(result.get("status") or "")
            disposition = (
                "redirected" if legacy_status in {"redirected", "steered"}
                else "queued_after_race"
            )
            followup_started = legacy_status == "streaming"

        if disposition == "queued_after_race":
            next_turn_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes-workspace:{session_id}:{turn_id}:{message_id}",
            ).hex

        with lock:
            entry["status"] = disposition
            entry["updated_at"] = _utc_now()
            public_result: dict[str, Any] = {
                "acknowledged_message_id": message_id,
                "disposition": disposition,
                "sequence": sequence,
                "session_id": session_id,
                "status": "running" if disposition == "redirected" else "queued",
                "turn_id": turn_id,
            }
            if next_turn_id:
                now = _utc_now()
                state.setdefault("turns", {})[next_turn_id] = {
                    "idempotency_key": f"redirect:{message_id}",
                    "redirects": [],
                    "session_id": session_id,
                    "started_at": now,
                    "status": "running" if followup_started else "queued",
                    "turn_id": next_turn_id,
                    "updated_at": now,
                }
                if followup_started:
                    turn["status"] = "completed"
                    state["active_turn_id"] = next_turn_id
                    state["queued_turn_id"] = None
                else:
                    state["queued_turn_id"] = next_turn_id
                public_result["next_turn_id"] = next_turn_id
            state["redirects"][message_id]["response"] = dict(public_result)

        self._emit(
            "turn.redirected",
            session_id,
            {
                "disposition": disposition,
                "message_id": message_id,
                "next_turn_id": next_turn_id,
                "sequence": sequence,
                "text": text,
                "turn_id": turn_id,
            },
        )
        return self._ok(rid, public_result)

    def turn_cancel(self, rid: Any, params: dict) -> dict:
        session_id = str(params.get("session_id") or "").strip()
        turn_id = str(params.get("turn_id") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if not session_id or not turn_id:
            return self._err(rid, 4002, "session_id and turn_id are required")
        session, error = self._session(rid, session_id)
        if error is not None or session is None:
            return error  # type: ignore[return-value]
        state, lock = _state_and_lock(session)
        with lock:
            turn = state.setdefault("turns", {}).get(turn_id)
            if not isinstance(turn, dict):
                return self._err(rid, 4093, "turn is not active")
            status = str(turn.get("status") or "")
            if status in {"cancelling", "cancelled", "completed", "failed"}:
                return self._ok(
                    rid,
                    {
                        "session_id": session_id,
                        "status": status,
                        "turn_id": turn_id,
                    },
                )
            if state.get("active_turn_id") != turn_id:
                return self._err(rid, 4093, "turn is not active")
            turn["status"] = "cancelling"
            turn["updated_at"] = _utc_now()

        response = self._legacy["session.interrupt"](
            rid, {"session_id": session_id}
        )
        if "error" in response:
            with lock:
                turn["status"] = "running"
            return response
        self._emit(
            "agent.status",
            session_id,
            {"reason": reason, "status": "cancelling", "turn_id": turn_id},
        )
        return self._ok(
            rid,
            {"session_id": session_id, "status": "cancelling", "turn_id": turn_id},
        )

    def turn_info(self, rid: Any, params: dict) -> dict:
        session_id = str(params.get("session_id") or "").strip()
        requested_turn_id = str(params.get("turn_id") or "").strip() or None
        if not session_id:
            return self._err(rid, 4006, "session_id required")
        session, error = self._session(rid, session_id)
        if error is not None or session is None:
            return error  # type: ignore[return-value]
        state, lock = _state_and_lock(session)
        with lock:
            if requested_turn_id:
                turn = (state.get("turns") or {}).get(requested_turn_id)
                if not isinstance(turn, dict):
                    return self._err(rid, 4008, "turn not found")
                result = dict(turn)
                result["redirects"] = [dict(item) for item in turn.get("redirects", [])]
                result["active"] = state.get("active_turn_id") == requested_turn_id
                result["queued"] = state.get("queued_turn_id") == requested_turn_id
            else:
                result = _turn_snapshot_locked(state, session)
                result["session_id"] = session_id
        return self._ok(rid, result)
