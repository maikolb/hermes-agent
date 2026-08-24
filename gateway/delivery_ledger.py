"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_claimed()     -> state='claimed'     + unique attempt_token, pre-network
    mark_claimed_attempting()
                       -> state='attempting'  immediately before remote send
    mark_delivered()   -> state='delivered'   only on SendResult.success
    mark_deferred()    -> state='deferred'    adapter explicitly did not touch network
    mark_failed()      -> state='failed'      definitive rejection/exception

On startup, ``sweep_recoverable()`` claims only rows whose send never
started and whose immutable checkpoint fence still matches.  A crash after
``attempting`` is inherently ambiguous: the platform may have accepted the
message before local acknowledgement.  Strict durable sessions therefore
stop in ``delivery_ambiguous`` instead of retrying and risking a duplicate.
Legacy non-durable callers retain their historical marker-based behavior.
Every result transition for a durable row compares that exact token, so a
late acknowledgement from an earlier attempt cannot finish a newer attempt.

Poison rows cannot spin: attempts are capped, stale rows expire, and terminal
recovery states are kept briefly for inspection before pruning.

Durable final responses fail closed at this boundary.  A send is allowed only
after an immutable, checkpoint-fenced obligation is recorded.  Non-durable
legacy callers retain their historical best-effort behavior.

Recovery uses an additional durable-only ``claimed`` state. Claiming owns a
row without asserting that the network boundary was entered; only the final
``claimed -> attempting`` CAS immediately before ``adapter.send()`` spends a
recovery attempt. A process death while ``claimed`` is therefore replay-safe.
There is still an unavoidable physical interval between that local CAS and the
remote platform call: without a platform idempotency key, no local database can
make those two systems atomic. A death after ``attempting`` is therefore
reported as ambiguous, never advertised as exactly-once delivery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500


class DeliveryLedgerIntegrityError(RuntimeError):
    """Stable obligation identity or checkpoint fencing was violated."""


_TERMINAL_RECOVERY_STATES = {
    "abandoned",
    "delivery_ambiguous",
    "legacy_unfenced",
    "recovery_blocked",
    "superseded",
}

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            session_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT
        )"""
    )
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(delivery_obligations)"
        ).fetchall()
    }
    migrations = {
        "checkpoint_turn_id": "TEXT",
        "checkpoint_revision": "TEXT",
        "checkpoint_content_sha256": "TEXT",
        "content_sha256": "TEXT",
        "attempt_token": "TEXT",
    }
    for name, sql_type in migrations.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE delivery_obligations ADD COLUMN {name} {sql_type}"
            )


def _storage_db_path(
    storage_home: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the trusted ledger namespace without changing global config."""
    if storage_home is None:
        return Path(_db_path())
    return Path(storage_home).expanduser().resolve() / "state.db"


def _storage_namespace(
    storage_home: str | os.PathLike[str] | None = None,
) -> str:
    """Stable namespace returned with claims for follow-up CAS transitions."""
    return str(_storage_db_path(storage_home).parent.resolve())


