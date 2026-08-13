from pathlib import Path

import pytest

import gateway.config as gateway_config
from gateway.config import GatewayConfig, ProjectRouterConfig


def test_project_router_defaults_disabled_and_empty():
    config = GatewayConfig()

    assert config.project_router == ProjectRouterConfig()
    assert config.project_router.enabled is False
    assert config.project_router.db_path is None
    assert config.project_router.managed_chat_ids == []
    assert config.project_router.auto_register_topics is False
    assert config.project_router.management_topic_names == ["🧭 Gestão"]


def test_project_router_valid_parse_and_roundtrip():
    config = GatewayConfig.from_dict(
        {
            "project_router": {
                "enabled": True,
                "db_path": "state/router.db",
                "managed_chat_ids": [-1001, "-1002"],
                "auto_register_topics": True,
                "management_topic_names": ["🧭 Gestão", "Management"],
            }
        }
    )

    assert config.project_router == ProjectRouterConfig(
        enabled=True,
        db_path=Path("state/router.db"),
        managed_chat_ids=["-1001", "-1002"],
        auto_register_topics=True,
        management_topic_names=["🧭 Gestão", "Management"],
    )
    assert GatewayConfig.from_dict(config.to_dict()).project_router == config.project_router


def test_project_router_nested_fallback_and_top_level_precedence():
    nested = GatewayConfig.from_dict(
        {"gateway": {"project_router": {"enabled": True, "managed_chat_ids": ["7"]}}}
    )
    top = GatewayConfig.from_dict(
        {
            "project_router": {"enabled": False, "managed_chat_ids": ["8"]},
            "gateway": {"project_router": {"enabled": True, "managed_chat_ids": ["7"]}},
        }
    )

    assert nested.project_router.enabled is True
    assert nested.project_router.managed_chat_ids == ["7"]
    assert top.project_router.enabled is False
    assert top.project_router.managed_chat_ids == ["8"]


@pytest.mark.parametrize(
    "raw",
    [
        "enabled",
        [],
        {"enabled": True, "db_path": []},
        {"enabled": True, "managed_chat_ids": "7"},
        {"enabled": True, "managed_chat_ids": [""]},
        {"enabled": True, "managed_chat_ids": [True]},
        {"enabled": True, "management_topic_names": "Management"},
        {"enabled": True, "management_topic_names": [""]},
        {"enabled": True, "management_topic_names": [7]},
    ],
)
def test_project_router_malformed_values_safe_disable(raw):
    parsed = GatewayConfig.from_dict({"project_router": raw}).project_router

    assert parsed == ProjectRouterConfig()


def test_malformed_top_level_does_not_fall_back_to_nested_enabled_router():
    parsed = GatewayConfig.from_dict(
        {
            "project_router": "invalid",
            "gateway": {"project_router": {"enabled": True, "managed_chat_ids": ["7"]}},
        }
    )

    assert parsed.project_router == ProjectRouterConfig()


@pytest.mark.parametrize(
    "yaml_text, expected_ids",
    [
        ("project_router:\n  enabled: true\n  managed_chat_ids: ['1']\n", ["1"]),
        ("gateway:\n  project_router:\n    enabled: true\n    managed_chat_ids: ['2']\n", ["2"]),
        (
            "project_router:\n  enabled: false\n  managed_chat_ids: ['3']\n"
            "gateway:\n  project_router:\n    enabled: true\n    managed_chat_ids: ['4']\n",
            ["3"],
        ),
    ],
)
def test_load_gateway_config_project_router_precedence(
    monkeypatch, tmp_path, yaml_text, expected_ids
):
    (tmp_path / "config.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(gateway_config, "get_hermes_home", lambda: tmp_path)

    loaded = gateway_config.load_gateway_config()

    assert loaded.project_router.managed_chat_ids == expected_ids
    assert loaded.project_router.enabled is (expected_ids == ["1"] or expected_ids == ["2"])
