"""SPAWN_GUARD_20261016: _default_spawn refuses a worker whose profile cannot run its own contract."""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_db as kb


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
    kb._PROFILE_READY_CACHE.clear()
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
    with pytest.raises(ValueError, match="no readable config.yaml"):
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
