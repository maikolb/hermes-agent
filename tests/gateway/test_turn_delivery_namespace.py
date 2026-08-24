from __future__ import annotations

from gateway.platforms.base import MessageEvent
from gateway.run import _validated_turn_delivery_metadata


def _result(storage_home, **overrides):
    result = {
        "session_id": "session-profile",
        "turn_checkpoint_fence": {
            "turn_id": "turn-1",
            "deliverable_revision": "revision-1",
            "content_sha256": "a" * 64,
        },
        "turn_checkpoint_root": str(
            storage_home / "sessions" / "turn-checkpoints"
        ),
        "storage_home": str(storage_home),
    }
    result.update(overrides)
    return result


def test_validated_delivery_metadata_preserves_profile_namespace(tmp_path):
    storage_home = (tmp_path / "profiles" / "worker").resolve(strict=False)

    metadata = _validated_turn_delivery_metadata(_result(storage_home))

    assert metadata == {
        "session_id": "session-profile",
        "fence": {
            "turn_id": "turn-1",
            "deliverable_revision": "revision-1",
            "content_sha256": "a" * 64,
        },
        "checkpoint_root": str(
            (storage_home / "sessions" / "turn-checkpoints").resolve(
                strict=False
            )
        ),
        "storage_home": str(storage_home),
    }


def test_validated_delivery_metadata_rejects_cross_profile_root(tmp_path):
    worker_home = (tmp_path / "profiles" / "worker").resolve(strict=False)
    default_root = (
        tmp_path / "profiles" / "default" / "sessions" / "turn-checkpoints"
    ).resolve(strict=False)

    metadata = _validated_turn_delivery_metadata(
        _result(worker_home, turn_checkpoint_root=str(default_root))
    )

    assert metadata is None


def test_validated_delivery_metadata_rejects_incomplete_fence(tmp_path):
    storage_home = (tmp_path / "profiles" / "worker").resolve(strict=False)
    result = _result(storage_home)
    result["turn_checkpoint_fence"] = {
        "turn_id": "turn-1",
        "deliverable_revision": "revision-1",
        "content_sha256": "not-a-sha",
    }

    assert _validated_turn_delivery_metadata(result) is None


def test_message_event_delivery_namespace_defaults_are_empty():
    event = MessageEvent(text="hello")

    assert event.delivery_checkpoint_session_id is None
    assert event.delivery_checkpoint_fence is None
    assert event.delivery_checkpoint_root is None
    assert event.delivery_storage_home is None
