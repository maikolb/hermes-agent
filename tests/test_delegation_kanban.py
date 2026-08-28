"""Tests for the delegation → kanban mirror-card side channel."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def kanban_env(tmp_path: Path, monkeypatch):
    """Pin the kanban DB to a fresh temp file (HERMES_KANBAN_DB override)."""
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    # Reset the module-level init cache so each test gets a fresh schema.
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS = set()
    return db


def _get_task(task_id: str):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def test_create_cards_are_running_and_claimed(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards(
        [
            {"goal": "Analisar visual do video", "context": "reels bug"},
            {"goal": "Auditar UI/UX"},
        ],
        "deleg_test01",
        "default",
        live_paths=["/tmp/task-0.log", "/tmp/task-1.log"],
    )

    assert sorted(cards) == [0, 1]
    for index, task_id in cards.items():
        task = _get_task(task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock, "mirror card must hold a claim"

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, cards[0])
    assert any("deleg_test01" in c.body for c in comments)
    assert any("task-0.log" in c.body for c in comments)


def test_create_cards_idempotent_per_delegation(kanban_env):
    from tools import delegation_kanban as dk

    first = dk.create_delegation_cards([{"goal": "G"}], "deleg_same", "default")
    second = dk.create_delegation_cards([{"goal": "G"}], "deleg_same", "default")
    assert first[0] == second[0]


def test_close_cards_completed_becomes_done_with_summary(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_ok", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        # "completed" is the real terminal status stamped by
        # _execute_and_aggregate (regression: the smoke on the dovcrm board
        # blocked both cards because the closer only accepted "ok").
        [{"task_index": 0, "status": "completed", "summary": "entreguei X e Y"}],
    )
    task = _get_task(cards[0])
    assert task.status == "done"


def test_close_cards_failure_blocks_for_human(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_bad", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        [{"task_index": 0, "status": "failed", "error": "boom", "summary": ""}],
    )
    task = _get_task(cards[0])
    assert task.status == "blocked"


def test_close_cards_interrupted_blocks_for_human(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_int", "default")
    dk.close_delegation_cards(
        "default",
        cards,
        [{"task_index": 0, "status": "interrupted", "summary": "parcial"}],
    )
    task = _get_task(cards[0])
    assert task.status == "blocked"


def test_dispatch_cards_are_ready_and_unclaimed(kanban_env):
    from tools import delegation_kanban as dk

    cards = dk.create_dispatch_cards(
        [{"goal": "Analisar backlog", "context": "rota dispatcher"}],
        "default",
        delegation_id="deleg_route01",
    )

    assert sorted(cards) == [0]
    task = _get_task(cards[0])
    assert task.status == "ready"
    assert not task.claim_lock

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, cards[0])
    assert any("route_to_dispatcher" in c.body for c in comments)


def test_mirror_heartbeat_beats_running_cards(kanban_env):
    import time as _time

    from tools import delegation_kanban as dk

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_hb", "default")
    beat = dk.start_mirror_heartbeat("default", cards, interval=1.0)
    assert beat is not None
    try:
        deadline = _time.time() + 5
        heartbeat = None
        while _time.time() < deadline:
            heartbeat = _get_task(cards[0]).last_heartbeat_at
            if heartbeat:
                break
            _time.sleep(0.2)
        assert heartbeat, "mirror heartbeat must touch last_heartbeat_at"
    finally:
        beat.stop()


def test_mirror_heartbeat_none_without_board_or_cards(kanban_env):
    from tools import delegation_kanban as dk

    assert dk.start_mirror_heartbeat(None, {0: "t_x"}) is None
    assert dk.start_mirror_heartbeat("default", {}) is None


def test_route_is_strictly_opt_in_but_mirror_inherits(monkeypatch):
    """TARGET_ARCHITECTURE gap 9: routing changes delegation semantics, so it
    never turns itself on by inheritance; the principal-turn mirror is pure
    visibility and may keep inheriting worker_rotation."""
    from tools import delegation_kanban as dk

    monkeypatch.setattr(
        "tools.delegate_tool._load_config", lambda: {}, raising=True
    )
    monkeypatch.setattr(
        dk, "_display_worker_rotation", lambda: True, raising=True
    )
    assert dk.route_to_dispatcher_enabled() is False
    assert dk.mirror_principal_turns_enabled() is True

    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"route_to_dispatcher": True, "mirror_principal_turns": False},
        raising=True,
    )
    assert dk.route_to_dispatcher_enabled() is True
    assert dk.mirror_principal_turns_enabled() is False


def test_principal_mirror_lifecycle(kanban_env):
    import asyncio
    import time as _time

    from tools.principal_turn_mirror import PrincipalTurnMirror

    mirror = PrincipalTurnMirror("default", "auditar fluxo de pagamentos")

    asyncio.run(mirror.tick(30.0))
    assert mirror._task_id is None, "below threshold must not create a card"

    asyncio.run(mirror.tick(61.0))
    assert mirror._task_id, "past threshold must create the mirror card"
    task = _get_task(mirror._task_id)
    assert task.status == "running"
    assert task.claim_lock
    assert task.title.startswith("Principal: ")

    asyncio.run(mirror.tick(121.0))
    assert _get_task(mirror._task_id).last_heartbeat_at

    mirror.finish(180.0)
    deadline = _time.time() + 5
    status = None
    while _time.time() < deadline:
        status = _get_task(mirror._task_id).status
        if status == "done":
            break
        _time.sleep(0.2)
    assert status == "done"


def test_principal_mirror_resume_reclaims_same_card(kanban_env):
    import asyncio
    import time as _time

    from hermes_cli import kanban_db as kb
    from tools.principal_turn_mirror import PrincipalTurnMirror

    key = "principal:telegram:-1001:m42"
    interrupted = PrincipalTurnMirror("default", "corrigir pipeline", idempotency_key=key)
    asyncio.run(interrupted.tick(61.0))
    orphan_id = interrupted._task_id
    assert orphan_id
    assert _get_task(orphan_id).status == "running"
    # No finish(): the gateway died here and left the claim in place.

    resumed = PrincipalTurnMirror("default", "corrigir pipeline", idempotency_key=key)
    asyncio.run(resumed.tick(61.0))

    assert resumed._task_id == orphan_id, "resume must land on the SAME card"
    assert resumed.resumed is True
    task = _get_task(orphan_id)
    assert task.status == "running"
    assert task.claim_lock
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, orphan_id)
    assert any("RESUMED" in c.body for c in comments)

    resumed.finish(240.0)
    deadline = _time.time() + 5
    status = None
    while _time.time() < deadline:
        status = _get_task(orphan_id).status
        if status == "done":
            break
        _time.sleep(0.2)
    assert status == "done"


def test_no_board_creates_nothing(kanban_env):
    from tools import delegation_kanban as dk

    assert dk.create_delegation_cards([{"goal": "G"}], "deleg_x", None) == {}
    # Closing with no cards is a no-op rather than an error.
    dk.close_delegation_cards(None, {}, [{"task_index": 0, "status": "completed"}])


def test_mirror_cards_inherit_origin_scope_and_subscription(kanban_env, monkeypatch):
    """Acceptance finding (28/08, DOVTest): mirror cards carried no notify
    sub and no project/session scope, so rotation, focus and per-worker
    closeouts were structurally blind to in-process fan-outs."""
    from gateway.session_context import set_session_vars
    from tools import delegation_kanban as dk
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    # create_task validates project_id against projects_db and drops
    # dangling references; in production the auto-provisioned project row
    # exists. Simulate that here (id + no primary repo → scratch workspace).
    from types import SimpleNamespace

    monkeypatch.setattr(
        pdb,
        "get_project",
        lambda conn, pid: (
            SimpleNamespace(id="dovtest", primary_path=None)
            if pid == "dovtest"
            else None
        ),
    )

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1004309874643",
        chat_type="supergroup",
        chat_name="Nexa Factory",
        thread_id="6321",
        user_id="996979567",
        user_id_alt="",
        user_name="Maikol",
        scope_id="",
        session_key="agent:main:telegram:-1004309874643:6321",
        message_id="1",
        profile="hermes-project-factory",
        async_delivery=True,
        cron_session="",
        project_id="dovtest",
        project_board="default",
        project_workdir="",
        project_access="allow",
    )
    try:
        cards = dk.create_delegation_cards(
            [{"goal": "Executar T1"}], "deleg_origin", "default"
        )
    finally:
        for token in tokens:
            try:
                token.var.reset(token)
            except Exception:
                pass

    task = _get_task(cards[0])
    assert task.project_id == "dovtest"
    with kb.connect_closing() as conn:
        subs = conn.execute(
            "SELECT platform, chat_id, thread_id FROM kanban_notify_subs "
            "WHERE task_id = ?", (cards[0],)
        ).fetchall()
    assert [tuple(row) for row in subs] == [
        ("telegram", "-1004309874643", "6321")
    ]


def test_mirror_cards_without_origin_behave_as_before(kanban_env):
    from tools import delegation_kanban as dk
    from hermes_cli import kanban_db as kb

    cards = dk.create_delegation_cards([{"goal": "G"}], "deleg_plain", "default")
    with kb.connect_closing() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
            (cards[0],),
        ).fetchone()[0]
    assert count == 0
