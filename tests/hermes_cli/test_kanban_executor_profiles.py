"""kanban.executor_profiles: executor allowlist plus dispatcher self-heal.

A ready card whose assignee names something this host cannot spawn a worker
for (a retired profile whose directory survives, ``default``, a literal
placeholder such as ``unassigned``) used to sit in ``skipped_nonspawnable``
tick after tick, with no event, no status change and no message. Now:

* with ``kanban.default_assignee`` the dispatcher re-points the row and
  spawns on the same tick, recording an ``assigned`` event with
  ``source="dispatcher.heal"`` and the previous value;
* with ``kanban.executor_profiles`` but no default, the card is parked in
  ``blocked`` with a comment naming the assignee;
* with neither, upstream behaviour is unchanged (terminal lanes keep being
  skipped silently).

``_resolve_executable_assignee`` (creation as ready, promotion, assign on a
ready card) applies the same allowlist: a name outside it resolves to the
default executor instead of being accepted because its directory exists.
"""
from __future__ import annotations

import json
import sys
import tempfile

import pytest


@pytest.fixture()
def kanban_env(monkeypatch):
    """Fresh HERMES_HOME + stubbed config and profile directory lookup.

    ``state["kanban"]`` is what ``load_config()["kanban"]`` returns; tests
    mutate it between creation and dispatch to reproduce rows written before
    the allowlist existed. ``existing`` is the set of profile directories
    on this fake host: ``hnf`` (production executor), ``hpf`` (retired but
    the directory survives) and ``default`` (always "exists" upstream).
    """
    test_home = tempfile.mkdtemp(prefix="kanban_executor_profiles_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import config as hermes_config
    from hermes_cli import kanban_db, profiles

    state = {"kanban": {}}
    monkeypatch.setattr(
        hermes_config, "load_config", lambda *a, **k: {"kanban": dict(state["kanban"])}
    )
    existing = {"hnf", "hpf", "default"}
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in existing)
    return kanban_db, state


def _fake_spawn(*args, **kwargs):
    return 12345


def _make_task(kb, assignee="hpf"):
    """A ready task created while no allowlist is configured."""
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=assignee)
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "ready"
    assert row["assignee"] == assignee
    return task_id


def _set_assignee(kb, task_id, assignee, status=None):
    """Write a legacy row the way the old release did: no validation."""
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
            if status:
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _row(kb, task_id):
    with kb.connect_closing() as conn:
        return dict(conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone())


def _events(kb, task_id, kind):
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ).fetchall()
    return [json.loads(r[0]) if r[0] else {} for r in rows]


def _dispatch(kb, **kwargs):
    with kb.connect_closing() as conn:
        return kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False, **kwargs)


# ---------------------------------------------------------------------------
# dispatcher: heal
# ---------------------------------------------------------------------------

