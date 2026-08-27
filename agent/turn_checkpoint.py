"""Durable operational turn checkpoints for compaction and restart continuity.

The conversation transcript remains the authority for what was said.  This
module owns the small amount of *control-plane* state that cannot be recovered
reliably from a lossy natural-language summary: phase, next action, uncertain
tool outcomes, pending deliverable/verification, and the transcript boundary
around a compaction swap.

Checkpoints use a profile-scoped write-ahead journal.  Every update is written
to a same-directory temporary file, fsynced, atomically replaced, and read back
with a payload checksum.  A compaction journal records both the before and
after transcript hashes before the canonical SessionDB transcript transaction
runs; restart can therefore distinguish "swap did not happen" from "swap
committed before acknowledgement" without guessing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from filelock import FileLock, Timeout as FileLockTimeout

from agent.redact import redact_sensitive_text

SCHEMA_VERSION = 1
_MAX_LITERAL_CHARS = 131_072
_TERMINAL_PHASES = frozenset({"terminal", "delivered", "cancelled"})
_CONTINUATION_ONLY_RESPONSE_RE = re.compile(
    r"(?i)(?:"
    r"\bretom(?:ado|ada|ei|amos)\b|\bcontinuidade\s+ativa\b|"
    r"\breativad[oa]\b|\b(?:est[aá]|est[aã]o)\s+(?:em\s+execu[cç][aã]o|sendo\s+)\b|"
    r"\b(?:vou|iremos|seguirei|continuarei)\s+(?:continuar|prosseguir|retomar)\b|"
    r"\b(?:resumed|continuation\s+active|work\s+is\s+running)\b"
    r")"
)


class CheckpointError(RuntimeError):
    """Base class for checkpoint failures."""


class CheckpointWriteError(CheckpointError):
    """Atomic persistence or read-back failed."""


class CheckpointIntegrityError(CheckpointError):
    """The journal is malformed or its checksum does not match."""


class CheckpointConflictError(CheckpointError):
    """Live transcript matches neither side of a prepared compaction."""


def checkpoint_is_resumable(state: Mapping[str, Any] | None) -> bool:
    """Return whether *state* represents unfinished operational work."""
    return bool(
        isinstance(state, Mapping)
        and state.get("phase") not in _TERMINAL_PHASES
        and str(state.get("next_action") or "").strip().lower() not in {"", "none"}
    )


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_REDACTED_SENTINEL_RE = re.compile(r"«redacted(?::[^»]*)?»")
_CHECKPOINT_SECRET_FIELD_RE = re.compile(
    r"(?im)^(\s*(?:password|passwd|pwd|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|secret|private[_-]?key|authorization)\s*[:=]\s*).*$"
)


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _canonical_delivery_route(
    routing: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Normalize the three fields that identify one outbound destination."""
    if not isinstance(routing, Mapping):
        return None
    raw_platform = routing.get("platform")
    platform_value = getattr(raw_platform, "value", raw_platform)
    platform = str(platform_value or "").strip().casefold()
    if platform.startswith("platform."):
        platform = platform.split(".", 1)[1]
    raw_chat_id = routing.get("chat_id")
    chat_id = "" if raw_chat_id is None else str(raw_chat_id)
    raw_thread_id = routing.get("thread_id")
    thread_id = "" if raw_thread_id is None else str(raw_thread_id)
    if not platform or not chat_id:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _delivery_route_sha256(routing: Mapping[str, Any] | None) -> str | None:
    canonical = _canonical_delivery_route(routing)
    return _sha256_text(_canonical_json(canonical)) if canonical is not None else None


