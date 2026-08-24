import hashlib
import json

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


def test_adapter_serializes_project_routes_and_uses_configured_spool_root(tmp_path):
    intake_root = tmp_path / "intake-root"
    route_config = {
        "enabled": True,
        "routes": [
            {
                "project": "concursa-ai",
                "jid": "120363111111111111@g.us",
            }
        ],
    }

    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "passive_intake": route_config,
                "passive_intake_root": str(intake_root),
            },
        )
    )

    expected_root = intake_root.resolve()
    expected_json = json.dumps(
        route_config,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    expected_hash = hashlib.sha256(
        f"{expected_json}\0{expected_root}".encode("utf-8")
    ).hexdigest()[:16]
    assert adapter._passive_intake_root == expected_root
    assert adapter._passive_intake_config == expected_json
    assert adapter._passive_intake_config_hash == expected_hash


def test_adapter_rejects_enabled_passive_intake_without_configured_root():
    route_config = {
        "enabled": True,
        "routes": [
            {
                "project": "concursa-ai",
                "jid": "120363111111111111@g.us",
            }
        ],
    }

    with pytest.raises(ValueError, match="root is required"):
        WhatsAppAdapter(
            PlatformConfig(enabled=True, extra={"passive_intake": route_config})
        )


def test_adapter_rejects_relative_passive_intake_root():
    with pytest.raises(ValueError, match="must be absolute"):
        WhatsAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"passive_intake_root": "relative/intake-root"},
            )
        )


def test_adapter_disables_passive_intake_without_creating_legacy_root():
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={}))

    assert adapter._passive_intake_root is None
    assert adapter._passive_intake_config == '{"enabled":false,"routes":[]}'


def test_adapter_rejects_non_json_passive_intake_configuration(tmp_path):
    with pytest.raises(ValueError, match="must be JSON-serializable"):
        WhatsAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "passive_intake": {"enabled": True, "routes": {object()}},
                    "passive_intake_root": str(tmp_path / "intake-root"),
                },
            )
        )
