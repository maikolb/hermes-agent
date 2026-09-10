"""Capacidade no diagnóstico do dispatcher (patch 10/09/2026): fila esperando vaga não é travamento."""
from types import SimpleNamespace

from gateway import kanban_watchers as kw
from hermes_cli.kanban_db import DispatchResult


def test_dispatch_result_has_capacity_bucket():
    assert DispatchResult().skipped_capacity == []


def test_capacity_bits_render_reason_and_numbers():
    res = SimpleNamespace(skipped_capacity=[{"reason": "workers", "running": 4, "limit": 4, "reserved": 1}])
    assert kw._capacity_bits(res) == ["capacity: workers 4/4 (+1 reservados)"]
    assert kw._capacity_bits(SimpleNamespace()) == []


def test_capacity_only_is_true_only_when_every_board_waited_for_a_slot():
    capped = SimpleNamespace(skipped_capacity=[{"reason": "max_in_progress", "running": 16, "limit": 16}], spawned=[])
    assert kw._capacity_only([("a", capped), ("b", None)]) is True
    assert kw._capacity_only([("a", capped), ("b", SimpleNamespace(spawned=[("t", "p", "w")]))]) is False
    assert kw._capacity_only([("a", SimpleNamespace(skipped_unassigned=["t_x"]))]) is False
    assert kw._capacity_only([]) is False


def test_workspace_lease_counts_as_waiting_not_stuck():
    leased = SimpleNamespace(skipped_workspace_leased=[("t_a", "t_b", "/w")], spawned=[])
    assert kw._capacity_only([("dovcrm", leased)]) is True
