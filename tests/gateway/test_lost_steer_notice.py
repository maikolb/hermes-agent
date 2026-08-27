"""Steers that die with an errored turn must be announced, not swallowed."""

from __future__ import annotations

from gateway.run import _lost_steer_notice


class _AgentWithSteer:
    def __init__(self, pending):
        self._pending = pending
        self.drained = 0

    def _drain_pending_steer(self):
        self.drained += 1
        text, self._pending = self._pending, None
        return text


def test_notice_names_the_lost_steer():
    agent = _AgentWithSteer("coloca um worker paralelo nesse caso aqui\ne outro no link X")

    note = _lost_steer_notice(agent)

    assert "NOT processed" in note
    assert "coloca um worker paralelo" in note
    assert agent.drained == 1


def test_notice_truncates_long_steer():
    agent = _AgentWithSteer("x" * 500)

    note = _lost_steer_notice(agent)

    assert "…" in note
    assert len(note) < 320


def test_no_pending_steer_no_notice():
    assert _lost_steer_notice(_AgentWithSteer(None)) == ""
    assert _lost_steer_notice(None) == ""


def test_drain_failure_is_silent():
    class Boom:
        def _drain_pending_steer(self):
            raise RuntimeError("dead")

    assert _lost_steer_notice(Boom()) == ""
