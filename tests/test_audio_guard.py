"""The test-suite audio guard keeps the operator's speakers closed.

27/08 incident, twice over: full-suite runs played REAL audio on the
operator's machine — first via ffplay (subprocess path, Prefetch/registry
evidence), then AGAIN via sounddevice native output (spoken TTS at 21:50
with the subprocess proxy already active). These tests pin both escape
paths shut for every unmarked test.
"""

from __future__ import annotations

import pytest


def test_process_wide_kill_switch_is_armed():
    """HERMES_DISABLE_AUDIO is set once for the whole pytest process.

    Fixture-scoped guards revert on teardown, and voice playback runs on
    daemon threads that can fire AFTER teardown — the escape that played
    spoken TTS at 22:22 with all fixture guards active. The env switch is
    read by the product at call time, so late threads stay silent too.
    """
    import os

    assert os.environ.get("HERMES_DISABLE_AUDIO") == "1"


def test_kill_switch_silences_playback_without_any_fixture_patching():
    """play_audio_file refuses BEFORE touching players — env, not fixture."""
    import tools.voice_mode as vm

    assert vm._audio_disabled() is True
    assert vm.play_audio_file("/nonexistent/never-touched.wav") is False


def test_voice_mode_native_audio_is_blocked():
    import tools.voice_mode as vm

    with pytest.raises(ImportError):
        vm._import_audio()


def test_tts_tool_sounddevice_is_blocked():
    import tools.tts_tool as tts

    with pytest.raises(ImportError):
        tts._import_sounddevice()


def test_voice_mode_player_spawn_is_silenced(tmp_path, monkeypatch):
    """A genuine Popen of ffplay inside tools.voice_mode never spawns."""
    import tools.voice_mode as vm

    proc = vm.subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", str(tmp_path / "x.wav")]
    )
    # The guard returns an already-finished silent process object.
    assert proc.wait() == 0
    assert proc.poll() == 0


def test_converters_still_pass_through(monkeypatch):
    """ffmpeg (conversion, no speakers) is not blocked by the proxy."""
    import tools.voice_mode as vm

    captured = {}

    def _fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd

        class _P:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        return _P()

    # Simulate a test-installed mock at the global module: the proxy must
    # honor it for ANY command (mocks make no noise).
    monkeypatch.setattr(vm.subprocess._real, "Popen", _fake_popen)
    vm.subprocess.Popen(["ffmpeg", "-i", "a.ogg", "b.wav"])
    assert captured["cmd"][0] == "ffmpeg"
