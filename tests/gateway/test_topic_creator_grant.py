"""A+B gate opening for project Topic creation (operator order, 28/08).

The management Topic used to be the ONLY grant for project_topic_create;
a standalone group asked for a bind and the router had no path for it.
Now: config (``project_router.management_only: false``) or an allow/admin
ACL entry in the profile also grant the creator, and an explicit access
denial always wins.
"""

from __future__ import annotations

from pathlib import Path

from gateway.config import ProjectRouterConfig
from gateway.project_router import ProjectRouter, topic_creator_grant


# --- pure decision --------------------------------------------------------

def test_management_topic_keeps_historical_grant():
    assert topic_creator_grant(
        is_management=True, access="allow",
        management_only=True, sender_is_admin=False,
    ) == "management"


def test_denied_access_always_wins():
    for is_mgmt in (True, False):
        assert topic_creator_grant(
            is_management=is_mgmt, access="deny",
            management_only=False, sender_is_admin=True,
        ) is None


def test_management_without_explicit_allow_grants_nothing():
    assert topic_creator_grant(
        is_management=True, access=None,
        management_only=False, sender_is_admin=True,
    ) is None


def test_config_disables_the_gate_for_any_thread():
    assert topic_creator_grant(
        is_management=False, access=None,
        management_only=False, sender_is_admin=False,
    ) == "config"
    assert topic_creator_grant(
        is_management=False, access="allow",
        management_only=False, sender_is_admin=False,
    ) == "config"


def test_profile_admin_grants_outside_management():
    assert topic_creator_grant(
        is_management=False, access=None,
        management_only=True, sender_is_admin=True,
    ) == "acl"


def test_default_stays_closed():
    assert topic_creator_grant(
        is_management=False, access=None,
        management_only=True, sender_is_admin=False,
    ) is None


# --- ACL lookup -----------------------------------------------------------

def test_is_profile_admin_reads_seeded_acl(tmp_path: Path):
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        router.provision_topic_project(
            "Delivery",
            "Delivery",
            "telegram",
            "chat",
            "thread",
            allowed_users={"renan": "allow", "intruso": "deny"},
            board_creator=lambda slug, **kw: {"slug": slug},
        )
        assert router.is_profile_admin("renan") is True
        assert router.is_profile_admin("intruso") is False  # effect=deny
        assert router.is_profile_admin("desconhecido") is False
        assert router.is_profile_admin("") is False
        assert router.is_profile_admin(None) is False


def test_is_profile_admin_is_profile_scoped(tmp_path: Path):
    db = tmp_path / "router.db"
    with ProjectRouter(db, "default") as router:
        router.provision_topic_project(
            "Delivery", "Delivery", "telegram", "chat", "thread",
            allowed_users={"renan": "allow"},
            board_creator=lambda slug, **kw: {"slug": slug},
        )
    with ProjectRouter(db, "outro-perfil") as router:
        assert router.is_profile_admin("renan") is False


# --- config ---------------------------------------------------------------

def test_config_management_only_defaults_true():
    assert ProjectRouterConfig().management_only is True
    assert ProjectRouterConfig.from_dict({}).management_only is True


def test_config_management_only_parses_and_round_trips():
    cfg = ProjectRouterConfig.from_dict({"management_only": False})
    assert cfg.management_only is False
    assert cfg.to_dict()["management_only"] is False
