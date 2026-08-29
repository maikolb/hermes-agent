"""Active-PR guard completes the canonical cycle — automerge stays.

Operator contract (29/08, reaffirmed verbatim): branch → PR → merge →
delete-branch, fully autonomous; a green PR merges itself and a parked
PR is a failure. Hardening from the adversarial review applies only
where it does not park a PR: same-PR resolution cache, empty rollup is
never green, head-commit pinning, method fallback, visible refusals.
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

GREEN = {
    "state": "OPEN",
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    "headRefOid": "abc123def456",
}


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
    merged: list[tuple[str, str]] = []
    monkeypatch.setattr(kb, "_gh_pr_json", lambda url: dict(GREEN))
    monkeypatch.setattr(
        kb, "_gh_pr_merge",
        lambda url, head_oid="": merged.append((url, head_oid)) or (True, "--merge"),
    )

    assert kb.check_respawn_guard(conn, task_id) is None
    # merge targeted the cited PR AND pinned the validated head commit
    assert merged == [(PR_URL, "abc123def456")]
    events = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='pr_automerged'",
        (task_id,),
    ).fetchall()
    assert len(events) == 1
    conn.close()


@pytest.mark.parametrize("head", ["staging", "production", "release/2026.09"])
def test_release_train_head_is_never_automerged(board, monkeypatch, head):
    """A green train PR cited on a card must NOT ship to production as a
    guard side effect. Denylist extended per reviewer round (29/08)."""
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(
        kb, "_gh_pr_json", lambda url: {**GREEN, "headRefName": head}
    )
    monkeypatch.setattr(
        kb, "_gh_pr_merge",
        lambda url, head_oid="": (_ for _ in ()).throw(AssertionError("must not merge")),
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    conn.close()


def test_empty_check_rollup_is_not_green(board, monkeypatch):
    """Reviewer counterexample: a repo with no CI must not merge blind."""
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(
        kb, "_gh_pr_json", lambda url: {**GREEN, "statusCheckRollup": []}
    )
    monkeypatch.setattr(
        kb, "_gh_pr_merge",
        lambda url, head_oid="": (_ for _ in ()).throw(AssertionError("must not merge")),
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
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
        lambda url, head_oid="": (_ for _ in ()).throw(AssertionError("must not merge")),
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


def test_stale_resolution_for_other_pr_does_not_release(board, monkeypatch):
    """Reviewer counterexample: one old pr_resolved event must not release
    every FUTURE PR on the card with zero fresh lookups."""
    conn, task_id = _task_with_pr(monkeypatch)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'pr_resolved', ?, strftime('%s','now')-50)",
            (task_id, json.dumps({"pr": "https://github.com/acme/app/pull/1",
                                  "state": "MERGED"})),
        )
    calls: list[str] = []
    monkeypatch.setattr(
        kb, "_gh_pr_json",
        lambda url: calls.append(url) or {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
        },
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    assert calls == [PR_URL]  # fresh lookup happened for the NEW pr
    conn.close()


def test_merge_refusal_records_visible_event_once(board, monkeypatch):
    """The old behavior swallowed gh failures; a refused merge now records
    pr_merge_refused (once per PR) and retries on later ticks."""
    conn, task_id = _task_with_pr(monkeypatch)
    monkeypatch.setattr(kb, "_gh_pr_json", lambda url: dict(GREEN))
    monkeypatch.setattr(
        kb, "_gh_pr_merge",
        lambda url, head_oid="": (False, "GraphQL: Base branch is protected"),
    )

    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    assert kb.check_respawn_guard(conn, task_id) == "active_pr"
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_merge_refused'",
        (task_id,),
    ).fetchall()
    assert len(events) == 1
    assert "protected" in events[0]["payload"]
    conn.close()


def test_gh_pr_merge_pins_head_and_falls_back_on_method(board, monkeypatch):
    """Unit contract of _gh_pr_merge: pins --match-head-commit and falls
    back --merge → --squash when the repo refuses the merge METHOD
    (linear-history repos used to park PRs forever)."""
    calls: list[list[str]] = []

    class P:
        def __init__(self, rc, err=""):
            self.returncode = rc
            self.stderr = err
            self.stdout = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        if "--merge" in argv:
            return P(1, "Pull request merge method 'merge' is not allowed")
        return P(0)

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    ok, detail = kb._gh_pr_merge(PR_URL, "abc123")
    assert ok is True and detail == "--squash"
    assert all("--match-head-commit" in c and "abc123" in c for c in calls)
    assert ["--merge" in c for c in calls].count(True) == 1


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
