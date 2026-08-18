"""Regression contract for the process-wide Windows zero-UI broker."""

from __future__ import annotations

import subprocess
import sys

import pytest

from hermes_cli import windows_process_broker as broker


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_policy_strips_conflicting_console_and_breakaway_flags():
    flags, startupinfo = broker.hidden_spawn_policy(0x10 | 0x08 | 0x01000000)
    assert flags & 0x08000000
    assert flags & 0x04
    assert not flags & 0x10
    assert not flags & 0x08
    assert not flags & 0x01000000
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_existing_startupinfo_is_copied_and_preserved():
    original = subprocess.STARTUPINFO()
    original.dwFlags = 0x400
    flags, result = broker.hidden_spawn_policy(0, original)
    assert result is not original
    assert result.dwFlags & 0x400
    assert result.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert flags & 0x08000000
    assert flags & 0x04


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_runner_policy_is_hidden_but_not_suspended():
    flags, startupinfo = broker.hidden_spawn_policy(0, suspend=False)
    assert flags & 0x08000000
    assert not flags & 0x04
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_broker_is_installed_by_shared_bootstrap():
    import hermes_bootstrap

    hermes_bootstrap.activate_windows_process_broker()
    assert broker.broker_installed()
    assert subprocess.Popen is broker.WindowsHiddenPopen


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_install_repairs_stale_installed_flag(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", broker._original_popen)
    monkeypatch.setattr(broker, "_installed", True)

    assert broker.install_windows_process_broker() is True
    assert broker.broker_installed()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_broker_remains_installed_through_policy_wrapper(monkeypatch):
    class GuardedPopen(broker.WindowsHiddenPopen):
        pass

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    assert broker.install_windows_process_broker() is False
    assert subprocess.Popen is GuardedPopen
    assert broker.broker_installed()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_interactive_child_marker_is_capability_scoped():
    env = broker.interactive_desktop_child_env({"KEEP": "yes"})
    assert env["KEEP"] == "yes"
    assert env["HERMES_INTERNAL_INTERACTIVE_DESKTOP_CHILD"] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_direct_hidden_child_marker_is_capability_scoped():
    env = broker.direct_hidden_child_env({"KEEP": "yes"})
    assert env["KEEP"] == "yes"
    assert env["HERMES_INTERNAL_DIRECT_HIDDEN_CHILD"] == "1"


def test_posix_branch_is_a_noop(monkeypatch):
    monkeypatch.setattr(broker, "IS_WINDOWS", False)
    marker = object()
    flags, startupinfo = broker.hidden_spawn_policy(17, marker)
    assert (flags, startupinfo) == (17, marker)