def _redacted_literal(value: Any, *, limit: int = _MAX_LITERAL_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = _canonical_json(value)
    redacted = redact_sensitive_text(
        text,
        force=True,
        file_read=True,
        redact_url_credentials=True,
    )
    redacted = _CHECKPOINT_SECRET_FIELD_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _REDACTED_SENTINEL_RE.sub("[REDACTED]", redacted)
    if len(redacted) > limit:
        omitted = len(redacted) - limit
        redacted = f"{redacted[:limit]}\n...[checkpoint truncated {omitted} chars]"
    return redacted


def _stable_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return arguments
    return arguments


def tool_fingerprint(name: str, arguments: Any) -> str:
    normalized = {
        "name": str(name or ""),
        "arguments": _stable_tool_arguments(arguments),
    }
    return _sha256_text(_canonical_json(normalized))


def _message_projection(message: Any) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        return {"role": "unknown", "content": str(message)}
    projected: dict[str, Any] = {}
    # The live conversation loop emits tool-result identity as ``name`` while
    # SessionDB persists/replays the same value as ``tool_name``.  A checkpoint
    # hash spans that durability boundary, so these are aliases, not two
    # independent fields.  Hash the replay-canonical key and omit an empty name
    # exactly as SessionDB's read path does.
    tool_name = message.get("tool_name") or message.get("name")
    if tool_name:
        projected["tool_name"] = tool_name
    for key in (
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "api_content",
    ):
        if key in message and message.get(key) is not None:
            projected[key] = message.get(key)
    return projected


def transcript_hash(messages: Sequence[Any]) -> str:
    projected = [_message_projection(message) for message in messages]
    return _sha256_text(_canonical_json(projected))


def _payload_checksum(state: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in state.items() if key != "payload_sha256"}
    return _sha256_text(_canonical_json(unsigned))


def _validate_state(state: Any, *, expected_session_id: str | None = None) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise CheckpointIntegrityError("checkpoint payload is not an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise CheckpointIntegrityError(
            f"unsupported checkpoint schema_version={state.get('schema_version')!r}"
        )
    required = (
        "session_id",
        "turn_id",
        "revision",
        "phase",
        "active_user_turn",
        "transcript",
        "compaction",
        "payload_sha256",
    )
    missing = [key for key in required if key not in state]
    if missing:
        raise CheckpointIntegrityError(f"checkpoint missing fields: {', '.join(missing)}")
    if expected_session_id is not None and state.get("session_id") != expected_session_id:
        raise CheckpointIntegrityError("checkpoint session_id does not match requested session")
    expected = _payload_checksum(state)
    if state.get("payload_sha256") != expected:
        raise CheckpointIntegrityError("checkpoint checksum mismatch")
    return state


class TurnCheckpointStore:
    """Profile-scoped atomic write-ahead checkpoint store."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    def delivery_namespace(self) -> dict[str, str]:
        """Return the exact storage namespace owned by this store.

        Delivery acknowledgement can happen after a multiplexed profile's
        runtime scope has unwound.  Re-resolving ``get_hermes_home()`` at that
        boundary can therefore write the secondary profile's acknowledgement
        into the default profile.  Bind delivery metadata to the store that
        actually sealed the turn instead.

        The canonical checkpoint layout is part of the durable contract.  A
        non-canonical store remains usable by low-level tests and tooling, but
        it cannot mint trusted delivery-routing metadata.
        """
        checkpoint_root = self.root.expanduser().resolve(strict=False)
        if (
            checkpoint_root.name.casefold() != "turn-checkpoints"
            or checkpoint_root.parent.name.casefold() != "sessions"
        ):
            raise CheckpointIntegrityError(
                "delivery checkpoint store is outside the canonical "
                "<storage-home>/sessions/turn-checkpoints layout"
            )
        storage_home = checkpoint_root.parent.parent.resolve(strict=False)
        return {
            "checkpoint_root": str(checkpoint_root),
            "storage_home": str(storage_home),
        }

    def _mutate_current(
        self,
        session_id: str,
        mutator,
        *,
        expected_turn_id: str | None = None,
        max_conflict_retries: int = 16,
        precommit: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one optimistic mutation without losing concurrent tool updates.

        Tool workers can finish concurrently.  A plain ``load`` followed by
        ``_write`` lets two workers start from the same revision and one lose
        its checkpoint update.  Retry only compare-and-swap conflicts, and
        fence late workers to the turn that dispatched them so they can never
        mutate a newer turn in the same session.
        """
        last_conflict: CheckpointConflictError | None = None
        for _ in range(max_conflict_retries):
            state = copy.deepcopy(self.load(session_id))
            if (
                expected_turn_id is not None
                and str(state.get("turn_id") or "") != str(expected_turn_id)
            ):
                raise CheckpointConflictError(
                    "tool checkpoint update belongs to an older session turn"
                )
            changed = mutator(state)
            if changed is False:
                return state
            state["revision"] = int(state.get("revision", 0)) + 1
            try:
                return self._write(state, precommit=precommit)
            except CheckpointConflictError as exc:
                last_conflict = exc
                continue
        raise CheckpointConflictError(
            "checkpoint mutation could not commit after concurrent retries"
        ) from last_conflict

    def path_for(self, session_id: str) -> Path:
        digest = _sha256_text(str(session_id))[:32]
        return self.root / f"{digest}.json"

    def _read_path(self, path: Path, session_id: str) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise CheckpointIntegrityError(f"checkpoint JSON read failed: {exc}") from exc
        return _validate_state(state, expected_session_id=session_id)

    def load(self, session_id: str) -> dict[str, Any]:
        path = self.path_for(session_id)
        with _lock_for(path):
            return self._read_path(path, session_id)

    def _write(
        self,
        state: Mapping[str, Any],
        *,
        precommit: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        candidate = copy.deepcopy(dict(state))
        candidate["schema_version"] = SCHEMA_VERSION
        candidate["updated_at"] = time.time()
        candidate.pop("payload_sha256", None)
        candidate["payload_sha256"] = _payload_checksum(candidate)
        session_id = str(candidate.get("session_id") or "")
        if not session_id:
            raise CheckpointIntegrityError("checkpoint session_id is required")
        _validate_state(candidate, expected_session_id=session_id)

        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        encoded = (_canonical_json(candidate) + "\n").encode("utf-8")
        lock_path = path.with_name(f".{path.name}.lock")
        try:
            with FileLock(str(lock_path), timeout=10.0), _lock_for(path):
                if path.exists():
                    current = self._read_path(path, session_id)
                    current_turn = str(current.get("turn_id") or "")
                    candidate_turn = str(candidate.get("turn_id") or "")
                    current_revision = int(current.get("revision", 0))
                    candidate_revision = int(candidate.get("revision", 0))
                    if current_turn == candidate_turn:
                        if candidate_revision != current_revision + 1:
                            raise CheckpointConflictError(
                                "stale checkpoint write rejected: revision changed "
                                f"from {current_revision} before candidate "
                                f"{candidate_revision} committed"
                            )
                    elif candidate_revision not in {0, 1}:
                        raise CheckpointConflictError(
                            "stale checkpoint write rejected: a newer turn already "
                            "owns this session checkpoint"
                        )
                    elif float(candidate.get("created_at", 0) or 0) <= float(
                        current.get("created_at", 0) or 0
                    ):
                        raise CheckpointConflictError(
                            "stale checkpoint write rejected: candidate turn predates "
                            "the active session turn"
                        )
                # Run sidecar persistence only after the revision/turn CAS has
                # succeeded and while the checkpoint's cross-process lock is
                # still held.  Gateway reseals use this to persist the exact
                # inactive recovery artifact before publishing its new hash,
                # without allowing a stale turn to leave an orphan artifact.
                if precommit is not None:
                    precommit()
                try:
                    with open(temp, "xb") as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                    try:
                        directory_fd = os.open(str(path.parent), os.O_RDONLY)
                    except (OSError, AttributeError):
                        directory_fd = None
                    if directory_fd is not None:
                        try:
                            os.fsync(directory_fd)
                        except OSError:
                            pass
                        finally:
                            os.close(directory_fd)
                    read_back = self._read_path(path, session_id)
                    if read_back.get("revision") != candidate.get("revision"):
                        raise CheckpointIntegrityError(
                            "checkpoint read-back revision mismatch"
                        )
                    return read_back
                finally:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
        except FileLockTimeout as exc:
            raise CheckpointWriteError(
                f"checkpoint file lock timed out for session {session_id}"
            ) from exc
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointWriteError(f"checkpoint atomic write failed: {exc}") from exc

    @staticmethod
    def _base_state(
        *,
        session_id: str,
        turn_id: str,
        user_content: Any,
        messages: Sequence[Any],
        routing: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        now = time.time()
        raw_user = user_content if isinstance(user_content, str) else _canonical_json(user_content)
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "turn_id": turn_id,
            "revision": 1,
            "phase": "turn_started",
            "next_action": "continue_current_turn",
            "created_at": now,
            "updated_at": now,
            "routing": dict(routing or {}),
            "active_user_turn": {
                "sha256": _sha256_text(raw_user),
                "content": _redacted_literal(raw_user),
            },
            "transcript": {
                "current_hash": transcript_hash(messages),
                "message_count": len(messages),
                "before_hash": None,
                "after_hash": None,
            },
            "compaction": {"state": "captured", "prepared_at": None, "committed_at": None},
            "pending_tool": None,
            "pending_tools": [],
            "unknown_outcomes": [],
            "completed_tools": [],
            "pending_deliverable": None,
            "verification": {"pending": False, "attempts": 0, "kind": None},
            "changed_paths": [],
            "artifacts": [],
            "blockers": [],
            "delivery": {"obligation_id": None, "status": "none"},
            "recovery": {"restored": False, "resolution": "new_turn"},
            "payload_sha256": "",
        }

    def start_turn(
        self,
        session_id: str,
        turn_id: str,
        user_content: Any,
        messages: Sequence[Any],
        routing: Mapping[str, Any] | None = None,
        resume_existing: bool = False,
    ) -> dict[str, Any]:
        raw_user = user_content if isinstance(user_content, str) else _canonical_json(user_content)
        user_sha = _sha256_text(raw_user)
        try:
            existing = self.restore(session_id, messages)
        except FileNotFoundError:
            existing = None
        if (
            existing
            and existing.get("phase") not in {"terminal", "delivered", "cancelled"}
            and (
                resume_existing
                or existing.get("active_user_turn", {}).get("sha256") == user_sha
            )
        ):
            state = copy.deepcopy(existing)
            state["revision"] = int(state.get("revision", 0)) + 1
            state["routing"] = dict(routing or state.get("routing") or {})
            state["recovery"] = {
                "restored": True,
                "resolution": state.get("recovery", {}).get("resolution", "same_turn"),
            }
            return self._write(state)

        return self._write(
            self._base_state(
                session_id=session_id,
                turn_id=turn_id,
                user_content=user_content,
                messages=messages,
                routing=routing,
            )
        )

    def transition(
        self,
        session_id: str,
        *,
        phase: str,
        next_action: str | None = None,
        changed_paths: Sequence[str] | None = None,
        artifacts: Sequence[str] | None = None,
        blockers: Sequence[str] | None = None,
        verification: Mapping[str, Any] | None = None,
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        state["revision"] = int(state.get("revision", 0)) + 1
        state["phase"] = str(phase)
        if next_action is not None:
            state["next_action"] = _redacted_literal(next_action, limit=8_192)
        if changed_paths is not None:
            state["changed_paths"] = sorted({str(path) for path in changed_paths if path})
        if artifacts is not None:
            state["artifacts"] = sorted({str(path) for path in artifacts if path})
        if blockers is not None:
            state["blockers"] = [_redacted_literal(item, limit=8_192) for item in blockers]
        if verification is not None:
            state["verification"] = {**state.get("verification", {}), **dict(verification)}
        if delivery is not None:
            state["delivery"] = {**state.get("delivery", {}), **dict(delivery)}
        return self._write(state)

    def prepare_compaction(
        self,
        session_id: str,
        before_messages: Sequence[Any],
        after_messages: Sequence[Any],
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        before_hash = transcript_hash(before_messages)
        after_hash = transcript_hash(after_messages)
        state["revision"] = int(state.get("revision", 0)) + 1
        state["transcript"] = {
            "current_hash": before_hash,
            "message_count": len(before_messages),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "after_count": len(after_messages),
        }
        state["compaction"] = {
            "state": "prepared",
            "prepared_at": time.time(),
            "committed_at": None,
        }
        state["next_action"] = "commit_compacted_transcript_then_resume"
        return self._write(state)

    def commit_compaction(
        self, session_id: str, active_messages: Sequence[Any]
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        live_hash = transcript_hash(active_messages)
        expected = state.get("transcript", {}).get("after_hash")
        if not expected or live_hash != expected:
            raise CheckpointConflictError(
                "cannot commit checkpoint: live transcript does not match after transcript"
            )
        state["revision"] = int(state.get("revision", 0)) + 1
        state["transcript"]["current_hash"] = live_hash
        state["transcript"]["message_count"] = len(active_messages)
        state["compaction"] = {
            "state": "committed",
            "prepared_at": state.get("compaction", {}).get("prepared_at"),
            "committed_at": time.time(),
        }
        state["phase"] = "turn_active"
        state["next_action"] = "resume_current_turn_from_checkpoint"
        return self._write(state)

    def commit_compaction_rebased(
        self, session_id: str, active_messages: Sequence[Any]
    ) -> dict[str, Any]:
        """Commit a compaction whose live transcript GREW during the swap.

        A steer (or any concurrent append) landing between prepare and
        commit makes the strict hash check fail even though nothing is
        wrong: the live transcript is exactly the compacted set plus new
        tail messages. Accept that shape — the prepared ``after`` must be a
        verbatim prefix of the live transcript — and commit on the live
        hash. Anything else is a genuine conflict and still raises.
        """
        state = copy.deepcopy(self.load(session_id))
        transcript = state.get("transcript", {})
        expected = transcript.get("after_hash")
        after_count = int(transcript.get("after_count") or 0)
        live = list(active_messages)
        if not expected or after_count <= 0 or len(live) < after_count:
            raise CheckpointConflictError(
                "cannot rebase checkpoint: live transcript does not extend "
                "the prepared after transcript"
            )
        if transcript_hash(live[:after_count]) != expected:
            raise CheckpointConflictError(
                "cannot rebase checkpoint: live transcript does not extend "
                "the prepared after transcript"
            )
        state["revision"] = int(state.get("revision", 0)) + 1
        state["transcript"]["current_hash"] = transcript_hash(live)
        state["transcript"]["message_count"] = len(live)
        state["compaction"] = {
            "state": "committed",
            "prepared_at": state.get("compaction", {}).get("prepared_at"),
            "committed_at": time.time(),
        }
        state["phase"] = "turn_active"
        state["next_action"] = "resume_current_turn_from_checkpoint"
        return self._write(state)

    def migrate_session(
        self,
        old_session_id: str,
        new_session_id: str,
        active_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Move the live checkpoint across a legacy session rotation boundary."""
        old_state = copy.deepcopy(self.load(old_session_id))
        new_state = copy.deepcopy(old_state)
        new_state["session_id"] = str(new_session_id)
        new_state["parent_session_id"] = str(old_session_id)
        new_state["revision"] = 0
        new_state["compaction"] = {
            "state": "committed",
            "prepared_at": old_state.get("compaction", {}).get("prepared_at"),
            "committed_at": time.time(),
        }
        live_hash = transcript_hash(active_messages)
        new_state["transcript"]["after_hash"] = live_hash
        new_state["transcript"]["current_hash"] = live_hash
        new_state["transcript"]["message_count"] = len(active_messages)
        new_state["phase"] = "turn_active"
        new_state["next_action"] = "resume_current_turn_from_checkpoint"
        written = self._write(new_state)

        old_state["revision"] = int(old_state.get("revision", 0)) + 1
        old_state["phase"] = "superseded"
        old_state["next_action"] = f"continue_in_session:{new_session_id}"
        old_state["recovery"] = {
            "restored": False,
            "resolution": "rotated",
            "child_session_id": str(new_session_id),
        }
        self._write(old_state)
        return written

    def restore(
        self, session_id: str, active_messages: Sequence[Any]
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        live_hash = transcript_hash(active_messages)
        compaction_state = state.get("compaction", {}).get("state")
        changed = False

        if compaction_state == "prepared":
            before_hash = state.get("transcript", {}).get("before_hash")
            after_hash = state.get("transcript", {}).get("after_hash")
            if live_hash == before_hash:
                state["compaction"]["state"] = "captured"
                state["phase"] = "turn_active"
                state["recovery"] = {
                    "restored": True,
                    "resolution": "swap_not_applied",
                }
                state["next_action"] = "retry_compaction_or_continue_original_transcript"
                changed = True
            elif live_hash == after_hash:
                state["compaction"]["state"] = "committed"
                state["compaction"]["committed_at"] = time.time()
                state["transcript"]["current_hash"] = live_hash
                state["transcript"]["message_count"] = len(active_messages)
                state["phase"] = "turn_active"
                state["recovery"] = {
                    "restored": True,
                    "resolution": "swap_committed_before_ack",
                }
                state["next_action"] = "resume_current_turn_from_checkpoint"
                changed = True
            else:
                state["compaction"]["state"] = "conflict"
                state["phase"] = "checkpoint_conflict"
                state["blockers"] = list(state.get("blockers") or []) + [
                    "Live transcript matched neither before nor after checkpoint hash."
                ]
                state["revision"] = int(state.get("revision", 0)) + 1
                self._write(state)
                raise CheckpointConflictError(
                    "live transcript matches neither before nor after checkpoint hash"
                )

        pending_items: list[dict[str, Any]] = []
        pending = state.get("pending_tool")
        if isinstance(pending, dict) and pending.get("status") == "attempting":
            pending_items.append(pending)
        pending_items.extend(
            item
            for item in (state.get("pending_tools") or [])
            if isinstance(item, dict) and item.get("status") == "attempting"
        )
        if pending_items:
            existing = {
                item.get("fingerprint")
                for item in state.get("unknown_outcomes", [])
                if isinstance(item, dict)
            }
            for pending_item in pending_items:
                fingerprint = pending_item.get("fingerprint")
                if fingerprint in existing:
                    continue
                state.setdefault("unknown_outcomes", []).append(
                    {
                        "call_id": pending_item.get("call_id"),
                        "name": pending_item.get("name"),
                        "fingerprint": fingerprint,
                        "status": "unknown_outcome",
                        "attempted_at": pending_item.get("attempted_at"),
                        "detected_at": time.time(),
                        "replay_block_count": 0,
                    }
                )
                existing.add(fingerprint)
            state["pending_tool"] = None
            state["pending_tools"] = []
            state["phase"] = "reconcile_required"
            state["next_action"] = "read_back_target_before_retrying_uncertain_tool"
            state["recovery"] = {
                "restored": True,
                "resolution": "uncertain_tool_detected",
            }
            changed = True

        if changed:
            state["revision"] = int(state.get("revision", 0)) + 1
            return self._write(state)
        return state

    @staticmethod
    def _pending_tool_record(call: Mapping[str, Any]) -> dict[str, Any]:
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        return {
            "call_id": str(call.get("call_id") or ""),
            "name": name,
            "fingerprint": tool_fingerprint(name, arguments),
            "arguments_sha256": _sha256_text(
                _canonical_json(_stable_tool_arguments(arguments))
            ),
            "status": "attempting",
            "attempted_at": time.time(),
        }

    def mark_tool_batch_attempt(
        self,
        session_id: str,
        calls: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not calls:
            return self.load(session_id)
        state = copy.deepcopy(self.load(session_id))
        pending = [self._pending_tool_record(call) for call in calls]
        state["revision"] = int(state.get("revision", 0)) + 1
        state["phase"] = "tool_attempting"
        state["next_action"] = "await_tool_batch_results"
        state["pending_tools"] = pending
        state["pending_tool"] = pending[0] if len(pending) == 1 else None
        return self._write(state)

    def mark_tool_attempt(
        self,
        session_id: str,
        *,
        call_id: str,
        name: str,
        arguments: Any,
        expected_turn_id: str | None = None,
    ) -> dict[str, Any]:
        pending = self._pending_tool_record(
            {"call_id": call_id, "name": name, "arguments": arguments}
        )

        def mutate(state: dict[str, Any]) -> bool:
            pending_items = list(state.get("pending_tools") or [])
            if isinstance(state.get("pending_tool"), dict):
                legacy = state["pending_tool"]
                if not any(
                    str(item.get("call_id") or "")
                    == str(legacy.get("call_id") or "")
                    for item in pending_items
                    if isinstance(item, dict)
                ):
                    pending_items.append(legacy)
            if any(
                isinstance(item, dict)
                and str(item.get("call_id") or "") == str(call_id)
                for item in pending_items
            ):
                return False

            fingerprint = pending.get("fingerprint")
            matching_unknown = [
                item
                for item in (state.get("unknown_outcomes") or [])
                if isinstance(item, dict)
                and item.get("fingerprint") == fingerprint
                and not (
                    isinstance(item.get("reconciliation"), dict)
                    and item["reconciliation"].get("retry_completed_at")
                )
            ]
            retry_authorized = False
            for unknown in state.get("unknown_outcomes") or []:
                if not isinstance(unknown, dict):
                    continue
                if unknown.get("fingerprint") != fingerprint:
                    continue
                reconciliation = unknown.get("reconciliation")
                if not isinstance(reconciliation, dict):
                    continue
                if reconciliation.get("disposition") != "safe_to_retry":
                    continue
                if reconciliation.get("retry_consumed_at"):
                    continue
                reconciliation["retry_consumed_at"] = time.time()
                reconciliation["retry_call_id"] = str(call_id)
                retry_authorized = True
                break
            if matching_unknown and not retry_authorized:
                raise CheckpointIntegrityError(
                    "uncertain tool effect has no durable safe-to-retry authorization"
                )

            pending_items.append(pending)
            state["phase"] = "tool_attempting"
            state["next_action"] = "await_tool_results"
            state["pending_tools"] = pending_items
            state["pending_tool"] = (
                pending_items[0] if len(pending_items) == 1 else None
            )
            return True

        return self._mutate_current(
            session_id,
            mutate,
            expected_turn_id=expected_turn_id,
        )

    def mark_tool_batch_results(
        self,
        session_id: str,
        results: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        pending_items = list(state.get("pending_tools") or [])
        if not pending_items and isinstance(state.get("pending_tool"), dict):
            pending_items = [state["pending_tool"]]
        pending_by_id = {str(item.get("call_id")): item for item in pending_items}
        result_ids = {str(item.get("call_id")) for item in results}
        if set(pending_by_id) != result_ids:
            raise CheckpointIntegrityError(
                "tool result batch does not match pending checkpoint calls"
            )
        completed = list(state.get("completed_tools") or [])
        unknown = list(state.get("unknown_outcomes") or [])
        unknown_added = False
        for result in results:
            call_id = str(result.get("call_id"))
            pending = pending_by_id[call_id]
            summary = result.get("result_summary")
            disposition = str(result.get("disposition") or "completed")
            if disposition not in {"completed", "safe_to_retry", "unknown_outcome"}:
                disposition = "completed"
            record = {
                "call_id": call_id,
                "name": pending.get("name"),
                "fingerprint": pending.get("fingerprint"),
                "result_sha256": _sha256_text(_canonical_json(summary)),
                "result_summary": _redacted_literal(summary, limit=16_384),
                "effect_disposition": disposition,
                "completed_at": time.time(),
            }
            if disposition == "unknown_outcome":
                unknown.append(record)
                unknown_added = True
            else:
                completed.append(record)
        state["completed_tools"] = completed[-32:]
        state["unknown_outcomes"] = unknown[-32:]
        state["pending_tool"] = None
        state["pending_tools"] = []
        state["phase"] = "reconcile_required" if unknown_added else "tool_completed"
        state["next_action"] = (
            "read_back_target_before_retrying_uncertain_tool"
            if unknown_added
            else "evaluate_tool_results"
        )
        state["revision"] = int(state.get("revision", 0)) + 1
        return self._write(state)

    def mark_tool_result(
        self,
        session_id: str,
        *,
        call_id: str,
        result_summary: Any,
        disposition: str = "completed",
        expected_turn_id: str | None = None,
    ) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> bool:
            pending_items = list(state.get("pending_tools") or [])
            if not pending_items and isinstance(state.get("pending_tool"), dict):
                pending_items = [state["pending_tool"]]
            pending = next(
                (
                    item
                    for item in pending_items
                    if isinstance(item, dict)
                    and str(item.get("call_id") or "") == str(call_id)
                ),
                None,
            )
            if pending is None:
                completed_ids = {
                    str(item.get("call_id") or "")
                    for item in (state.get("completed_tools") or [])
                    if isinstance(item, dict)
                }
                unknown_ids = {
                    str(item.get("call_id") or "")
                    for item in (state.get("unknown_outcomes") or [])
                    if isinstance(item, dict)
                }
                if str(call_id) in completed_ids | unknown_ids:
                    return False
                raise CheckpointIntegrityError(
                    "tool result does not match a pending checkpoint call"
                )

            effect_disposition = str(disposition or "completed")
            if effect_disposition not in {
                "completed",
                "safe_to_retry",
                "unknown_outcome",
            }:
                effect_disposition = "completed"
            record = {
                "call_id": str(call_id),
                "name": pending.get("name"),
                "fingerprint": pending.get("fingerprint"),
                "result_sha256": _sha256_text(_canonical_json(result_summary)),
                "result_summary": _redacted_literal(result_summary, limit=16_384),
                "effect_disposition": effect_disposition,
                "completed_at": time.time(),
            }
            remaining = [
                item
                for item in pending_items
                if not (
                    isinstance(item, dict)
                    and str(item.get("call_id") or "") == str(call_id)
                )
            ]
            completed = list(state.get("completed_tools") or [])
            unknown = list(state.get("unknown_outcomes") or [])
            if effect_disposition == "unknown_outcome":
                record["status"] = "unknown_outcome"
                record["detected_at"] = time.time()
                record["replay_block_count"] = 0
                unknown.append(record)
            else:
                completed.append(record)
                for prior in unknown:
                    if not isinstance(prior, dict):
                        continue
                    if prior.get("fingerprint") != pending.get("fingerprint"):
                        continue
                    reconciliation = prior.get("reconciliation")
                    if not isinstance(reconciliation, dict):
                        continue
                    if reconciliation.get("retry_call_id") != str(call_id):
                        continue
                    reconciliation["retry_completed_at"] = time.time()
                    reconciliation["retry_result_sha256"] = record["result_sha256"]
            state["completed_tools"] = completed[-32:]
            state["unknown_outcomes"] = unknown[-32:]
            state["pending_tools"] = remaining
            state["pending_tool"] = remaining[0] if len(remaining) == 1 else None
            unresolved = any(
                isinstance(item, dict)
                and not isinstance(item.get("reconciliation"), dict)
                for item in state["unknown_outcomes"]
            )
            state["phase"] = "reconcile_required" if unresolved else "tool_completed"
            state["next_action"] = (
                "read_back_target_before_retrying_uncertain_tool"
                if unresolved
                else "evaluate_tool_results"
            )
            return True

        return self._mutate_current(
            session_id,
            mutate,
            expected_turn_id=expected_turn_id,
        )

    def guard_unknown_replay(
        self, session_id: str, name: str, arguments: Any
    ) -> str | None:
        state = copy.deepcopy(self.load(session_id))
        fingerprint = tool_fingerprint(name, arguments)
        matches = [
            item
            for item in state.get("unknown_outcomes", [])
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint
        ]
        if not matches:
            return None
        unresolved = [
            item
            for item in matches
            if not isinstance(item.get("reconciliation"), dict)
        ]
        retryable = [
            item
            for item in matches
            if isinstance(item.get("reconciliation"), dict)
            and item["reconciliation"].get("disposition") == "safe_to_retry"
            and not item["reconciliation"].get("retry_consumed_at")
        ]
        if not unresolved and retryable:
            return None
        if not unresolved and all(
            isinstance(item.get("reconciliation"), dict)
            and item["reconciliation"].get("retry_completed_at")
            for item in matches
        ):
            return None
        for item in state.get("unknown_outcomes", []):
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint:
                item["replay_block_count"] = int(item.get("replay_block_count", 0)) + 1
                item["last_blocked_at"] = time.time()
        state["phase"] = "reconcile_required"
        state["next_action"] = "read_back_target_before_retrying_uncertain_tool"
        state["revision"] = int(state.get("revision", 0)) + 1
        self._write(state)
        return (
            "Checkpoint blocked this exact replay because a prior tool call has an "
            "unknown outcome. The block is durable: perform an authoritative, "
            "tool-specific readback and record a reconciled disposition before any "
            "retry. Generic model text cannot unlock this effect boundary."
        )

    def reconcile_unknown_outcome(
        self,
        session_id: str,
        *,
        fingerprint: str,
        disposition: str,
        readback_call_id: str,
        reconciler_identity: str,
        evidence: Any,
        expected_turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Seal a tool-specific authoritative readback for an uncertain effect.

        This is deliberately a store API, not a generic model-callable escape
        hatch.  A registered reconciler must first perform a real readback and
        pass the completed call id.  Free-form model text alone cannot change
        the replay disposition.
        """
        if disposition not in {"already_applied", "safe_to_retry"}:
            raise CheckpointIntegrityError(
                "reconciliation disposition must be already_applied or safe_to_retry"
            )
        if not str(reconciler_identity or "").strip():
            raise CheckpointIntegrityError("reconciler identity is required")

        def mutate(state: dict[str, Any]) -> bool:
            target = next(
                (
                    item
                    for item in reversed(state.get("unknown_outcomes") or [])
                    if isinstance(item, dict)
                    and item.get("fingerprint") == fingerprint
                    and not isinstance(item.get("reconciliation"), dict)
                ),
                None,
            )
            if target is None:
                raise CheckpointIntegrityError(
                    "unknown tool outcome was not found or is already reconciled"
                )
            readback = next(
                (
                    item
                    for item in reversed(state.get("completed_tools") or [])
                    if isinstance(item, dict)
                    and str(item.get("call_id") or "") == str(readback_call_id)
                ),
                None,
            )
            if readback is None:
                raise CheckpointIntegrityError(
                    "authoritative readback call is not durably completed"
                )
            detected_at = float(
                target.get("detected_at") or target.get("completed_at") or 0
            )
            if float(readback.get("completed_at") or 0) < detected_at:
                raise CheckpointIntegrityError(
                    "authoritative readback predates the uncertain outcome"
                )
            target["reconciliation"] = {
                "disposition": disposition,
                "readback_call_id": str(readback_call_id),
                "readback_tool": readback.get("name"),
                "readback_fingerprint": readback.get("fingerprint"),
                "readback_result_sha256": readback.get("result_sha256"),
                "evidence_sha256": _sha256_text(_canonical_json(evidence)),
                "evidence": _redacted_literal(evidence, limit=8_192),
                "reconciler_identity": str(reconciler_identity),
                "reconciled_at": time.time(),
            }
            state["phase"] = (
                "tool_completed"
                if disposition == "already_applied"
                else "reconcile_required"
            )
            state["next_action"] = (
                "evaluate_tool_results"
                if disposition == "already_applied"
                else "retry_authorized_tool_once"
            )
            return True

        return self._mutate_current(
            session_id,
            mutate,
            expected_turn_id=expected_turn_id,
        )

    def mark_deliverable(
        self,
        session_id: str,
        content: str,
        *,
        verification_pending: bool,
        verification_attempts: int = 0,
        verification_kind: str = "verify_on_stop",
        expected_fence: Mapping[str, Any] | None = None,
        require_unbound_delivery: bool = False,
        precommit: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        normalized_expected_fence = None
        if expected_fence is not None:
            normalized_expected_fence = {
                "turn_id": str(expected_fence.get("turn_id") or ""),
                "deliverable_revision": str(
                    expected_fence.get("deliverable_revision") or ""
                ),
                "content_sha256": str(expected_fence.get("content_sha256") or ""),
            }
            if not all(normalized_expected_fence.values()):
                raise ValueError("expected deliverable fence must be complete")

        def mutate(state: dict[str, Any]) -> bool:
            previous = state.get("pending_deliverable")
            if normalized_expected_fence is not None:
                current_fence = {
                    "turn_id": str(
                        previous.get("turn_id") or state.get("turn_id") or ""
                    )
                    if isinstance(previous, Mapping)
                    else "",
                    "deliverable_revision": str(
                        previous.get("deliverable_revision") or ""
                    )
                    if isinstance(previous, Mapping)
                    else "",
                    "content_sha256": str(previous.get("sha256") or "")
                    if isinstance(previous, Mapping)
                    else "",
                }
                if current_fence != normalized_expected_fence:
                    raise CheckpointConflictError(
                        "deliverable reseal belongs to a stale checkpoint fence"
                    )
            delivery = state.get("delivery")
            if (
                require_unbound_delivery
                and isinstance(delivery, Mapping)
                and delivery.get("obligation_id")
            ):
                raise CheckpointConflictError(
                    "bound delivery checkpoint cannot be resealed"
                )

            digest = _sha256_text(content or "")
            previous_revision = (
                previous.get("deliverable_revision")
                if isinstance(previous, Mapping)
                and previous.get("sha256") == digest
                and previous.get("turn_id") == state.get("turn_id")
                else None
            )
            state["pending_deliverable"] = {
                "turn_id": state.get("turn_id"),
                "deliverable_revision": previous_revision or uuid.uuid4().hex,
                "sha256": digest,
                "content": _redacted_literal(content or ""),
                "captured_at": time.time(),
            }
            state["verification"] = {
                "pending": bool(verification_pending),
                "attempts": int(verification_attempts),
                "kind": str(verification_kind),
            }
            state["phase"] = (
                "verification_pending"
                if verification_pending
                else "deliverable_composed"
            )
            state["next_action"] = (
                "verify_then_deliver" if verification_pending else "finalize_delivery"
            )
            return True

        return self._mutate_current(
            session_id,
            mutate,
            expected_turn_id=(
                normalized_expected_fence["turn_id"]
                if normalized_expected_fence is not None
                else None
            ),
            precommit=precommit,
        )

    def mark_terminal(
        self,
        session_id: str,
        *,
        final_response: str,
        delivery_obligation_id: str | None = None,
        delivery_status: str = "none",
    ) -> dict[str, Any]:
        state = copy.deepcopy(self.load(session_id))
        if not state.get("pending_deliverable"):
            state["pending_deliverable"] = {
                "sha256": _sha256_text(final_response or ""),
                "content": _redacted_literal(final_response or ""),
                "captured_at": time.time(),
            }
        state["verification"] = {**state.get("verification", {}), "pending": False}
        state["delivery"] = {
            "obligation_id": delivery_obligation_id,
            "status": delivery_status,
        }
        if delivery_status in {"pending", "awaiting_ledger", "attempting", "failed"}:
            state["phase"] = "delivery_pending"
            state["next_action"] = "await_or_recover_delivery_obligation"
        elif delivery_status == "delivered":
            state["phase"] = "delivered"
            state["next_action"] = "none"
        else:
            state["phase"] = "terminal"
            state["next_action"] = "none"
        state["revision"] = int(state.get("revision", 0)) + 1
        return self._write(state)

    def bind_delivery_obligation(
        self,
        session_id: str,
        *,
        obligation_id: str,
        turn_id: str,
        deliverable_revision: str,
        content_sha256: str,
        routing: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """CAS-bind one delivery obligation to one exact deliverable fence.

        The first non-empty obligation id committed for the fenced deliverable
        owns that checkpoint.  Rebinding the same id is idempotent (and does
        not advance the checkpoint revision); a different id or stale fence is
        rejected without mutating durable state.
        """
        requested_obligation = str(obligation_id or "")
        requested_route = _canonical_delivery_route(routing)
        requested_route_sha256 = _delivery_route_sha256(routing)
        requested_fence = {
            "turn_id": str(turn_id or ""),
            "deliverable_revision": str(deliverable_revision or ""),
            "content_sha256": str(content_sha256 or ""),
        }
        if (
            not requested_obligation
            or not all(requested_fence.values())
            or requested_route is None
            or requested_route_sha256 is None
        ):
            return None

        def mutate(state: dict[str, Any]) -> bool:
            pending = state.get("pending_deliverable")
            if not isinstance(pending, Mapping):
                return False
            current_fence = {
                "turn_id": str(
                    pending.get("turn_id") or state.get("turn_id") or ""
                ),
                "deliverable_revision": str(
                    pending.get("deliverable_revision") or ""
                ),
                "content_sha256": str(pending.get("sha256") or ""),
            }
            if current_fence != requested_fence:
                return False
            checkpoint_route = _canonical_delivery_route(state.get("routing"))
            if checkpoint_route != requested_route:
                return False

            current_delivery = state.get("delivery")
            delivery = (
                dict(current_delivery)
                if isinstance(current_delivery, Mapping)
                else {"status": "none"}
            )
            current_obligation = str(delivery.get("obligation_id") or "")
            current_route_sha256 = str(delivery.get("route_sha256") or "")
            if current_obligation and current_obligation != requested_obligation:
                return False
            if current_route_sha256 and current_route_sha256 != requested_route_sha256:
                return False
            delivery["obligation_id"] = requested_obligation
            delivery["route_sha256"] = requested_route_sha256
            state["delivery"] = delivery
            return not (
                current_obligation == requested_obligation
                and current_route_sha256 == requested_route_sha256
            )

        state = self._mutate_current(
            session_id,
            mutate,
        )
        pending = state.get("pending_deliverable")
        if not isinstance(pending, Mapping):
            return None
        committed_fence = {
            "turn_id": str(pending.get("turn_id") or state.get("turn_id") or ""),
            "deliverable_revision": str(
                pending.get("deliverable_revision") or ""
            ),
            "content_sha256": str(pending.get("sha256") or ""),
        }
        delivery = state.get("delivery")
        committed_obligation = (
            str(delivery.get("obligation_id") or "")
            if isinstance(delivery, Mapping)
            else ""
        )
        committed_route_sha256 = (
            str(delivery.get("route_sha256") or "")
            if isinstance(delivery, Mapping)
            else ""
        )
        if (
            committed_fence != requested_fence
            or committed_obligation != requested_obligation
            or committed_route_sha256 != requested_route_sha256
            or _canonical_delivery_route(state.get("routing")) != requested_route
        ):
            return None
        return state

    def mark_delivery_status(
        self,
        session_id: str,
        *,
        obligation_id: str,
        status: str,
        turn_id: str,
        deliverable_revision: str,
        content_sha256: str,
    ) -> dict[str, Any] | None:
        state = copy.deepcopy(self.load(session_id))
        pending = state.get("pending_deliverable")
        if not isinstance(pending, Mapping):
            return None
        if (
            str(state.get("turn_id") or "") != str(turn_id or "")
            or str(pending.get("turn_id") or "") != str(turn_id or "")
            or str(pending.get("deliverable_revision") or "")
            != str(deliverable_revision or "")
            or str(pending.get("sha256") or "") != str(content_sha256 or "")
        ):
            return None
        current_delivery = state.get("delivery")
        current_obligation = (
            str(current_delivery.get("obligation_id") or "")
            if isinstance(current_delivery, Mapping)
            else ""
        )
        requested_obligation = str(obligation_id or "")
        if not requested_obligation:
            return None
        if current_obligation and current_obligation != requested_obligation:
            return None
        if status in {"delivered", "failed", "deferred", "delivery_ambiguous"}:
            if not current_obligation:
                return None
        delivery = (
            dict(current_delivery)
            if isinstance(current_delivery, Mapping)
            else {}
        )
        delivery.update(
            {
                "obligation_id": requested_obligation,
                "status": status,
            }
        )
        state["delivery"] = delivery
        if status == "delivered":
            state["phase"] = "delivered"
            state["next_action"] = "none"
        else:
            state["phase"] = "delivery_pending"
            state["next_action"] = "recover_delivery_obligation"
        state["revision"] = int(state.get("revision", 0)) + 1
        return self._write(state)

    def mark_best_effort_delivery(
        self,
        session_id: str,
        *,
        reported_success: bool,
        turn_id: str,
        deliverable_revision: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Close an unsupported transport without claiming exact delivery.

        Some platform adapters can reformat or truncate text while returning
        success.  They must not bind an exactly-once obligation to the full
        checkpoint payload.  Record their transport outcome explicitly as
        best-effort and make the checkpoint terminal so restart recovery does
        not loop on a guarantee that transport cannot provide.
        """

        expected_fence = {
            "turn_id": str(turn_id or ""),
            "deliverable_revision": str(deliverable_revision or ""),
            "content_sha256": str(content_sha256 or ""),
        }
        if not all(expected_fence.values()):
            raise ValueError("best-effort delivery fence must be complete")

        def mutate(state: dict[str, Any]) -> bool:
            pending = state.get("pending_deliverable")
            current_fence = {
                "turn_id": str(state.get("turn_id") or ""),
                "deliverable_revision": (
                    str(pending.get("deliverable_revision") or "")
                    if isinstance(pending, Mapping)
                    else ""
                ),
                "content_sha256": (
                    str(pending.get("sha256") or "")
                    if isinstance(pending, Mapping)
                    else ""
                ),
            }
            if current_fence != expected_fence:
                raise CheckpointConflictError(
                    "best-effort delivery belongs to a stale checkpoint fence"
                )
            delivery = state.get("delivery")
            if isinstance(delivery, Mapping) and delivery.get("obligation_id"):
                raise CheckpointConflictError(
                    "bound exact delivery cannot be downgraded to best-effort"
                )
            state["delivery"] = {
                "obligation_id": None,
                "status": "best_effort",
                "reported_success": bool(reported_success),
            }
            state["phase"] = "terminal"
            state["next_action"] = "none"
            return True

        return self._mutate_current(
            session_id,
            mutate,
            expected_turn_id=expected_fence["turn_id"],
        )

    def mark_delivery_if_content_matches(
        self,
        session_id: str,
        *,
        content: str,
        obligation_id: str,
    ) -> dict[str, Any] | None:
        """Close delivery only when *content* is the checkpointed artifact.

        Streaming bypasses the gateway's ordinary send boundary.  Its final
        delivery acknowledgement is authoritative only for the exact payload
        recorded in ``pending_deliverable``; a preview, stale finalize, or a
        later turn must never close an unrelated checkpoint.
        """
        state = copy.deepcopy(self.load(session_id))
        pending = state.get("pending_deliverable")
        expected_sha = pending.get("sha256") if isinstance(pending, Mapping) else None
        if not expected_sha or expected_sha != _sha256_text(content or ""):
            return None
        if (
            state.get("phase") == "delivered"
            and isinstance(state.get("delivery"), Mapping)
            and state["delivery"].get("obligation_id") == obligation_id
        ):
            return state
        state["verification"] = {**state.get("verification", {}), "pending": False}
        state["delivery"] = {
            "obligation_id": str(obligation_id or ""),
            "status": "delivered",
        }
        state["phase"] = "delivered"
        state["next_action"] = "none"
        state["revision"] = int(state.get("revision", 0)) + 1
        return self._write(state)


def checkpoint_store_for_agent(agent: Any) -> TurnCheckpointStore | None:
    """Return the profile-scoped store for a real durable-session agent."""
    raw_session_id = getattr(agent, "session_id", None)
    session_db = getattr(agent, "_session_db", None)
    if (
        not isinstance(raw_session_id, str)
        or not raw_session_id
        or session_db is None
    ):
        return None
    db_path = getattr(session_db, "db_path", None)
    if not isinstance(db_path, (str, os.PathLike)):
        # Test doubles and stateless adapters often expose truthy MagicMock
        # attributes. They are not durable SessionDB instances and must not
        # create checkpoints under a fabricated path.
        return None
    existing = getattr(agent, "_turn_checkpoint_store", None)
    if isinstance(existing, TurnCheckpointStore):
        return existing

    checkpoint_home = Path(db_path).parent
    store = TurnCheckpointStore(checkpoint_home / "sessions" / "turn-checkpoints")
    agent._turn_checkpoint_store = store
    return store


def resumable_checkpoint_for_agent(agent: Any) -> dict[str, Any] | None:
    """Load the profile-scoped unfinished checkpoint for *agent*, if any.

    This is deliberately a read-only predicate used by the gateway before it
    interprets a human message as control-plane continuation.  A generic
    "continue" without durable unfinished state remains an ordinary new turn.
    """
    store = checkpoint_store_for_agent(agent)
    if store is None:
        return None
    try:
        state = store.load(str(agent.session_id))
    except (FileNotFoundError, CheckpointError, OSError, ValueError):
        return None
    return state if checkpoint_is_resumable(state) else None


def _content_sha256(value: Any) -> str:
    if not isinstance(value, str):
        value = _canonical_json(value)
    return _sha256_text(value)


def recover_checkpoint_message_content(
    agent: Any,
    state: Mapping[str, Any],
    messages: Sequence[Any],
    *,
    field: str = "pending_deliverable",
) -> str | None:
    """Resolve an exact checkpointed payload from the durable transcript by hash.

    The journal intentionally stores only a redacted diagnostic literal.  The
    canonical exact content remains in SessionDB and is selected by SHA-256.
    """
    target = state.get(field)
    target_sha = target.get("sha256") if isinstance(target, Mapping) else None
    if not target_sha:
        return None
    candidates = list(messages)
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is not None and session_id:
        try:
            candidates.extend(
                session_db.get_messages(str(session_id), include_inactive=True)
            )
        except Exception:
            pass
    for message in reversed(candidates):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str) and _content_sha256(content) == target_sha:
            return content
    return None


def initialize_agent_turn_checkpoint(
    agent: Any,
    *,
    turn_id: str,
    user_content: Any,
    messages: Sequence[Any],
) -> dict[str, Any] | None:
    store = checkpoint_store_for_agent(agent)
    if store is None:
        agent._turn_checkpoint_state = None
        return None
    # AIAgent stores gateway identity in private instance fields
    # (``_chat_id``/``_thread_id``); the former public-name lookup silently
    # emitted empty routes even for freshly constructed gateway agents.
    routing = {
        "platform": str(getattr(agent, "platform", None) or ""),
        "chat_id": str(
            getattr(agent, "_chat_id", None)
            or getattr(agent, "chat_id", None)
            or ""
        ),
        "thread_id": str(
            getattr(agent, "_thread_id", None)
            or getattr(agent, "thread_id", None)
            or ""
        ),
        "task_id": str(
            getattr(agent, "_task_id", None)
            or getattr(agent, "task_id", None)
            or ""
        ),
    }
    resume_existing = bool(getattr(agent, "_resume_turn_from_checkpoint", False))
    try:
        state = store.start_turn(
            str(agent.session_id),
            str(turn_id),
            user_content,
            messages,
            routing=routing,
            resume_existing=resume_existing,
        )
    finally:
        # One-shot: a reused gateway agent must not bind the next genuine user
        # turn to an older unfinished checkpoint.
        agent._resume_turn_from_checkpoint = False
    agent._turn_checkpoint_state = state
    agent._turn_checkpoint_restored = bool(state.get("recovery", {}).get("restored"))
    if agent._turn_checkpoint_restored:
        agent._turn_checkpoint_resume_baseline = {
            "revision": int(state.get("revision", 0) or 0),
            "phase": str(state.get("phase") or ""),
            "next_action": str(state.get("next_action") or ""),
            "completed_tools_count": len(state.get("completed_tools") or []),
        }
    else:
        agent._turn_checkpoint_resume_baseline = None
    agent._checkpoint_resume_guard_nudges = 0
    agent._checkpoint_resume_guard_exhausted = False
    agent._restored_pending_deliverable = recover_checkpoint_message_content(
        agent, state, messages
    )
    return state


def build_checkpoint_resume_note(state: Mapping[str, Any]) -> str:
    """Deterministic, secret-free control note for a resumed model turn."""
    uncertain = [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "fingerprint": item.get("fingerprint"),
        }
        for item in state.get("unknown_outcomes", [])
        if isinstance(item, Mapping)
    ]
    payload = {
        "schema_version": state.get("schema_version"),
        "turn_id": state.get("turn_id"),
        "phase": state.get("phase"),
        "next_action": state.get("next_action"),
        "verification": state.get("verification"),
        "changed_paths": state.get("changed_paths", []),
        "artifacts": state.get("artifacts", []),
        "blockers": state.get("blockers", []),
        "unknown_outcomes": uncertain,
        "delivery": state.get("delivery"),
    }
    return (
        "[TURN CHECKPOINT — MACHINE RESTORE]\n"
        "Resume the active user request from this checkpoint. The natural-language "
        "compaction summary is auxiliary; this structured phase/next_action state is "
        "authoritative. Reconcile every unknown_outcome against the target before "
        "retrying it.\n"
        + _canonical_json(payload)
    )


def build_checkpoint_continuation_nudge(
    agent: Any,
    final_response: str,
    *,
    max_nudges: int = 3,
) -> str | None:
    """Reject status-only exits from an explicit checkpoint continuation.

    The model may acknowledge a restore and stop without performing the next
    action.  That is not continuity.  This guard keeps the same model turn
    alive and never exposes the false acknowledgement as a completed answer.
    """
    if not bool(getattr(agent, "_gateway_explicit_checkpoint_resume", False)):
        return None
    state = getattr(agent, "_turn_checkpoint_state", None)
    if not isinstance(state, Mapping):
        return None
    restored = bool(state.get("recovery", {}).get("restored"))
    text = str(final_response or "").strip()
    status_only = bool(text and _CONTINUATION_ONLY_RESPONSE_RE.search(text))
    if restored and not status_only:
        return None
    attempts = int(getattr(agent, "_checkpoint_resume_guard_nudges", 0) or 0)
    if attempts >= max_nudges:
        agent._checkpoint_resume_guard_exhausted = True
        return None
    agent._checkpoint_resume_guard_nudges = attempts + 1
    if not restored:
        return (
            "[CHECKPOINT CONTINUATION GUARD] The requested checkpoint was not "
            "restored. Do not claim continuity or progress. Re-read the durable "
            "checkpoint and restore the original turn before answering."
        )
    return (
        "[CHECKPOINT CONTINUATION GUARD] A status-only acknowledgement is not "
        "a resumed task. Do not say that work is active, resumed, running, or "
        "being completed. Continue now from checkpoint.next_action, execute the "
        "remaining material steps, and stop only with a concrete result or a "
        "specific evidence-backed blocker."
    )


def update_checkpoint_delivery(
    session_id: str | None,
    *,
    obligation_id: str,
    status: str,
    turn_id: str,
    deliverable_revision: str,
    content_sha256: str,
    checkpoint_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Update only the exact deliverable revision owning an obligation.

    ``checkpoint_root`` is the trusted namespace emitted by the finalizer.
    The ambient-home fallback is retained only for single-profile and legacy
    direct callers; multiplexed gateway delivery must pass the explicit root.
    """
    if not session_id:
        return False
    if checkpoint_root is None:
        from hermes_constants import get_hermes_home

        checkpoint_root = get_hermes_home() / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    try:
        store.load(str(session_id))
    except FileNotFoundError:
        return False
    updated = store.mark_delivery_status(
        str(session_id),
        obligation_id=str(obligation_id or ""),
        status=str(status),
        turn_id=str(turn_id or ""),
        deliverable_revision=str(deliverable_revision or ""),
        content_sha256=str(content_sha256 or ""),
    )
    return updated is not None


def update_checkpoint_best_effort_delivery(
    session_id: str | None,
    *,
    reported_success: bool,
    turn_id: str,
    deliverable_revision: str,
    content_sha256: str,
    checkpoint_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Record a non-exact transport outcome without minting an obligation."""

    if not session_id:
        return False
    if checkpoint_root is None:
        from hermes_constants import get_hermes_home

        checkpoint_root = get_hermes_home() / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    try:
        store.mark_best_effort_delivery(
            str(session_id),
            reported_success=bool(reported_success),
            turn_id=str(turn_id or ""),
            deliverable_revision=str(deliverable_revision or ""),
            content_sha256=str(content_sha256 or ""),
        )
    except FileNotFoundError:
        return False
    return True


def bind_checkpoint_delivery_obligation(
    session_id: str | None,
    *,
    obligation_id: str,
    turn_id: str,
    deliverable_revision: str,
    content_sha256: str,
    routing: Mapping[str, Any],
    checkpoint_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Durably bind one exact obligation before any delivery side effect.

    ``checkpoint_root`` remains explicit for multiplexed profiles.  Omitting it
    retains the legacy single-profile ambient-home behavior.
    """
    if not session_id:
        return False
    if checkpoint_root is None:
        from hermes_constants import get_hermes_home

        checkpoint_root = get_hermes_home() / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    try:
        bound = store.bind_delivery_obligation(
            str(session_id),
            obligation_id=str(obligation_id or ""),
            turn_id=str(turn_id or ""),
            deliverable_revision=str(deliverable_revision or ""),
            content_sha256=str(content_sha256 or ""),
            routing=routing,
        )
    except FileNotFoundError:
        return False
    return bound is not None


def checkpoint_delivery_fence(
    state: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Return the immutable fence for the currently composed deliverable."""
    if not isinstance(state, Mapping):
        return None
    pending = state.get("pending_deliverable")
    if not isinstance(pending, Mapping):
        return None
    fence = {
        "turn_id": str(pending.get("turn_id") or state.get("turn_id") or ""),
        "deliverable_revision": str(
            pending.get("deliverable_revision") or ""
        ),
        "content_sha256": str(pending.get("sha256") or ""),
    }
    return fence if all(fence.values()) else None


def reseal_checkpoint_deliverable(
    session_id: str,
    content: str,
    *,
    expected_fence: Mapping[str, Any],
    checkpoint_root: str | os.PathLike[str],
    storage_home: str | os.PathLike[str],
    verification_kind: str,
) -> dict[str, str]:
    """CAS-reseal the exact outbound text and its recovery artifact.

    Gateway and platform transforms may legitimately change the bytes produced
    by the agent finalizer. The durable delivery ledger must fence the exact
    text handed to the transport, not a pre-extraction envelope containing
    ``MEDIA:`` directives. This helper keeps the checkpoint and inactive
    recovery artifact in one lock ordering and refuses stale or already-bound
    delivery state.
    """
    from hermes_state import SessionDB

    store = TurnCheckpointStore(checkpoint_root)
    with SessionDB(Path(storage_home) / "state.db") as session_db:
        state = store.mark_deliverable(
            str(session_id),
            content,
            verification_pending=False,
            verification_kind=str(verification_kind),
            expected_fence=expected_fence,
            require_unbound_delivery=True,
            precommit=lambda: session_db.append_delivery_recovery_artifact(
                str(session_id), content
            ),
        )
    fence = checkpoint_delivery_fence(state)
    if fence is None:
        raise CheckpointIntegrityError("resealed checkpoint fence is missing")
    return fence


def checkpoint_delivery_fence_matches(
    session_id: str,
    *,
    turn_id: str,
    deliverable_revision: str,
    content_sha256: str,
    obligation_id: str | None = None,
    routing: Mapping[str, Any] | None = None,
    checkpoint_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Read-only stale-obligation fence check used by startup recovery.

    Legacy callers may omit ``obligation_id`` to compare only the deliverable
    fence.  Passing it requires the checkpoint to be bound to that exact id.
    """
    if checkpoint_root is None:
        from hermes_constants import get_hermes_home

        checkpoint_root = get_hermes_home() / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    state = store.load(str(session_id))
    fence = checkpoint_delivery_fence(state)
    if fence != {
        "turn_id": str(turn_id or ""),
        "deliverable_revision": str(deliverable_revision or ""),
        "content_sha256": str(content_sha256 or ""),
    }:
        return False
    if obligation_id is None:
        if routing is None:
            return True
        requested_route = _canonical_delivery_route(routing)
        return (
            requested_route is not None
            and _canonical_delivery_route(state.get("routing")) == requested_route
        )
    requested_route = _canonical_delivery_route(routing)
    requested_route_sha256 = _delivery_route_sha256(routing)
    if requested_route is None or requested_route_sha256 is None:
        return False
    if _canonical_delivery_route(state.get("routing")) != requested_route:
        return False
    delivery = state.get("delivery")
    bound_obligation = (
        str(delivery.get("obligation_id") or "")
        if isinstance(delivery, Mapping)
        else ""
    )
    bound_route_sha256 = (
        str(delivery.get("route_sha256") or "")
        if isinstance(delivery, Mapping)
        else ""
    )
    return (
        bool(bound_obligation)
        and bound_obligation == str(obligation_id or "")
        and bound_route_sha256 == requested_route_sha256
    )


def update_checkpoint_stream_delivery(
    session_id: str | None,
    *,
    final_response: str,
    turn_id: str,
    deliverable_revision: str,
    content_sha256: str,
    checkpoint_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Close a streamed turn after exact final-payload confirmation.

    Returns ``False`` when there is no checkpoint or when the streamed final
    response does not match its pending deliverable byte-for-byte.
    """
    if not session_id or not final_response:
        return False
    fence = {
        "turn_id": str(turn_id or ""),
        "deliverable_revision": str(deliverable_revision or ""),
        "content_sha256": str(content_sha256 or ""),
    }
    if not all(fence.values()) or _sha256_text(final_response) != fence["content_sha256"]:
        return False
    if checkpoint_root is None:
        from hermes_constants import get_hermes_home

        checkpoint_root = get_hermes_home() / "sessions" / "turn-checkpoints"
    store = TurnCheckpointStore(checkpoint_root)
    obligation_id = "stream:" + hashlib.sha256(
        _canonical_json(fence).encode("utf-8", "surrogatepass")
    ).hexdigest()
    try:
        checkpoint_state = store.load(str(session_id))
        bound = store.bind_delivery_obligation(
            str(session_id),
            obligation_id=obligation_id,
            turn_id=fence["turn_id"],
            deliverable_revision=fence["deliverable_revision"],
            content_sha256=fence["content_sha256"],
            routing=checkpoint_state.get("routing") or {},
        )
        if bound is None:
            return False
        state = store.mark_delivery_status(
            str(session_id),
            obligation_id=obligation_id,
            status="delivered",
            turn_id=fence["turn_id"],
            deliverable_revision=fence["deliverable_revision"],
            content_sha256=fence["content_sha256"],
        )
    except FileNotFoundError:
        return False
    return state is not None


__all__ = [
    "SCHEMA_VERSION",
    "CheckpointConflictError",
    "CheckpointError",
    "CheckpointIntegrityError",
    "CheckpointWriteError",
    "TurnCheckpointStore",
    "bind_checkpoint_delivery_obligation",
    "build_checkpoint_resume_note",
    "build_checkpoint_continuation_nudge",
    "checkpoint_is_resumable",
    "checkpoint_delivery_fence",
    "checkpoint_delivery_fence_matches",
    "checkpoint_store_for_agent",
    "initialize_agent_turn_checkpoint",
    "recover_checkpoint_message_content",
    "resumable_checkpoint_for_agent",
    "tool_fingerprint",
    "transcript_hash",
    "update_checkpoint_delivery",
    "update_checkpoint_best_effort_delivery",
    "update_checkpoint_stream_delivery",
]
