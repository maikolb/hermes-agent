"""Regression contract for invisible Windows child-process creation."""

import subprocess

from hermes_cli._subprocess_compat import IS_WINDOWS, windows_hidden_popen_kwargs
from hermes_cli.windows_process_broker import broker_installed, hidden_desktop_ready


def test_windows_hidden_popen_kwargs_are_platform_safe():
    kwargs = windows_hidden_popen_kwargs()
    if not IS_WINDOWS:
        assert kwargs == {}
        return

    assert kwargs["creationflags"] & 0x08000000
    startupinfo = kwargs["startupinfo"]
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE
    assert broker_installed()
    assert hidden_desktop_ready()