@contextmanager
def _transaction(
    storage_home: str | os.PathLike[str] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    if storage_home is None:
        # Preserve the monkeypatchable no-argument path used by legacy callers
        # and tests.  A non-default namespace is explicit and trusted.
        conn = _connect()
    else:
        path = _storage_db_path(storage_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            _initialize_schema(conn)
        except Exception:
            conn.close()
            raise
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists. Route through the
        # cross-platform probe: ``os.kill(pid, 0)`` on Windows is NOT a
        # no-op (bpo-14484 — CPython maps sig=0 to
        # ``GenerateConsoleCtrlEvent(0, pid)``), so a raw probe here could
        # Ctrl+C the gateway's own console group whenever psutil failed to
        # read the start time of a live pid. ``_pid_exists`` keeps the
        # EPERM-means-alive semantics (exists but owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                # Never fall back to a raw sig-0 probe on Windows.
                return False
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    session_id: Optional[str] = None,
    checkpoint_turn_id: Optional[str] = None,
    checkpoint_revision: Optional[str] = None,
    checkpoint_content_sha256: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> str:
    """Record an obligation monotonically.

    Returns ``created``, ``already_delivered``, or ``existing_in_flight``.
    Re-recording can never reset state or ownership.  A stable-id collision
    with different immutable content/routing/fence fails closed.  New durable
    rows (those carrying ``session_id``) require all three checkpoint-fence
    fields; only non-durable legacy rows may remain unfenced.
    """
    durable = bool(session_id)
    fence = (
        checkpoint_turn_id,
        checkpoint_revision,
        checkpoint_content_sha256,
    )
    if durable and not all(fence):
        raise DeliveryLedgerIntegrityError(
            "new durable obligations require a complete checkpoint fence"
        )
    if durable and (
        len(str(checkpoint_content_sha256)) != 64
        or any(
            char not in "0123456789abcdefABCDEF"
            for char in str(checkpoint_content_sha256)
        )
    ):
        raise DeliveryLedgerIntegrityError(
            "checkpoint_content_sha256 must be a full SHA-256 digest"
        )

    now = time.time()
    pid, started = _owner_stamp()
    content_sha256 = hashlib.sha256(
        content.encode("utf-8", "replace")
    ).hexdigest()
    if durable and str(checkpoint_content_sha256).lower() != content_sha256:
        raise DeliveryLedgerIntegrityError(
            "durable obligation content does not match its checkpoint digest"
        )
    immutable = (
        str(session_key),
        str(platform),
        str(chat_id),
        str(thread_id) if thread_id else None,
        str(content),
        str(checkpoint_turn_id) if checkpoint_turn_id else None,
        str(checkpoint_revision) if checkpoint_revision else None,
        str(checkpoint_content_sha256) if checkpoint_content_sha256 else None,
        content_sha256,
    )
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                session_id, content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, checkpoint_turn_id,
                checkpoint_revision, checkpoint_content_sha256, content_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(obligation_id) DO NOTHING""",
            (obligation_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None,
             str(session_id) if session_id else None,
             content, now, now, pid, started,
             str(checkpoint_turn_id) if checkpoint_turn_id else None,
             str(checkpoint_revision) if checkpoint_revision else None,
             str(checkpoint_content_sha256)
             if checkpoint_content_sha256 else None,
             content_sha256),
        )
        created = bool(cursor.rowcount)
        row = conn.execute(
            """SELECT session_key, platform, chat_id, thread_id, session_id,
                      content, state, checkpoint_turn_id, checkpoint_revision,
                      checkpoint_content_sha256, content_sha256
               FROM delivery_obligations WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        if row is None:
            raise DeliveryLedgerIntegrityError(
                "obligation insert succeeded but read-back row is missing"
            )
        existing_immutable = (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]) if row[3] else None,
            str(row[5]),
            str(row[7]) if row[7] else None,
            str(row[8]) if row[8] else None,
            str(row[9]) if row[9] else None,
            str(row[10]) if row[10] else hashlib.sha256(
                str(row[5]).encode("utf-8", "replace")
            ).hexdigest(),
        )
        if existing_immutable != immutable:
            raise DeliveryLedgerIntegrityError(
                "stable obligation id collided with different routing, content, or fence"
            )
        existing_session_id = str(row[4]) if row[4] else None
        requested_session_id = str(session_id) if session_id else None
        if existing_session_id != requested_session_id:
            raise DeliveryLedgerIntegrityError(
                "stable obligation id collided across durable sessions"
            )
        outcome = (
            "created"
            if created
            else "already_delivered"
            if str(row[6]) == "delivered"
            else "existing_in_flight"
        )
    _prune(storage_home=storage_home)
    return outcome


def mark_attempting(
    obligation_id: str,
    *,
    attempt_token: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> str:
    """Claim one concrete send attempt and return its durable CAS token.

    The token is generated here unless an explicit test/recovery token is
    supplied.  Only one caller can move a pending/deferred row into the
    attempting state; losers receive an empty string and must not send.
    """
    token = str(attempt_token or secrets.token_hex(16))
    if not token:
        raise ValueError("attempt_token must not be empty")
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='attempting', attempt_token=?, updated_at=?,
                   last_error=NULL
               WHERE obligation_id=? AND state IN ('pending', 'deferred')
                 AND (attempt_token IS NULL OR attempt_token<>?)""",
            (token, time.time(), obligation_id, token),
        )
    return token if cursor.rowcount else ""


def mark_claimed(
    obligation_id: str,
    *,
    attempt_token: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> str:
    """Own a durable send without asserting that network I/O has started.

    This is the producer-side counterpart to ``sweep_recoverable``.  It mints
    the CAS token while leaving ``attempts`` unchanged; callers must finish the
    checkpoint/capability gates and then call ``mark_claimed_attempting`` at the
    actual adapter boundary.  Legacy non-durable rows are intentionally
    rejected so their historical ``mark_attempting`` behavior stays isolated.
    """

    token = str(attempt_token or secrets.token_hex(16))
    if not token:
        raise ValueError("attempt_token must not be empty")
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='claimed', attempt_token=?, owner_pid=?,
                   owner_started_at=?, updated_at=?, last_error=NULL
               WHERE obligation_id=? AND session_id IS NOT NULL
                 AND state IN ('pending', 'deferred')
                 AND (attempt_token IS NULL OR attempt_token<>?)""",
            (token, pid, started, time.time(), obligation_id, token),
        )
    return token if cursor.rowcount else ""


