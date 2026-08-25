"""Regression contract for the process-wide Windows zero-UI broker."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import windows_process_broker as broker


def test_policy_wrapper_uses_concrete_base_popen_signature():
    class GuardedPopen(broker._original_popen):
        def __init__(self, cmd, *args, **kwargs):
            super().__init__(cmd, *args, **kwargs)

    signature = broker._concrete_popen_signature(GuardedPopen)
    bound = signature.bind([sys.executable, "-V"], stdout=subprocess.PIPE)

    assert bound.arguments["args"] == [sys.executable, "-V"]
    assert signature.parameters["args"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


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
    assert not flags & 0x10


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_gui_runner_policy_is_no_window_but_not_suspended():
    flags, startupinfo = broker._runner_spawn_policy()
    assert flags & 0x08000000
    assert not flags & 0x04
    assert not flags & 0x10
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_broker_is_installed_by_shared_bootstrap():
    import hermes_bootstrap

    hermes_bootstrap.activate_windows_process_broker()
    assert broker.broker_installed()
    assert issubclass(subprocess.Popen, broker.WindowsHiddenPopen)


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")
def test_direct_child_uses_private_runner_and_exposes_real_target_pid():
    broker.install_windows_process_broker()
    proc = subprocess.Popen(
        [
            str(Path(sys.base_prefix) / "python.exe"),
            "-c",
            "import os,time; print(os.getpid(), flush=True); time.sleep(0.2)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=broker.direct_hidden_child_env(),
    )
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    assert int(stdout.strip()) == proc.pid
    assert proc.pid != proc._hermes_runner_pid


def test_spawn_handshake_retries_transient_read_error(tmp_path, monkeypatch):
    status_path = tmp_path / "spawn-status.json"
    status_path.write_text(json.dumps({"child_pid": 4242}), encoding="utf-8")
    original_read_text = Path.read_text
    attempts = 0

    def transient_read_text(path, *args, **kwargs):
        nonlocal attempts
        if path == status_path and attempts == 0:
            attempts += 1
            raise PermissionError("transient scanner lock")
        return original_read_text(path, *args, **kwargs)

    class LiveRunner:
        pass

    monkeypatch.setattr(Path, "read_text", transient_read_text)
    monkeypatch.setattr(broker._original_popen, "poll", lambda _self: None)

    child_pid = broker._consume_spawn_handshake(
        LiveRunner(), str(status_path), required=True
    )

    assert child_pid == 4242
    assert attempts == 1
    assert not status_path.exists()


def test_posix_branch_is_a_noop(monkeypatch):
    monkeypatch.setattr(broker, "IS_WINDOWS", False)
    marker = object()
    flags, startupinfo = broker.hidden_spawn_policy(17, marker)
    assert (flags, startupinfo) == (17, marker)
