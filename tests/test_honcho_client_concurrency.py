"""Concurrency test for get_honcho_client() — the TOCTOU race fix (#24759).

Proves the Honcho client is constructed exactly once even when many threads
race the first call, by stubbing the SDK constructor and counting invocations.
"""

import hashlib
import json
import sys
import threading
import types
from contextlib import contextmanager
from dataclasses import replace

import pytest

from plugins.memory.honcho import client as honcho_client
from plugins.memory.honcho.client import (
    HonchoClientConfig,
    get_honcho_client,
    reset_honcho_client,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    reset_honcho_client()
    yield
    reset_honcho_client()
    reset_session_vars()


@contextmanager
def _session_scope(
    *,
    platform="telegram",
    profile="profile-a",
    chat_id="chat-a",
    project_id="project-a",
):
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform=platform,
        profile=profile,
        chat_id=chat_id,
        project_id=project_id,
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _expected_project_workspace(base, profile, chat_id, project_id):
    digest = hashlib.sha256(b"honcho-telegram-project-workspace-v1")
    for component in (base, profile, chat_id, project_id):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _install_fake_honcho_sdk(
    monkeypatch, build_count, build_lock, *, disable_oauth=True
):
    """Make `from honcho import Honcho` resolve to a counting fake."""

    class _FakeHoncho:
        def __init__(self, **kwargs):
            with build_lock:
                build_count["n"] += 1
                build_count.setdefault("kwargs", []).append(dict(kwargs))
            import time
            time.sleep(0.01)  # widen the race window
            self.kwargs = kwargs

    fake_mod = types.ModuleType("honcho")
    fake_mod.Honcho = _FakeHoncho
    monkeypatch.setitem(sys.modules, "honcho", fake_mod)
    # Skip the lazy-install path entirely.
    monkeypatch.setattr(
        honcho_client, "_resolve_optional_float", lambda *a, **k: None, raising=False
    )
    if disable_oauth:
        monkeypatch.setattr(honcho_client, "_apply_fresh_oauth_token", lambda config: None)


def test_get_honcho_client_builds_once_under_concurrent_first_call(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)

    config = HonchoClientConfig(
        api_key="test-key",
        workspace_id="ws",
        environment="production",
    )

    barrier = threading.Barrier(20)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        c = get_honcho_client(config)
        with results_lock:
            results.append(c)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert build_count["n"] == 1, "Honcho client must be constructed exactly once"
    assert len(results) == 20
    assert all(r is results[0] for r in results), "all threads share one client"


def test_concurrent_workspaces_build_once_per_key_and_return_correct_client(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)

    configs = {
        "alpha": HonchoClientConfig(
            host="profile-alpha",
            api_key="test-key",
            workspace_id="workspace-alpha",
            environment="production",
            timeout=30,
        ),
        "beta": HonchoClientConfig(
            host="profile-beta",
            api_key="test-key",
            workspace_id="workspace-beta",
            environment="production",
            timeout=30,
        ),
    }
    barrier = threading.Barrier(24)
    results = {"alpha": [], "beta": []}
    results_lock = threading.Lock()

    def worker(name):
        barrier.wait()
        client = get_honcho_client(configs[name])
        with results_lock:
            results[name].append(client)

    threads = [
        threading.Thread(target=worker, args=(("alpha", "beta")[i % 2],))
        for i in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert build_count["n"] == 2
    assert sorted(row["workspace_id"] for row in build_count["kwargs"]) == [
        "workspace-alpha",
        "workspace-beta",
    ]
    assert all(client is results["alpha"][0] for client in results["alpha"])
    assert all(client is results["beta"][0] for client in results["beta"])
    assert results["alpha"][0] is not results["beta"][0]
    assert results["alpha"][0].kwargs["workspace_id"] == "workspace-alpha"
    assert results["beta"][0].kwargs["workspace_id"] == "workspace-beta"


def test_context_scoped_homes_resolve_distinct_clients_concurrently(
    monkeypatch, tmp_path
):
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)
    homes = {}
    for name in ("alpha", "beta"):
        home = tmp_path / name
        home.mkdir()
        (home / "honcho.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "apiKey": "test-key",
                    "workspace": f"workspace-{name}",
                    "timeout": 30,
                }
            )
        )
        homes[name] = home

    barrier = threading.Barrier(16)
    results = {"alpha": [], "beta": []}
    results_lock = threading.Lock()

    def worker(name):
        token = set_hermes_home_override(homes[name])
        try:
            barrier.wait()
            client = get_honcho_client()
        finally:
            reset_hermes_home_override(token)
        with results_lock:
            results[name].append(client)

    threads = [
        threading.Thread(target=worker, args=(("alpha", "beta")[i % 2],))
        for i in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert build_count["n"] == 2
    assert all(client is results["alpha"][0] for client in results["alpha"])
    assert all(client is results["beta"][0] for client in results["beta"])
    assert results["alpha"][0] is not results["beta"][0]
    assert results["alpha"][0].kwargs["workspace_id"] == "workspace-alpha"
    assert results["beta"][0].kwargs["workspace_id"] == "workspace-beta"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "host-b"),
        ("workspace_id", "workspace-b"),
        ("base_url", "https://two.invalid"),
        ("environment", "staging"),
        ("api_key", "credential-b"),
        ("timeout", 60),
    ],
)
def test_cache_signature_separates_every_sdk_identity_field(
    monkeypatch, field, value
):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)

    base = HonchoClientConfig(
        host="host-a",
        workspace_id="workspace-a",
        base_url="https://one.invalid",
        environment="production",
        api_key="credential-a",
        timeout=30,
    )
    variant = replace(base, **{field: value})

    base_client = get_honcho_client(base)
    variant_client = get_honcho_client(variant)

    assert base_client is not variant_client
    assert get_honcho_client(variant) is variant_client
    assert build_count["n"] == 2


