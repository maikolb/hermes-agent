"""Steers that die with an errored turn: drained, announced or auto-resumed."""

from __future__ import annotations

from gateway.run import _drain_lost_steer, _lost_steer_notice


class _AgentWithSteer:
    def __init__(self, pending):
        self._pending = pending
        self.drained = 0

    def _drain_pending_steer(self):
        self.drained += 1
        text, self._pending = self._pending, None
        return text


def test_drain_returns_and_clears_pending():
    agent = _AgentWithSteer("coloca um worker paralelo nesse caso aqui")

    assert _drain_lost_steer(agent) == "coloca um worker paralelo nesse caso aqui"
    assert agent.drained == 1
    assert _drain_lost_steer(agent) == ""


def test_drain_tolerates_failure_and_none():
    class Boom:
        def _drain_pending_steer(self):
            raise RuntimeError("dead")

    assert _drain_lost_steer(Boom()) == ""
    assert _drain_lost_steer(None) == ""


def test_notice_names_the_lost_steer():
    note = _lost_steer_notice("coloca um worker paralelo nesse caso\ne outro no link X")

    assert "NOT processed" in note
    assert "coloca um worker paralelo" in note


def test_notice_truncates_long_steer():
    note = _lost_steer_notice("x" * 500)

    assert "…" in note
    assert len(note) < 320


def test_no_steer_no_notice():
    assert _lost_steer_notice("") == ""
