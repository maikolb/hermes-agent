"""The WhatsApp Node.js probe must not mistake a slow Node for a missing one.

``check_whatsapp_requirements`` runs ``node --version`` with a short timeout.
On a loaded machine the first (cold) Node start can take longer than that; it
used to fall into the generic ``except`` and report Node as missing, which
drops WhatsApp for the gateway's whole life: the platform registry never
retries adapter creation, and ``connect()`` marks ``whatsapp_node_missing`` as
non-retryable. Measured on a Windows host on 2026-10-13: first
``node --version`` 5.13 s, then 0.5 to 0.8 s.
"""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.config import Platform
import plugins.platforms.whatsapp.adapter as wa


def test_missing_node_is_reported_missing():
    with patch.object(wa, "find_node_executable", return_value=None):
        assert wa.check_whatsapp_requirements() is False


def test_node_that_answers_is_present():
    # A real interpreter stands in for node: it answers ``--version`` with exit 0.
    with patch.object(wa, "find_node_executable", return_value=sys.executable):
        assert wa.check_whatsapp_requirements() is True


def test_node_that_fails_is_not_present():
    with (
        patch.object(wa, "find_node_executable", return_value=sys.executable),
        patch.object(wa.subprocess, "run", return_value=MagicMock(returncode=1)),
    ):
        assert wa.check_whatsapp_requirements() is False


def test_node_that_cannot_start_is_not_present(tmp_path):
    with patch.object(wa, "find_node_executable", return_value=str(tmp_path / "no-such-node.exe")):
        assert wa.check_whatsapp_requirements() is False


def test_slow_node_is_present_and_logged(monkeypatch, caplog):
    """A real process that outlives the probe timeout counts as present, with a warning."""
    monkeypatch.setattr(wa, "_NODE_PROBE_TIMEOUT_S", 0.001)
    with (
        patch.object(wa, "find_node_executable", return_value=sys.executable),
        caplog.at_level(logging.WARNING, logger=wa.__name__),
    ):
        assert wa.check_whatsapp_requirements() is True
    assert "did not answer within" in caplog.text


def _adapter(bridge_script: Path):
    adapter = wa.WhatsAppAdapter.__new__(wa.WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_script = str(bridge_script)
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    return adapter


def test_connect_with_slow_node_is_not_a_missing_node(tmp_path):
    """connect() moves past the Node check instead of marking a fatal, non-retryable node error."""
    adapter = _adapter(tmp_path / "missing-bridge.js")
    with (
        patch.object(wa, "find_node_executable", return_value="node"),
        patch.object(wa.subprocess, "run", side_effect=subprocess.TimeoutExpired(["node", "--version"], 5)),
    ):
        assert asyncio.run(adapter.connect()) is False
    assert adapter._fatal_error_code == "whatsapp_bridge_missing"
