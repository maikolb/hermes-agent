"""SPAWN_GUARD_20261016: _default_spawn refuses a worker whose profile cannot run its own contract."""
from __future__ import annotations

import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.real_spawn_guard


def _task(assignee):
    return kb.Task(
        id="t_guard", title="guard", body=None, assignee=assignee, status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None, tenant=None, current_run_id=7,
    )


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    kb._PROFILE_REFUSED.clear()
    monkeypatch.setattr(kb, "_PROBE_TIMEOUT_SECONDS", 300)  # the real probe took 12-21 s on a loaded box; only a hung one may time out
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    launched = []

    class Proc:
        pid = 4242

    real_popen = subprocess.Popen
    # Only the worker command is faked; the plugin probe (a python child) runs for real.
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: (launched.append(cmd) or Proc()) if cmd[0] == "hermes" else real_popen(cmd, *a, **k))
    (tmp_path / "ws").mkdir()

    def spawn(assignee):
        return kb._default_spawn(_task(assignee), str(tmp_path / "ws"))

    def profile(name, config):
        home = root / "profiles" / name
        home.mkdir()
        if config is not None:
            home.joinpath("config.yaml").write_text(config, encoding="utf-8")
        return home

    return spawn, profile, launched, root


def test_normal_profile_still_launches(host):
    spawn, profile, launched, _ = host
    profile("ok", "toolsets:\n  - hermes-cli\n")
    assert spawn("ok") == 4242 and launched


def test_profile_without_config_is_refused(host):
    spawn, profile, launched, _ = host
    profile("bare", None)
    with pytest.raises(kb.WorkerProfileRefused, match="no readable config.yaml"):
        spawn("bare")
    assert not launched


def test_profile_outside_the_executor_list_is_refused(host):
    spawn, profile, launched, root = host
    root.joinpath("config.yaml").write_text("kanban:\n  executor_profiles: [good]\n", encoding="utf-8")
    profile("good", "toolsets:\n  - hermes-cli\n")
    profile("legacy", "toolsets:\n  - hermes-cli\n")
    with pytest.raises(ValueError, match="not an executor"):
        spawn("legacy")
    assert spawn("good") == 4242 and len(launched) == 1


def test_enabled_plugin_that_does_not_load_is_refused(host):
    spawn, profile, launched, _ = host
    home = profile("plug", "plugins:\n  enabled: [absent-plugin]\n")
    with pytest.raises(ValueError, match="absent-plugin"):
        spawn("plug")
    assert not launched
    # the same profile with a plugin that does load passes
    plugin = home / "plugins" / "present-plugin"
    plugin.mkdir(parents=True)
    plugin.joinpath("plugin.yaml").write_text("name: present-plugin\nversion: '1'\n", encoding="utf-8")
    plugin.joinpath("__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    home.joinpath("config.yaml").write_text("plugins:\n  enabled: [present-plugin]\n", encoding="utf-8")
    assert spawn("plug") == 4242 and len(launched) == 1


def _cfg(*lines):
    return "\n".join(lines) + "\n"


PLUG = _cfg("plugins:", "  enabled: [absent-plugin]")
PRESENT = _cfg("plugins:", "  enabled: [present-plugin]")


def _count_probes(monkeypatch):
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: (calls.append(cmd), real_run(cmd, *a, **k))[1])
    return calls


def _present_plugin(home, extra=""):
    plugin = home / "plugins" / "present-plugin"
    plugin.mkdir(parents=True, exist_ok=True)
    plugin.joinpath("plugin.yaml").write_text(_cfg("name: present-plugin", "version: '1'"), encoding="utf-8")
    plugin.joinpath("__init__.py").write_text(_cfg("def register(ctx):", "    pass") + extra, encoding="utf-8")


def test_probe_that_cannot_run_is_refused_fail_closed(host, monkeypatch):
    spawn, profile, launched, _ = host
    profile("plug", PRESENT)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)))
    with pytest.raises(kb.WorkerProfileRefused, match="Could not verify"):
        spawn("plug")
    assert not launched