def test_cache_key_hashes_credential_without_retaining_raw_key():
    raw_key = "credential-must-not-enter-cache-key"
    config = HonchoClientConfig(
        host="host",
        workspace_id="workspace",
        environment="production",
        api_key=raw_key,
        timeout=30,
    )

    key = honcho_client._client_cache_key(config)

    assert raw_key not in repr(key)
    expected = hashlib.sha256(f"key:{raw_key}".encode()).hexdigest()[:16]
    assert key[-1] == expected


def test_telegram_project_workspace_is_stable_separated_and_digest_only():
    base = "workspace::shared-base"
    contexts = {
        "alpha": ("profile::red", "chat::team-one", "project::north"),
        "other_team": ("profile::blue", "chat::team-two", "project::north"),
        "other_project": ("profile::red", "chat::team-one", "project::south"),
    }
    resolved = {}

    for name, (profile, chat_id, project_id) in contexts.items():
        with _session_scope(
            profile=profile, chat_id=chat_id, project_id=project_id
        ):
            config = HonchoClientConfig(workspace_id=base)
            first = honcho_client._effective_workspace_id(config)
            second = honcho_client._effective_workspace_id(config)
        resolved[name] = first
        assert first == second
        assert first == _expected_project_workspace(
            base, profile, chat_id, project_id
        )
        assert len(first) == 64
        assert set(first) <= set("0123456789abcdef")
        assert all(
            raw not in first for raw in (base, profile, chat_id, project_id)
        )

    assert len(set(resolved.values())) == len(resolved)


@pytest.mark.parametrize(
    ("platform", "project_id"),
    [("telegram", ""), ("discord", "project::north")],
)
def test_non_project_or_non_telegram_context_preserves_legacy_workspace(
    platform, project_id
):
    base = "legacy-workspace"
    with _session_scope(
        platform=platform,
        profile="profile::legacy",
        chat_id="chat::legacy",
        project_id=project_id,
    ):
        config = HonchoClientConfig(workspace_id=base)
        assert honcho_client._effective_workspace_id(config) == base


def test_process_environment_identity_alone_does_not_partition_workspace(monkeypatch):
    base = "legacy-workspace"
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "profile::stale")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat::stale")
    monkeypatch.setenv("HERMES_PROJECT_ID", "project::stale")

    config = HonchoClientConfig(workspace_id=base)

    assert honcho_client._effective_workspace_id(config) == base


