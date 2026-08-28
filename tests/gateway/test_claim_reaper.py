"""Stale-claim reaper (operator, 28/08 concursa-ai).

Two gateway restarts left four mirror cards claimed-running with dead PIDs
for the full claim TTL; they occupied every max_in_progress slot and
starved the ready queue while the board showed dead work as alive. The
reaper archives dead delegation mirrors, requeues dead dispatcher workers,
and never touches live claims or principal mirrors.
"""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace

import pytest

import gateway.status as gs
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

HOST = socket.gethostname()
DEAD_PID = 999999999


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._active_profile_name = lambda: "default"
    runner._kanban_board_display_targets = lambda profiles: {
        "default": [
            {
                "platform": "telegram",
                "chat_id": "chat-1",
                "thread_id": "topic-7",
                "notifier_profile": "default",
            }
        ]
    }
    runner._claim_reaper_cfg = {"enabled": True, "heartbeat_secs": 600.0}
    return runner


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS = set()
    real_connect = kb.connect
    monkeypatch.setattr(
        kb, "connect", lambda db_path=None, board=None: real_connect()
    )
    kb.init_db()
    return tmp_path


def _make_running(
    *, claim, worker_pid=None, heartbeat_age=None, mirror_comment=False
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="card", assignee="hermes")
        hb = None if heartbeat_age is None else int(time.time() - heartbeat_age)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "claim_expires=?, worker_pid=?, last_heartbeat_at=? "
                "WHERE id=?",
                (claim, int(time.time() + 86400), worker_pid, hb, task_id),
            )
        if mirror_comment:
            kb.add_comment(
                conn, task_id, "hermes",
                "Mirror card for in-process delegation deleg_x task 0, "
                "spawned by hermes.",
            )
    finally:
        conn.close()
    return task_id


def _status(task_id):
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row["status"], row["claim_lock"]
    finally:
        conn.close()


def test_dead_mirror_is_archived_and_dead_worker_requeued(board, monkeypatch):
    monkeypatch.setattr(gs, "_pid_exists", lambda pid: pid != DEAD_PID)
    mirror = _make_running(
        claim=f"{HOST}:{DEAD_PID}", mirror_comment=True, heartbeat_age=120,
    )
    worker = _make_running(
        claim=f"{HOST}:{DEAD_PID}", worker_pid=DEAD_PID, heartbeat_age=120,
    )

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(mirror) == ("archived", None)
    assert _status(worker) == ("ready", None)

    conn = kb.connect()
    try:
        kinds = {
            row["payload"]
            for row in conn.execute(
                "SELECT payload FROM task_events WHERE kind='claim_reaped'",
            ).fetchall()
        }
    finally:
        conn.close()
    assert any("archived_stale_mirror" in payload for payload in kinds)
    assert any("requeued" in payload for payload in kinds)


def test_live_pid_wins_over_stale_heartbeat(board, monkeypatch):
    monkeypatch.setattr(gs, "_pid_exists", lambda pid: True)
    task_id = _make_running(
        claim=f"{HOST}:12345", worker_pid=12345, heartbeat_age=99999,
    )

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(task_id)[0] == "running", (
        "a local claim with a live PID must never be reaped, even with a "
        "stale heartbeat"
    )


def test_foreign_host_reaped_by_heartbeat_only(board, monkeypatch):
    monkeypatch.setattr(
        gs, "_pid_exists",
        lambda pid: (_ for _ in ()).throw(AssertionError("no PID check")),
    )
    stale = _make_running(
        claim="other-host:111", worker_pid=111, heartbeat_age=99999,
    )
    fresh = _make_running(
        claim="other-host:222", worker_pid=222, heartbeat_age=30,
    )

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(stale)[0] == "ready"
    assert _status(fresh)[0] == "running"


def test_dead_dispatcher_claim_with_live_worker_is_untouched(board, monkeypatch):
    """The claim records the dispatcher PID; dispatcher workers are
    independent subprocesses that survive a gateway restart. Reaping on the
    dead dispatcher PID would requeue live work into a duplicate."""
    WORKER_PID = 4242
    monkeypatch.setattr(gs, "_pid_exists", lambda pid: pid == WORKER_PID)
    task_id = _make_running(
        claim=f"{HOST}:{DEAD_PID}", worker_pid=WORKER_PID, heartbeat_age=30,
    )

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(task_id)[0] == "running"


def test_live_dispatcher_with_dead_worker_reaps_on_stale_heartbeat(
    board, monkeypatch
):
    LIVE_DISPATCHER = 5151
    monkeypatch.setattr(gs, "_pid_exists", lambda pid: pid == LIVE_DISPATCHER)
    stale = _make_running(
        claim=f"{HOST}:{LIVE_DISPATCHER}", worker_pid=DEAD_PID,
        heartbeat_age=99999,
    )
    fresh = _make_running(
        claim=f"{HOST}:{LIVE_DISPATCHER}", worker_pid=DEAD_PID,
        heartbeat_age=30,
    )

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(stale)[0] == "ready"
    assert _status(fresh)[0] == "running", (
        "a fresh heartbeat is the grace period for a just-spawned worker"
    )


def test_unclassified_dead_claim_is_left_alone(board, monkeypatch):
    """Principal-turn mirrors (no worker_pid, no delegation comment) keep
    their own idempotent resume path — the reaper must not interfere."""
    monkeypatch.setattr(gs, "_pid_exists", lambda pid: False)
    task_id = _make_running(claim=f"{HOST}:{DEAD_PID}", heartbeat_age=99999)

    asyncio.run(_runner()._kanban_claim_reaper())

    assert _status(task_id)[0] == "running"