def mark_claimed_attempting(
    obligation_id: str,
    *,
    attempt_token: str,
    storage_home: str | os.PathLike[str] | None = None,
) -> bool:
    """Enter the network-attempt boundary for one durable recovery claim.

    ``sweep_recoverable`` assigns the opaque token while the row is still
    replay-safe in ``claimed``. The recovery runner calls this only after its
    checkpoint/capability gates and immediately before ``adapter.send()``.
    Attempts are spent here, never by the earlier ownership claim.
    """

    token = str(attempt_token or "")
    if not token:
        raise ValueError("attempt_token must not be empty")
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='attempting', attempts=attempts+1, updated_at=?,
                   last_error=NULL
               WHERE obligation_id=? AND session_id IS NOT NULL
                 AND state='claimed' AND attempt_token=?""",
            (time.time(), obligation_id, token),
        )
    return bool(cursor.rowcount)


def mark_delivered(
    obligation_id: str,
    *,
    attempt_token: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> bool:
    return _transition_state(
        obligation_id,
        "delivered",
        from_states={"attempting"},
        attempt_token=attempt_token,
        storage_home=storage_home,
    )


def mark_failed(
    obligation_id: str,
    error: str = "",
    *,
    attempt_token: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> bool:
    return _transition_state(
        obligation_id,
        "failed",
        from_states={"attempting"},
        error=error,
        attempt_token=attempt_token,
        storage_home=storage_home,
    )


def mark_deferred(
    obligation_id: str,
    error: str = "",
    *,
    attempt_token: Optional[str] = None,
    refund_attempt: bool = False,
    storage_home: str | os.PathLike[str] | None = None,
) -> bool:
    """Keep a known-not-attempted delivery recoverable without ambiguity.

    ``refund_attempt`` is used when a claimed replay loses transport before
    the adapter touches the network. Such a no-op must not consume the poison
    row budget.
    """
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='deferred', updated_at=?, last_error=?,
                   attempts=CASE
                       WHEN ? AND state='attempting' AND attempts > 0
                       THEN attempts - 1
                       ELSE attempts
                   END
               WHERE obligation_id=? AND state IN (
                   'pending', 'claimed', 'attempting'
               )
                 AND (
                     (session_id IS NULL AND (? IS NULL OR attempt_token=?))
                     OR
                     (session_id IS NOT NULL AND ? IS NOT NULL AND attempt_token=?)
                 )""",
            (
                time.time(),
                error[:500] if error else None,
                int(bool(refund_attempt)),
                obligation_id,
                attempt_token,
                attempt_token,
                attempt_token,
                attempt_token,
            ),
        )
        return bool(cursor.rowcount)


def send_was_not_attempted(result: Any) -> bool:
    """True only for an adapter's explicit pre-network rejection signal."""
    if isinstance(result, dict):
        raw = result.get("raw_response")
    else:
        raw = getattr(result, "raw_response", None)
    return isinstance(raw, dict) and raw.get("send_attempted") is False