def test_telegram_project_cache_is_distinct_and_builds_once_per_scope(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)
    config = HonchoClientConfig(
        host="profile-host",
        api_key="test-key",
        workspace_id="workspace::shared-base",
        timeout=30,
    )
    contexts = {
        "one": ("profile::one", "chat::one", "project::one"),
        "two": ("profile::two", "chat::two", "project::two"),
    }
    barrier = threading.Barrier(20)
    results = {"one": [], "two": []}
    results_lock = threading.Lock()

    def worker(name):
        profile, chat_id, project_id = contexts[name]
        with _session_scope(
            profile=profile, chat_id=chat_id, project_id=project_id
        ):
            barrier.wait()
            client = get_honcho_client(config)
        with results_lock:
            results[name].append(client)

    threads = [
        threading.Thread(target=worker, args=(("one", "two")[i % 2],))
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert build_count["n"] == 2
    assert all(client is results["one"][0] for client in results["one"])
    assert all(client is results["two"][0] for client in results["two"])
    assert results["one"][0] is not results["two"][0]
    for name, (profile, chat_id, project_id) in contexts.items():
        assert results[name][0].kwargs["workspace_id"] == (
            _expected_project_workspace(
                config.workspace_id, profile, chat_id, project_id
            )
        )


def test_captured_digest_survives_honcho_worker_without_raw_ids(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)
    monkeypatch.setenv("HONCHO_API_KEY", "test-key")
    base = "workspace::base-sensitive"
    profile = "profile::team-sensitive"
    chat_id = "chat::sensitive"
    project_id = "project::sensitive"

    with _session_scope(
        profile=profile, chat_id=chat_id, project_id=project_id
    ):
        config = HonchoClientConfig.from_env(
            workspace_id=base, host="profile-host"
        )

    assert config._bound_workspace_id == _expected_project_workspace(
        base, profile, chat_id, project_id
    )
    assert all(
        raw not in repr(config) for raw in (profile, chat_id, project_id)
    )
    assert all(
        raw not in config._bound_workspace_id
        for raw in (base, profile, chat_id, project_id)
    )
    with _session_scope(
        profile=profile, chat_id=chat_id, project_id=""
    ):
        assert honcho_client._effective_workspace_id(config) == base

    result = []
    thread = threading.Thread(target=lambda: result.append(get_honcho_client(config)))
    thread.start()
    thread.join()

    assert build_count["n"] == 1
    assert result[0].kwargs["workspace_id"] == config._bound_workspace_id


def test_session_manager_reuses_captured_config_for_worker_refresh(monkeypatch):
    from plugins.memory.honcho import session as honcho_session

    config = HonchoClientConfig(write_frequency="turn")
    replacement = object()
    calls = []

    def fake_get_honcho_client(received_config=None):
        calls.append(received_config)
        return replacement

    monkeypatch.setattr(
        honcho_session, "get_honcho_client", fake_get_honcho_client
    )
    manager = honcho_session.HonchoSessionManager(
        honcho=object(), config=config
    )

    assert manager.honcho is replacement
    assert calls == [config]


def test_oauth_rotation_rebuilds_only_the_rotated_signature(monkeypatch, tmp_path):
    from plugins.memory.honcho import oauth

    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(
        monkeypatch, build_count, build_lock, disable_oauth=False
    )
    tokens = iter(
        [
            ("oauth-token-old", False),
            ("oauth-token-new", True),
            ("oauth-token-new", False),
            ("oauth-token-new", False),
        ]
    )
    monkeypatch.setattr(
        oauth, "ensure_fresh_token", lambda *args, **kwargs: next(tokens)
    )
    monkeypatch.setattr(oauth, "apply_token_to_client", lambda client, token: False)
    config_path = tmp_path / "honcho.json"
    config = HonchoClientConfig(
        host="profile-oauth",
        api_key="oauth-token-old",
        workspace_id="workspace-oauth",
        environment="production",
        timeout=30,
        config_path=config_path,
        hermes_home=tmp_path,
        raw={
            "hosts": {
                "profile-oauth": {
                    "oauth": {"refreshToken": "oauth-refresh-stable"}
                }
            }
        },
    )

    first = get_honcho_client(config)
    second = get_honcho_client(config)
    third = get_honcho_client(config)

    assert first is not second
    assert second is third
    assert build_count["n"] == 2
    assert [row["api_key"] for row in build_count["kwargs"]] == [
        "oauth-token-old",
        "oauth-token-new",
    ]


def test_reset_allows_rebuild(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)

    config = HonchoClientConfig(
        api_key="test-key", workspace_id="ws", environment="production"
    )

    c1 = get_honcho_client(config)
    assert build_count["n"] == 1
    # Cached: no rebuild.
    assert get_honcho_client(config) is c1
    assert build_count["n"] == 1

    reset_honcho_client()
    c2 = get_honcho_client(config)
    assert build_count["n"] == 2
    assert c2 is not c1


def test_reset_clears_all_keyed_clients(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)
    configs = [
        HonchoClientConfig(
            host="profile-a", api_key="key", workspace_id="a", timeout=30
        ),
        HonchoClientConfig(
            host="profile-b", api_key="key", workspace_id="b", timeout=30
        ),
    ]

    before = [get_honcho_client(config) for config in configs]
    assert build_count["n"] == 2

    reset_honcho_client()
    after = [get_honcho_client(config) for config in configs]

    assert build_count["n"] == 4
    assert all(new is not old for old, new in zip(before, after))


def test_missing_credentials_still_raises_before_build(monkeypatch):
    build_count = {"n": 0}
    build_lock = threading.Lock()
    _install_fake_honcho_sdk(monkeypatch, build_count, build_lock)

    bad = HonchoClientConfig(api_key="", base_url="", workspace_id="ws")
    with pytest.raises(ValueError):
        get_honcho_client(bad)
    assert build_count["n"] == 0