def test_success_is_cached_across_processes_and_invalidated_by_changes(host, monkeypatch):
    spawn, profile, launched, _ = host
    home = profile("plug", PRESENT)
    _present_plugin(home)
    calls = _count_probes(monkeypatch)
    assert spawn("plug") == 4242 and len(calls) == 1
    kb._PROFILE_REFUSED.clear()  # a new process keeps nothing in memory; only the file persists
    assert spawn("plug") == 4242 and len(calls) == 1  # file cache hit
    home.joinpath("config.yaml").write_text(_cfg("plugins:", "  enabled: [present-plugin, absent-plugin]"), encoding="utf-8")
    with pytest.raises(kb.WorkerProfileRefused, match="absent-plugin"):
        spawn("plug")
    assert len(calls) == 2  # config change invalidated it
    home.joinpath("config.yaml").write_text(PRESENT, encoding="utf-8")
    kb._PROFILE_REFUSED.clear()
    assert spawn("plug") == 4242 and len(calls) == 2  # same state as the cached success: still a hit
    _present_plugin(home, extra="# edit\n")
    assert spawn("plug") == 4242 and len(calls) == 3  # plugin file change invalidated it


def test_safe_mode_and_disabled_plugin_are_not_probed(host, monkeypatch):
    spawn, profile, launched, _ = host
    profile("plug", PLUG)
    profile("off", _cfg("plugins:", "  enabled: [absent-plugin]", "  disabled: [absent-plugin]"))
    calls = _count_probes(monkeypatch)
    assert spawn("off") == 4242  # disabled on purpose: off, not broken
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")
    assert spawn("plug") == 4242  # operator turned plugins off for this process
    assert len(launched) == 2 and len(calls) == 1


def test_dispatch_once_refusal_keeps_the_card_ready_with_one_event(host, tmp_path):
    spawn, profile, launched, root = host
    home = profile("plug", PLUG)
    kb.init_db()
    conn = kb.connect()
    tid = kb.create_task(conn, title="t", assignee="plug")
    for _ in range(5):
        res = kb.dispatch_once(conn, failure_limit=2)
        assert not res.spawned and not res.auto_blocked
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.consecutive_failures == 0
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert kinds.count("spawn_refused") == 1 and "gave_up" not in kinds and "spawn_failed" not in kinds
    assert not launched
    # the profile gets fixed: the very next tick launches it
    _present_plugin(home)
    home.joinpath("config.yaml").write_text(PRESENT, encoding="utf-8")
    res = kb.dispatch_once(conn, failure_limit=2)
    assert [s[0] for s in res.spawned] == [tid] and launched


def test_request_bootstrap_refusal_runs_before_the_attach_and_requeues_the_request(host):
    import json

    from hermes_cli import nfos_runtime

    spawn, profile, launched, _ = host
    profile("plug", PLUG)
    kb.init_db()
    payload = {"project": {"profile": "plug", "delivery_type": "code"}, "source": {}, "text": "do it", "attachments": []}
    with kb.connect_closing() as conn:
        conn.execute("INSERT INTO nfos_requests(id,source_key,payload,status,claim_token,created_at) VALUES('r1','s1',?,'starting','tok',1)",
                     (json.dumps(payload),))
        conn.commit()
    nfos_runtime.bootstrap(db=kb.kanban_db_path(), board="default", request="r1", token="tok")
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status, task_id, claim_token, last_error FROM nfos_requests WHERE id='r1'").fetchone()
        assert (row["status"], row["task_id"], row["claim_token"]) == ("pending", None, None)
        assert "absent-plugin" in row["last_error"]
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0  # nothing attached, no card claimed
    assert not launched


def _fake_probe(monkeypatch, stderr=None, calls=None):
    """Stand-in for the plugin probe child: same answer (enabled minus disabled, not found under <home>/plugins), no 12-21 s."""
    import json as _json

    import yaml

    real_run = subprocess.run

    def run(cmd, *a, **k):
        if cmd[1:2] != ["-c"] or "discover_plugins" not in cmd[2]:
            return real_run(cmd, *a, **k)
        if calls is not None:
            calls.append(cmd)
        home = k["env"]["HERMES_HOME"]
        cfg = yaml.safe_load(open(home + "/config.yaml", encoding="utf-8")) or {}
        skip = set(_json.loads(cmd[3]))
        bad = [n for n in sorted(cfg["plugins"]["enabled"]) if n not in skip and not os.path.exists(f"{home}/plugins/{n}/plugin.yaml")]
        if stderr is not None:
            return subprocess.CompletedProcess(cmd, 1, "", stderr())
        return subprocess.CompletedProcess(cmd, 0, _json.dumps(bad) + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)


def _request(conn, rid, profile, created_at):
    import json

    payload = {"project": {"profile": profile, "delivery_type": "code"}, "source": {}, "text": "do it", "attachments": []}
    conn.execute("INSERT INTO nfos_requests(id,source_key,payload,status,created_at) VALUES(?,?,?,'pending',?)",
                 (rid, "s_" + rid, json.dumps(payload), created_at))
    conn.commit()


