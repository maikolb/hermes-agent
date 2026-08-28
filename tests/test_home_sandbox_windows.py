"""The session HERMES_HOME sandbox must arm on BOTH production layouts.

27/08 incident: a user-level Windows env var HERMES_HOME pointing at the
platform-native production home (%LOCALAPPDATA%\\hermes) passed the
production check — which only knew the POSIX ~/.hermes layout — so the
session sandbox never armed. Collection-time imports froze
``gateway.run._hermes_home`` at the LIVE install: /update tests wrote
``.update_pending.json`` into the operator's real home and the live
config (TTS enabled) fed voice tests that played real audio.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import (
    HERMES_HOME_AT_CONFTEST_IMPORT,
    _hermes_home_points_at_production,
)


def test_windows_native_layout_counts_as_production():
    from hermes_constants import _get_platform_default_hermes_home

    native = str(_get_platform_default_hermes_home())
    assert _hermes_home_points_at_production(native) is True


def test_posix_layout_still_counts_as_production():
    assert _hermes_home_points_at_production(str(Path.home() / ".hermes")) is True


def test_profile_home_under_native_root_counts_as_production():
    from hermes_constants import _get_platform_default_hermes_home

    profile = _get_platform_default_hermes_home() / "profiles" / "factory"
    assert _hermes_home_points_at_production(str(profile)) is True


def test_genuinely_custom_home_is_honored(tmp_path):
    assert _hermes_home_points_at_production(str(tmp_path / "custom")) is False


def test_session_sandbox_was_armed_before_any_test_import():
    """The env var frozen at conftest import must never be a production root."""
    from hermes_constants import _get_platform_default_hermes_home

    frozen = HERMES_HOME_AT_CONFTEST_IMPORT
    assert frozen, "conftest must always leave HERMES_HOME set"
    resolved = Path(frozen).expanduser().resolve()
    assert resolved != (Path.home() / ".hermes").resolve()
    assert resolved != _get_platform_default_hermes_home().resolve()


def test_gateway_run_home_is_not_production():
    import gateway.run as gr

    from hermes_constants import _get_platform_default_hermes_home

    frozen = Path(str(gr._hermes_home)).resolve()
    assert frozen != (Path.home() / ".hermes").resolve()
    assert frozen != _get_platform_default_hermes_home().resolve()
