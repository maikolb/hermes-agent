import pytest

import hermes_cli.windows_process_broker as process_broker
from tools import terminal_tool


def test_terminal_zero_ui_boundary_installs_and_verifies_broker(monkeypatch):
    state = {"ready": False}

    monkeypatch.setattr(terminal_tool.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        process_broker,
        "install_windows_process_broker",
        lambda: state.__setitem__("ready", True),
    )
    monkeypatch.setattr(process_broker, "broker_installed", lambda: state["ready"])

    terminal_tool._install_windows_terminal_zero_ui_boundary()

    assert state["ready"] is True


def test_terminal_zero_ui_boundary_fails_closed(monkeypatch):
    monkeypatch.setattr(terminal_tool.platform, "system", lambda: "Windows")
    monkeypatch.setattr(process_broker, "install_windows_process_broker", lambda: None)
    monkeypatch.setattr(process_broker, "broker_installed", lambda: False)

    with pytest.raises(RuntimeError, match="zero-UI broker is not active"):
        terminal_tool._install_windows_terminal_zero_ui_boundary()


def test_terminal_zero_ui_boundary_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(terminal_tool.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        process_broker,
        "install_windows_process_broker",
        lambda: pytest.fail("Windows broker must not install off Windows"),
    )

    terminal_tool._install_windows_terminal_zero_ui_boundary()
