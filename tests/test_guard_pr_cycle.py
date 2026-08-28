"""Active-PR guard completes the canonical cycle (operator, 28/08).

The ecosystem's flow is branch → PR → merge → delete-branch, fully
autonomous. The guard exists to prevent duplicate PRs, not to park a
card behind an orphaned green PR (Wave 4 do DOVCRM: 7.6 days ready).
"""

from __future__ import annotations

import json
import time

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_AUTO_MERGE_ACTIVE_PR", raising=False)
    kb._INITIALIZED_PATHS = set()
    kb.init_db()
    return tmp_path


PR_URL = "https://github.com/acme/app/pull/38"


def _task_with_pr(monkeypatch):
    conn = kb.connect()
    task_id = kb.create_task(conn, title="wave", assignee="hermes")
    kb.add_comment(conn, task_id, "worker", f"PR aberta: {PR_URL}")
    return conn, task_id


def test_merged_pr_releases_guard_and_records_event(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(kb, "_gh_pr_json", lambda url: {"state": "MERGED"})

    assert kb.check_respawn_guard(conn, task_id) is None
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? AND kind='pr_resolved'",
        (task_id,),
    ).fetchall()
    assert len(events) == 1
    assert PR_URL in events[0]["payload"]
    # second pass: resolved event short-circuits, no network call
    monkeypatch.setattr(
        kb, "_gh_pr_json", lambda url: (_ for _ in ()).throw(AssertionError("network"))
    )
    assert kb.check_respawn_guard(conn, task_id) is None
    conn.close()


def test_open_green_mergeable_pr_is_merged_and_released(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    merged: list[str] = []
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
    )
    monkeypatch.setattr(kb, "_gh_pr_merge", lambda url: merged.append(url) or True)

    assert kb.check_respawn_guard(conn, task_id) is None
    assert merged == [PR_URL]
    events = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='pr_automerged'",
        (task_id,),
    ).fetchall()
    assert len(events) == 1
    conn.close()


def test_red_or_conflicting_pr_keeps_holding(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
        },
    )
    monkeypatch.setattr(
        kb, "_gh_pr_merge",
        lambda url: (_ for _ in ()).throw(AssertionError("must not merge")),
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    conn.close()


def test_gh_failure_keeps_previous_behavior(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(kb, "_gh_pr_json", lambda url: None)

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    conn.close()


def test_kill_switch_disables_resolution(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_AUTO_MERGE_ACTIVE_PR", "off")
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    conn.close()


def test_rework_requested_after_pr_releases_guard(board, monkeypatch):
    """Wave 4 do DOVCRM: reviewer pediu mudanças DEPOIS da PR aberta; o
    respawn é o ciclo de correção na MESMA branch/PR, não duplicação."""
    conn, task_id = _task_with_pr(monkeypatch)
    time.sleep(1.1)
    kb.add_comment(
        conn, task_id, "hermes-project-factory",
        "Changes requested (review round 1): corrigir autorização no store.",
    )
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )

    assert kb.check_respawn_guard(conn, task_id) is None
    conn.close()


def test_formal_changes_requested_event_releases_guard(board, monkeypatch):
    conn, task_id = _task_with_pr(monkeypatch)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'changes_requested', '{}', strftime('%s','now')+2)",
            (task_id,),
        )
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )

    assert kb.check_respawn_guard(conn, task_id) is None
    conn.close()