def test_retired_profile_is_healed_to_default_and_spawned(kanban_env):
    """The production case: a card assigned to a profile whose directory
    survives but that is no longer an executor. With the allowlist and a
    default the dispatcher re-points it and spawns on the same tick."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    res = _dispatch(kb, default_assignee="hnf", executor_profiles=["hnf"])

    assert res.healed_assignee == [(task_id, "hpf", "hnf")]
    assert not res.skipped_nonspawnable
    assert not res.blocked_nonspawnable
    assert [(s[0], s[1]) for s in res.spawned] == [(task_id, "hnf")]
    assert _row(kb, task_id)["assignee"] == "hnf"
    assigned = _events(kb, task_id, "assigned")
    assert len(assigned) == 1
    assert assigned[0]["assignee"] == "hnf"
    assert assigned[0]["source"] == "dispatcher.heal"
    assert assigned[0]["previous"] == "hpf"
    assert assigned[0]["lane"] == "ready"


def test_default_and_placeholder_assignees_are_healed(kanban_env):
    """``default`` always "exists" upstream; a placeholder never does. Both
    are re-pointed: the first because of the allowlist, the second even
    without one (no directory, so no worker could ever start)."""
    kb, state = kanban_env
    t_default = _make_task(kb, "hpf")
    _set_assignee(kb, t_default, "default")
    with kb.connect_closing() as conn:
        t_placeholder = kb.create_task(conn, title="t2", assignee="hpf")
    _set_assignee(kb, t_placeholder, "unassigned")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    res = _dispatch(kb, default_assignee="hnf", executor_profiles=["hnf"])

    assert sorted(res.healed_assignee) == sorted([
        (t_default, "default", "hnf"),
        (t_placeholder, "unassigned", "hnf"),
    ])
    assert sorted(s[0] for s in res.spawned) == sorted([t_default, t_placeholder])
    assert _row(kb, t_default)["assignee"] == "hnf"
    assert _row(kb, t_placeholder)["assignee"] == "hnf"


def test_placeholder_is_healed_without_allowlist_when_default_is_set(kanban_env):
    """No allowlist, only ``default_assignee``: a name with no directory is
    still re-pointed instead of rotting in ``skipped_nonspawnable``."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    _set_assignee(kb, task_id, "unassigned")
    state["kanban"] = {"default_assignee": "hnf"}

    res = _dispatch(kb, default_assignee="hnf")

    assert res.healed_assignee == [(task_id, "unassigned", "hnf")]
    assert not res.skipped_nonspawnable
    assert _row(kb, task_id)["assignee"] == "hnf"


def test_executor_assignee_is_left_alone(kanban_env):
    """A card already on an executor is dispatched untouched: no heal
    bucket, no ``assigned`` event."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hnf")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    res = _dispatch(kb, default_assignee="hnf", executor_profiles=["hnf"])

    assert not res.healed_assignee
    assert [(s[0], s[1]) for s in res.spawned] == [(task_id, "hnf")]
    assert _events(kb, task_id, "assigned") == []


def test_dry_run_reports_heal_without_writing(kanban_env):
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            default_assignee="hnf", executor_profiles=["hnf"],
        )

    assert res.healed_assignee == [(task_id, "hpf", "hnf")]
    assert [(s[0], s[1]) for s in res.spawned] == [(task_id, "hnf")]
    assert _row(kb, task_id) == {"status": "ready", "assignee": "hpf"}
    assert _events(kb, task_id, "assigned") == []


def test_review_lane_is_healed_too(kanban_env):
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    _set_assignee(kb, task_id, "hpf", status="review")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    res = _dispatch(kb, default_assignee="hnf", executor_profiles=["hnf"])

    assert res.healed_assignee == [(task_id, "hpf", "hnf")]
    assert [(s[0], s[1]) for s in res.spawned] == [(task_id, "hnf")]
    assigned = _events(kb, task_id, "assigned")
    assert assigned and assigned[-1]["lane"] == "review"
    assert _row(kb, task_id)["assignee"] == "hnf"


# ---------------------------------------------------------------------------
# dispatcher: park or skip
# ---------------------------------------------------------------------------

def test_allowlist_without_default_parks_the_card_with_a_comment(kanban_env):
    """The operator declared the executors but no default: nobody will pull
    this card by hand, so it goes to ``blocked`` with a visible reason."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    state["kanban"] = {"executor_profiles": ["hnf"]}

    res = _dispatch(kb, executor_profiles=["hnf"])

    assert res.blocked_nonspawnable == [(task_id, "hpf")]
    assert not res.spawned
    assert not res.healed_assignee
    assert _row(kb, task_id) == {"status": "blocked", "assignee": "hpf"}
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, task_id)
    assert len(comments) == 1
    assert "'hpf'" in comments[0].body
    assert "executor" in comments[0].body

    # A parked card is not touched again on the next tick.
    res2 = _dispatch(kb, executor_profiles=["hnf"])
    assert not res2.blocked_nonspawnable
    assert not res2.skipped_nonspawnable


