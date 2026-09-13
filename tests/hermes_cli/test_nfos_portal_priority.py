"""PORTAL_PRIORITY_20260912: prioridade explícita do pedido vence a palavra urgente; portal é plataforma humana."""
from hermes_cli import nfos_delivery as delivery


def test_explicit_priority_wins_and_portal_is_human():
    assert delivery._intake_priority({"project": {"priority": 10}, "urgent": True, "source": {"platform": "portal"}}) == 10
    assert delivery._intake_priority({"project": {"priority": 100}, "source": {"platform": "portal"}}) == 100
    assert delivery._intake_priority({"project": {"priority": 55}, "source": {"platform": "portal"}}) == delivery.HUMAN_REQUEST_PRIORITY
    assert delivery._intake_priority({"project": {}, "source": {"platform": "portal"}}) == delivery.HUMAN_REQUEST_PRIORITY
    assert delivery._intake_priority({"urgent": True, "source": {"platform": "telegram"}}) == delivery.URGENT_PRIORITY
    assert delivery._intake_priority({"source": {"platform": "cron"}}) == 0
