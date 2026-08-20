"""Windows regression for Playwright console-window prevention."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools import browser_tool


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows console-subsystem regression",
)


def _touch_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_explicit_headless_shell_is_rejected(monkeypatch, tmp_path):
    headless_shell = _touch_executable(tmp_path / "chrome-headless-shell.exe")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", str(headless_shell))

    with pytest.raises(RuntimeError, match="can open Windows Terminal"):
        browser_tool._resolve_windows_gui_browser_executable()


def test_explicit_gui_chromium_is_accepted(monkeypatch, tmp_path):
    chrome = _touch_executable(tmp_path / "chrome.exe")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", str(chrome))

    assert browser_tool._resolve_windows_gui_browser_executable() == str(chrome)


def test_bundled_full_chromium_wins_over_headless_shell(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)

    _touch_executable(
        tmp_path
        / "ms-playwright"
        / "chromium_headless_shell-1208"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe"
    )
    chrome = _touch_executable(
        tmp_path
        / "ms-playwright"
        / "chromium-1208"
        / "chrome-win64"
        / "chrome.exe"
    )

    assert browser_tool._resolve_windows_gui_browser_executable() == str(chrome)