def test_without_default_or_allowlist_upstream_skip_is_unchanged(kanban_env):
    """No default, no allowlist: a control-plane lane pulled by a terminal.
    The row must not be mutated and no event must be written."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    _set_assignee(kb, task_id, "orion-cc")
    state["kanban"] = {}

    res = _dispatch(kb)

    assert res.skipped_nonspawnable == [task_id]
    assert not res.healed_assignee
    assert not res.blocked_nonspawnable
    assert not res.spawned
    assert _row(kb, task_id) == {"status": "ready", "assignee": "orion-cc"}
    assert _events(kb, task_id, "assigned") == []


def test_default_outside_allowlist_does_not_heal(kanban_env):
    """A misconfigured default (not on the allowlist) must not become the
    target of the heal: the card is parked and the operator sees why."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    _set_assignee(kb, task_id, "default")
    state["kanban"] = {"default_assignee": "hpf", "executor_profiles": ["hnf"]}

    res = _dispatch(kb, default_assignee="hpf", executor_profiles=["hnf"])

    assert not res.healed_assignee
    assert res.blocked_nonspawnable == [(task_id, "default")]
    assert _row(kb, task_id)["assignee"] == "default"


# ---------------------------------------------------------------------------
# resolution at creation / promotion / assign
# ---------------------------------------------------------------------------

def test_resolve_executable_assignee_honours_the_allowlist(kanban_env):
    kb, state = kanban_env
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}

    assert kb._resolve_executable_assignee("hnf") == "hnf"
    assert kb._resolve_executable_assignee("hpf") == "hnf"
    assert kb._resolve_executable_assignee("default") == "hnf"
    assert kb._resolve_executable_assignee("unassigned") == "hnf"
    assert kb._resolve_executable_assignee("") == "hnf"

    state["kanban"] = {"executor_profiles": ["hnf"]}
    with pytest.raises(ValueError, match="not an executor"):
        kb._resolve_executable_assignee("hpf")

    state["kanban"] = {"default_assignee": "hpf", "executor_profiles": ["hnf"]}
    with pytest.raises(ValueError, match="not an executor"):
        kb._resolve_executable_assignee("default")


def test_resolve_without_allowlist_keeps_upstream_rule(kanban_env):
    kb, state = kanban_env
    state["kanban"] = {"default_assignee": "hnf"}

    assert kb._resolve_executable_assignee("hpf") == "hpf"
    assert kb._resolve_executable_assignee("unassigned") == "hnf"
    with pytest.raises(ValueError, match="does not exist"):
        kb._resolve_executable_assignee("ghost")


def test_create_task_outside_allowlist_lands_on_the_default_executor(kanban_env):
    kb, state = kanban_env
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="hpf")
    assert _row(kb, task_id) == {"status": "ready", "assignee": "hnf"}


def test_assign_on_ready_task_outside_allowlist_repoints(kanban_env):
    kb, state = kanban_env
    task_id = _make_task(kb, "hnf")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}
    with kb.connect_closing() as conn:
        assert kb.assign_task(conn, task_id, "hpf") is True
    assert _row(kb, task_id)["assignee"] == "hnf"


def test_promotion_from_todo_repoints_with_previous_value(kanban_env):
    """``_ensure_ready_assignee`` runs inside the promotion; the event keeps
    the value the row carried while it waited."""
    kb, state = kanban_env
    task_id = _make_task(kb, "hpf")
    _set_assignee(kb, task_id, "hpf", status="todo")
    state["kanban"] = {"default_assignee": "hnf", "executor_profiles": ["hnf"]}
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._ensure_ready_assignee(conn, task_id)
    assert _row(kb, task_id)["assignee"] == "hnf"
    assigned = _events(kb, task_id, "assigned")
    assert assigned[-1] == {
        "assignee": "hnf", "source": "kanban.executor_profiles", "previous": "hpf",
    }
