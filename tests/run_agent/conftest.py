"""Fast-path fixtures shared across tests/run_agent/.

Many tests in this directory exercise the retry/backoff paths in the
agent loop. Production code uses ``jittered_backoff(base_delay=5.0)``
with a ``while time.time() < sleep_end`` loop — a single retry test
spends 5+ seconds of real wall-clock time on backoff waits.

Mocking ``jittered_backoff`` to return 0.0 collapses the while-loop
to a no-op (``time.time() < time.time() + 0`` is false immediately),
which handles the most common case without touching ``time.sleep``.

We deliberately DO NOT mock ``time.sleep`` here — some tests
(test_interrupt_propagation, test_primary_runtime_restore, etc.) use
the real ``time.sleep`` for threading coordination or assert that it
was called with specific values. Tests that want to additionally
fast-path direct ``time.sleep(N)`` calls in production code should
monkeypatch ``run_agent.time.sleep`` locally (see
``test_anthropic_error_handling.py`` for the pattern).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Short-circuit retry backoff for all tests in this directory."""
    try:
        import run_agent
    except ImportError:
        return

    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    # The conversation loop was extracted out of run_agent.py into
    # ``agent.conversation_loop``, which imports ``jittered_backoff``
    # directly (``from agent.retry_utils import jittered_backoff``).
    # Patching ``run_agent.jittered_backoff`` alone misses every retry
    # path under the new module — tests that exercise rate-limit /
    # invalid-response / server-error retries burn real wall-clock
    # seconds per retry. Patch both for full coverage.
    try:
        from agent import conversation_loop as _conv_loop
        monkeypatch.setattr(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _close_session_db_handles(monkeypatch):
    """Close SessionDB handles before a TemporaryDirectory removes its files.

    POSIX permits unlinking an open SQLite database; Windows correctly rejects
    it with WinError 32. Tests own these databases, so close matching handles at
    the directory lifecycle boundary without weakening production locking.
    """
    from hermes_state import SessionDB

    original_init = SessionDB.__init__
    original_cleanup = tempfile.TemporaryDirectory.cleanup
    created = []

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    def cleanup_with_closed_databases(temp_dir):
        root = Path(temp_dir.name).resolve(strict=False)
        for db in reversed(created):
            db_path = Path(getattr(db, "db_path", "")).resolve(strict=False)
            try:
                db_path.relative_to(root)
            except ValueError:
                continue
            try:
                db.close()
            except Exception:
                pass
        return original_cleanup(temp_dir)

    monkeypatch.setattr(SessionDB, "__init__", tracked_init)
    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        "cleanup",
        cleanup_with_closed_databases,
    )
    try:
        yield
    finally:
        for db in reversed(created):
            try:
                db.close()
            except Exception:
                pass