EXECUTORS = "kanban:\n  executor_profiles: [concursa]\n  default_assignee: concursa\n"


def test_request_for_a_profile_that_create_task_retargets_is_not_refused(host):
    """B1: the card is created on default_assignee, so the guard must judge that profile and not the raw payload one."""
    from hermes_cli import nfos_delivery, nfos_runtime

    spawn, profile, launched, root = host
    root.joinpath("config.yaml").write_text(EXECUTORS, encoding="utf-8")
    profile("concursa", "toolsets:\n  - hermes-cli\n")
    profile("hermes-project-factory", None)  # bare dir, outside executor_profiles: create_task would re-point it
    kb.init_db()
    with kb.connect_closing() as conn:
        _request(conn, "r1", "hermes-project-factory", 1)
        got = nfos_delivery.reserve_request(conn, capacity=1)
        assert nfos_runtime._profile_ready_or_requeue(conn, got["id"], got["claim_token"]) is True
        assert conn.execute("SELECT status FROM nfos_requests WHERE id='r1'").fetchone()["status"] == "starting"


def test_refused_request_steps_back_so_the_next_one_is_reserved(host):
    """B1: a true refusal must not sit at the head of the queue and starve a capacity-1 board."""
    from hermes_cli import nfos_delivery, nfos_runtime

    _, profile, _, _ = host
    profile("bare", None)
    profile("good", "toolsets:\n  - hermes-cli\n")
    kb.init_db()
    with kb.connect_closing() as conn:
        _request(conn, "r_old", "bare", 1)
        _request(conn, "r_new", "good", 2)
        order = []
        for _ in range(2):
            got = nfos_delivery.reserve_request(conn, capacity=1)
            order.append(got["id"])
            if not nfos_runtime._profile_ready_or_requeue(conn, got["id"], got["claim_token"]):
                continue
            conn.execute("UPDATE nfos_requests SET status='attached' WHERE id=?", (got["id"],))  # the good one got its card
            conn.commit()
        assert order == ["r_old", "r_new"]  # r_new is reached on the very next reservation
        assert nfos_delivery.reserve_request(conn, capacity=1) is None  # r_old is held, not at the head again
        row = conn.execute("SELECT status,last_error FROM nfos_requests WHERE id='r_old'").fetchone()
        assert row["status"] == "pending" and row["last_error"].startswith(nfos_delivery.LAUNCH_REFUSED_PREFIX)
        # once the hold is over the refused request is tried again, before newer ones
        conn.execute("UPDATE nfos_requests SET claimed_at=claimed_at-? WHERE id='r_old'", (nfos_delivery.LAUNCH_REFUSED_HOLD_SECONDS + 1,))
        conn.commit()
        assert nfos_delivery.reserve_request(conn, capacity=1)["id"] == "r_old"


def test_refusal_with_a_changing_stderr_is_one_event_not_one_per_tick(host, monkeypatch):
    """B2: the dedupe key is (code, profile), not the text of the reason (which carries the probe stderr tail)."""
    spawn, profile, launched, _ = host
    profile("plug", PRESENT)
    ticks = iter(range(100))
    _fake_probe(monkeypatch, stderr=lambda: f"Traceback at tick {next(ticks)}: boom")
    kb.init_db()
    conn = kb.connect()
    tid = kb.create_task(conn, title="t", assignee="plug")
    for _ in range(5):
        kb._PROFILE_REFUSED.clear()  # ticks are 60 s apart: the in-memory memo is gone
        kb.dispatch_once(conn, failure_limit=2)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert kinds.count("spawn_refused") == 1 and not launched


def test_refusal_is_remembered_longer_than_one_dispatch_tick(host, monkeypatch):
    """B3: the tick is 60 s and the probe runs under the board lock: a refused profile is not probed again on the next tick."""
    spawn, profile, launched, _ = host
    profile("plug", PLUG)
    calls = []
    _fake_probe(monkeypatch, calls=calls)
    now = [1000.0]
    monkeypatch.setattr(kb.time, "time", lambda: now[0])
    for _ in range(3):
        with pytest.raises(kb.WorkerProfileRefused, match="absent-plugin"):
            spawn("plug")
        now[0] += 60
    assert len(calls) == 1
    now[0] += 400
    with pytest.raises(kb.WorkerProfileRefused):
        spawn("plug")
    assert len(calls) == 2  # the memo does expire
