import hashlib
import json

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


def test_adapter_serializes_project_routes_and_uses_profile_spool_root(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-profile"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
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
        PlatformConfig(enabled=True, extra={"passive_intake": route_config})
    )

    expected_root = hermes_home / "platforms" / "whatsapp" / "passive-intake"
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


def test_adapter_rejects_non_json_passive_intake_configuration():
    with pytest.raises(ValueError, match="must be JSON-serializable"):
        WhatsAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"passive_intake": {"enabled": True, "routes": {object()}}},
            )
        )