def _transition_state(
    obligation_id: str,
    state: str,
    *,
    from_states: set[str],
    error: str = "",
    attempt_token: Optional[str] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> bool:
    placeholders = ",".join("?" for _ in from_states)
    with _DB_LOCK, _transaction(storage_home) as conn:
        cursor = conn.execute(
            f"""UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=? AND state IN ({placeholders})
                 AND (
                     (session_id IS NULL AND (? IS NULL OR attempt_token=?))
                     OR
                     (session_id IS NOT NULL AND ? IS NOT NULL AND attempt_token=?)
                 )""",
            (
                state,
                time.time(),
                error[:500] if error else None,
                obligation_id,
                *sorted(from_states),
                attempt_token,
                attempt_token,
                attempt_token,
                attempt_token,
            ),
        )
        return bool(cursor.rowcount)


def _checkpoint_fence_disposition(
    *,
    obligation_id: str,
    session_id: str,
    turn_id: str | None,
    revision: str | None,
    content_sha256: str | None,
    platform: str,
    chat_id: str,
    thread_id: str | None,
    storage_home: str | os.PathLike[str] | None = None,
) -> str:
    """Classify a durable obligation against the current checkpoint.

    ``match`` is the sole automatically recoverable disposition.  A complete
    but different fence is superseded by a newer turn.  Missing/corrupt
    checkpoint storage is blocked for operator reconciliation, never guessed.
    """
    if not turn_id or not revision or not content_sha256:
        return "legacy_unfenced"
    try:
        from agent.turn_checkpoint import checkpoint_delivery_fence_matches

        matches = checkpoint_delivery_fence_matches(
            str(session_id),
            turn_id=str(turn_id),
            deliverable_revision=str(revision),
            content_sha256=str(content_sha256),
            obligation_id=str(obligation_id),
            routing={
                "platform": str(platform),
                "chat_id": str(chat_id),
                "thread_id": str(thread_id) if thread_id is not None else "",
            },
            checkpoint_root=(
                Path(_storage_namespace(storage_home))
                / "sessions"
                / "turn-checkpoints"
            ),
        )
    except Exception:
        logger.warning(
            "delivery obligation recovery blocked: checkpoint unreadable "
            "(session=%s)",
            session_id,
            exc_info=True,
        )
        return "recovery_blocked"
    return "match" if matches else "superseded"


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
    include_live_deferred: bool = False,
    max_claims: Optional[int] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> List[Dict[str, Any]]:
    """Claim recoverable rows and return them for redelivery.

    By default only rows owned by dead processes are claimable. The running
    owner may additionally claim its own explicit ``deferred`` rows after the
    platform transport reports recovery.

    Claiming atomically re-stamps the owner to THIS process and assigns a fresh
    ``attempt_token``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp). Durable
    rows stay replay-safe in ``claimed`` and spend ``attempts`` only at their
    final pre-network transition; legacy rows preserve their historical
    claim-as-attempt behavior.
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.
    """
    if max_claims is not None and int(max_claims) < 1:
        raise ValueError("max_claims must be positive when provided")
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    storage_namespace = _storage_namespace(storage_home)
    claimed: List[Dict[str, Any]] = []

    # Snapshot first, then release state.db before consulting checkpoint files.
    # Gateway resealing holds the checkpoint FileLock while it persists the
    # exact recovery artifact to this same database.  Looking up a checkpoint
    # from inside a state.db write transaction would invert that lock order
    # (state.db -> checkpoint versus checkpoint -> state.db) and could turn a
    # healthy obligation into recovery_blocked after the FileLock timeout.
    with _DB_LOCK, _transaction(storage_home) as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      session_id, content, state, attempts, created_at,
                      owner_pid, owner_started_at, checkpoint_turn_id,
                      checkpoint_revision, checkpoint_content_sha256,
                      content_sha256, attempt_token
               FROM delivery_obligations
               WHERE state IN (
                   'pending', 'deferred', 'claimed', 'attempting', 'failed'
               )
               ORDER BY created_at ASC, obligation_id ASC"""
        ).fetchall()

    snapshots: List[Dict[str, Any]] = []
    for values in rows:
        (
            oid,
            session_key,
            platform,
            chat_id,
            thread_id,
            session_id,
            content,
            state,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            checkpoint_turn_id,
            checkpoint_revision,
            checkpoint_content_sha256,
            content_sha256,
            previous_attempt_token,
        ) = values
        actual_content_sha256 = hashlib.sha256(
            str(content).encode("utf-8", "replace")
        ).hexdigest()
        snapshot = {
            "obligation_id": oid,
            "session_key": session_key,
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "session_id": session_id,
            "content": content,
            "state": state,
            "attempts": attempts,
            "created_at": created_at,
            "owner_pid": owner_pid,
            "owner_started_at": owner_started_at,
            "checkpoint_turn_id": checkpoint_turn_id,
            "checkpoint_revision": checkpoint_revision,
            "checkpoint_content_sha256": checkpoint_content_sha256,
            "content_sha256": (
                actual_content_sha256
                if content_sha256 is None
                else str(content_sha256)
            ),
            "backfill_content_sha256": content_sha256 is None,
            "attempt_token": previous_attempt_token,
            "action": "none",
        }

        if content_sha256 is not None and str(content_sha256) != actual_content_sha256:
            snapshot["action"] = "content_corrupt"
            snapshots.append(snapshot)
            continue

        owner_alive = _owner_alive(owner_pid, owner_started_at)
        live_deferred = bool(
            include_live_deferred
            and state == "deferred"
            and owner_alive
            and owner_pid == pid
            and (
                owner_started_at is None
                or started is None
                or int(owner_started_at) == int(started)
            )
        )
        if owner_alive and not live_deferred:
            # A live gateway still owns this non-deferred row.  Preserve a
            # legacy digest backfill, but perform no recovery transition.
            snapshots.append(snapshot)
            continue
        if session_id and state in {"attempting", "failed"}:
            # A durable send crossed (or may have crossed) the adapter
            # boundary.  Never spend a retry budget on an unknowable outcome.
            snapshot["action"] = "delivery_ambiguous"
        elif attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
            snapshot["action"] = "abandoned"
        elif (
            deliverable_platforms is not None
            and platform not in deliverable_platforms
        ):
            # No adapter for this platform this boot — the caller cannot send,
            # so claiming would spend an attempt on a no-op.
            pass
        elif session_id:
            # This is deliberately outside every state.db transaction.  The
            # checkpoint itself has its own cross-process FileLock.
            fence_disposition = _checkpoint_fence_disposition(
                obligation_id=str(oid),
                session_id=str(session_id),
                turn_id=checkpoint_turn_id,
                revision=checkpoint_revision,
                content_sha256=checkpoint_content_sha256,
                platform=str(platform),
                chat_id=str(chat_id),
                thread_id=str(thread_id) if thread_id is not None else None,
                storage_home=storage_namespace,
            )
            snapshot["action"] = (
                "claim" if fence_disposition == "match" else fence_disposition
            )
        else:
            snapshot["action"] = "claim"
        snapshots.append(snapshot)

    def _snapshot_cas_params(snapshot: Dict[str, Any]) -> tuple[Any, ...]:
        return (
            snapshot["obligation_id"],
            snapshot["session_key"],
            snapshot["platform"],
            snapshot["chat_id"],
            snapshot["thread_id"],
            snapshot["session_id"],
            snapshot["state"],
            snapshot["owner_pid"],
            snapshot["owner_started_at"],
            snapshot["attempt_token"],
            snapshot["attempts"],
            snapshot["content"],
            snapshot["content_sha256"],
            snapshot["checkpoint_turn_id"],
            snapshot["checkpoint_revision"],
            snapshot["checkpoint_content_sha256"],
        )

    snapshot_cas_sql = """obligation_id=? AND session_key=? AND platform=?
                         AND chat_id=? AND thread_id IS ? AND session_id IS ?
                         AND state=?
                         AND owner_pid IS ? AND owner_started_at IS ?
                         AND attempt_token IS ? AND attempts=? AND content=?
                         AND content_sha256 IS ? AND checkpoint_turn_id IS ?
                         AND checkpoint_revision IS ?
                         AND checkpoint_content_sha256 IS ?"""

    # Apply every planned transition under one new state.db transaction.  Each
    # update compares the snapshot that was validated, so another gateway that
    # claimed or changed the row while checkpoint I/O was in flight wins and
    # this sweep becomes a no-op for that row.
    with _DB_LOCK, _transaction(storage_home) as conn:
        for snapshot in snapshots:
            if snapshot["backfill_content_sha256"]:
                # Migrated legacy rows did not have a stored digest. Persist
                # the first local observation; all subsequent sweeps compare.
                conn.execute(
                    """UPDATE delivery_obligations SET content_sha256=?
                       WHERE obligation_id=? AND content_sha256 IS NULL
                         AND content=?""",
                    (
                        snapshot["content_sha256"],
                        snapshot["obligation_id"],
                        snapshot["content"],
                    ),
                )

            action = snapshot["action"]
            cas_params = _snapshot_cas_params(snapshot)
            if action == "none":
                continue
            if action == "content_corrupt":
                conn.execute(
                    f"""UPDATE delivery_obligations
                        SET state='recovery_blocked', updated_at=?, last_error=?
                        WHERE {snapshot_cas_sql}""",
                    (
                        now,
                        "stored content digest does not match durable payload",
                        *cas_params,
                    ),
                )
                continue
            if action == "delivery_ambiguous":
                conn.execute(
                    f"""UPDATE delivery_obligations
                        SET state='delivery_ambiguous', updated_at=?, last_error=?
                        WHERE {snapshot_cas_sql}""",
                    (
                        now,
                        "send may have reached the platform; automatic retry blocked",
                        *cas_params,
                    ),
                )
                continue
            if action == "abandoned":
                conn.execute(
                    f"""UPDATE delivery_obligations
                        SET state='abandoned', updated_at=?
                        WHERE {snapshot_cas_sql}""",
                    (now, *cas_params),
                )
                continue
            if action != "claim":
                conn.execute(
                    f"""UPDATE delivery_obligations
                        SET state=?, updated_at=?, last_error=?
                        WHERE {snapshot_cas_sql}""",
                    (
                        action,
                        now,
                        "checkpoint fence did not authorize automatic recovery",
                        *cas_params,
                    ),
                )
                continue

            attempt_token = secrets.token_hex(16)
            durable_claim = bool(snapshot["session_id"])
            claimed_state = "claimed" if durable_claim else "attempting"
            attempt_delta = 0 if durable_claim else 1
            cursor = conn.execute(
                f"""UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+?,
                       state=?, attempt_token=?, updated_at=?
                   WHERE {snapshot_cas_sql}
                     AND (attempt_token IS NULL OR attempt_token<>?)""",
                (
                    pid,
                    started,
                    attempt_delta,
                    claimed_state,
                    attempt_token,
                    now,
                    *cas_params,
                    attempt_token,
                ),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": snapshot["obligation_id"],
                    "session_key": snapshot["session_key"],
                    "platform": snapshot["platform"],
                    "chat_id": snapshot["chat_id"],
                    "thread_id": snapshot["thread_id"],
                    "session_id": snapshot["session_id"],
                    "content": snapshot["content"],
                    "checkpoint_turn_id": snapshot["checkpoint_turn_id"],
                    "checkpoint_revision": snapshot["checkpoint_revision"],
                    "checkpoint_content_sha256": snapshot[
                        "checkpoint_content_sha256"
                    ],
                    "content_sha256": snapshot["content_sha256"],
                    "attempt_token": attempt_token,
                    "storage_home": storage_namespace,
                    # Durable claims are still pre-network and redeliver plainly;
                    # legacy attempting/failed rows retain the visible marker.
                    "needs_marker": (
                        not bool(snapshot["session_id"])
                        and snapshot["state"] not in {"pending", "deferred"}
                    ),
                    "attempts": snapshot["attempts"] + attempt_delta,
                })
                if max_claims is not None and len(claimed) >= int(max_claims):
                    break
    return claimed


def outstanding_session_keys(
    *,
    deliverable_platforms: Optional[set] = None,
    storage_home: str | os.PathLike[str] | None = None,
) -> List[str]:
    """Return sessions whose completed response is owned by the ledger.

    This is deliberately read-only.  Startup uses it to clear stale
    ``resume_pending`` markers *before* any network work without pre-claiming
    every response.  Pre-claiming a batch made rows behind a hung first send
    look ``attempting`` even though their adapters were never called; after a
    restart those untouched rows were then sealed as ambiguous.
    """
    params: list[Any] = []
    platform_sql = ""
    if deliverable_platforms is not None:
        normalized = sorted(str(value) for value in deliverable_platforms)
        if not normalized:
            return []
        platform_sql = " AND platform IN ({})".format(
            ",".join("?" for _ in normalized)
        )
        params.extend(normalized)
    with _DB_LOCK, _transaction(storage_home) as conn:
        rows = conn.execute(
            """SELECT DISTINCT session_key
               FROM delivery_obligations
               WHERE state IN (
                   'pending', 'deferred', 'claimed', 'attempting', 'failed',
                   'delivery_ambiguous', 'recovery_blocked', 'legacy_unfenced'
                 )"""
            + platform_sql
            + " ORDER BY session_key ASC",
            tuple(params),
        ).fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def _prune(
    now: Optional[float] = None,
    *,
    storage_home: str | os.PathLike[str] | None = None,
) -> None:
    """Prune terminal evidence only; active obligations are never cap victims."""
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _DB_LOCK, _transaction(storage_home) as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN (
                       'delivered', 'abandoned', 'delivery_ambiguous',
                       'legacy_unfenced', 'recovery_blocked', 'superseded'
                   ) AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         WHERE state IN (
                           'delivered', 'abandoned', 'delivery_ambiguous',
                           'legacy_unfenced', 'recovery_blocked', 'superseded'
                         )
                         ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'abandoned' THEN 1
                                    ELSE 2
                                  END, updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )
