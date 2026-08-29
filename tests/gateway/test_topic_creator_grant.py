"""A+B gate for project Topic creation — hardened cut (28/08 night).

First cut was REFUTED by adversarial review: deny could never win at
turn time (denied contexts raise upstream), the per-turn ACL lookup
blocked the event loop, and a multiplexed secondary profile inherited
the primary's ``management_only``. Authorization now happens at the
moment of use, inside the router the creator already opens, with a
deny-first three-verdict ACL check that also vetoes the config grant.
"""

from __future__ import annotations

from pathlib import Path

from gateway.config import ProjectRouterConfig
from gateway.project_router import ProjectRouter


def _seed(router: ProjectRouter, allowed_users: dict) -> None:
    router.provision_topic_project(
        "Delivery",
        "Delivery",
        "telegram",
        "chat-a",
        "thread-1",
        allowed_users=allowed_users,
        board_creator=lambda slug, **kw: {"slug": slug},
    )


# --- topic_grant_at_use: three verdicts, deny-first -----------------------

def test_admin_anywhere_in_profile_grants(tmp_path: Path):
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        _seed(router, {"renan": "allow"})
        assert router.topic_grant_at_use("renan", "chat-b") == "acl"
        assert router.topic_grant_at_use("renan", "chat-a") == "acl"


def test_deny_in_current_chat_vetoes_even_an_admin(tmp_path: Path):
    """Reviewer probe C: a user denied in a chat must not create there,
    even holding admin elsewhere — and the deny verdict is distinct so
    it also vetoes the config grant."""
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        _seed(router, {"renan": "allow"})
        router.provision_topic_project(
            "Outro", "Outro", "telegram", "chat-b", "thread-2",
            allowed_users={"renan": "deny"},
            board_creator=lambda slug, **kw: {"slug": slug},
        )
        assert router.topic_grant_at_use("renan", "chat-b") == "deny"
        assert router.topic_grant_at_use("renan", "chat-a") == "acl"


def test_unknown_user_has_no_acl_opinion(tmp_path: Path):
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        _seed(router, {"renan": "allow"})
        assert router.topic_grant_at_use("desconhecido", "chat-a") is None


def test_blank_sender_is_denied_not_neutral(tmp_path: Path):
    """A turn with no sender identity must never fall through to the
    config grant (internal continuations fabricate access without a
    sender — reviewer trap)."""
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        assert router.topic_grant_at_use("", "chat-a") == "deny"
        assert router.topic_grant_at_use(None, "chat-a") == "deny"


def test_member_role_allow_is_not_an_admin_grant(tmp_path: Path):
    """Reviewer probe E: effect=allow with role=member must not grant."""
    with ProjectRouter(tmp_path / "router.db", "default") as router:
        _seed(router, {"renan": "allow"})
        with router._lock:
            router._connection.execute(
                "INSERT INTO acl_entries(profile, chat_id, user_id, effect, role) "
                "VALUES ('default', 'chat-m', 'membro', 'allow', 'member')"
            )
            router._connection.commit()
        assert router.topic_grant_at_use("membro", "chat-m") is None


def test_verdict_is_profile_scoped(tmp_path: Path):
    db = tmp_path / "router.db"
    with ProjectRouter(db, "default") as router:
        _seed(router, {"renan": "allow"})
    with ProjectRouter(db, "outro-perfil") as router:
        assert router.topic_grant_at_use("renan", "chat-a") is None


# --- config ---------------------------------------------------------------

def test_config_management_only_defaults_true():
    assert ProjectRouterConfig().management_only is True
    assert ProjectRouterConfig.from_dict({}).management_only is True


def test_config_management_only_parses_and_round_trips():
    cfg = ProjectRouterConfig.from_dict({"management_only": False})
    assert cfg.management_only is False
    assert cfg.to_dict()["management_only"] is False
