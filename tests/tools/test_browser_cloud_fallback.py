"""Tests for fail-closed cloud browser provider runtime behavior.

Covers _get_session_info() when a cloud provider is configured but fails at
runtime. An explicit cloud route must never launch local Chromium implicitly.
"""
from unittest.mock import Mock

import pytest

import tools.browser_tool as browser_tool


def _reset_session_state(monkeypatch):
    """Clear caches so each test starts fresh."""
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda t: None)


class TestCloudProviderRuntimeFallback:
    """Tests for _get_session_info cloud-route failure handling."""

    def test_cloud_failure_fails_closed_without_local_browser(self, monkeypatch):
        """A cloud provider exception must not launch local Chromium."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.side_effect = RuntimeError("401 Unauthorized")
        local_session = Mock(side_effect=AssertionError("local browser was launched"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_local_session", local_session)

        with pytest.raises(RuntimeError, match="implicit local browser fallback is disabled"):
            browser_tool._get_session_info("task-1")

        local_session.assert_not_called()


    def test_no_provider_uses_local_directly(self, monkeypatch):
        """When no cloud provider is configured, local mode is used with no fallback markers."""
        _reset_session_state(monkeypatch)

        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)

        session = browser_tool._get_session_info("task-4")

        assert session["features"]["local"] is True
        assert "fallback_from_cloud" not in session


    def test_cloud_returns_invalid_session_fails_closed(self, monkeypatch):
        """An invalid cloud session must not trigger local Chromium."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.return_value = None
        local_session = Mock(side_effect=AssertionError("local browser was launched"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_local_session", local_session)

        with pytest.raises(RuntimeError, match="implicit local browser fallback is disabled"):
            browser_tool._get_session_info("task-7")

        local_session.assert_not_called()
