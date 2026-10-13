"""A profile with its own openai-codex pool does not borrow the root's Codex login.

``_load_provider_state`` lends a profile the root's ``providers.openai-codex``
when the profile has none.  On the Codex login path (``_read_codex_tokens``,
``resolve_codex_runtime_credentials``, ``_save_codex_tokens``) that loan let a
profile that authenticates through its own ``credential_pool.openai-codex``
refresh the ROOT's single-use refresh token and save the root's state (label,
``last_auth_error``) into the profile.  ``hermes auth remove`` suppressing
``device_code`` did not stop it, because only pool seeding read the
suppression.  The ``api_mode`` the auxiliary fallback asked for never depended
on the loan: the pool answers ``codex_responses`` on its own.

Every token here is a local stand-in and the token endpoint is a recording
double: nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli.auth import AuthError

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
DAY = 86400


def _jwt(exp: float, sub: str) -> str:
    claims = {"exp": int(exp), "sub": sub}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


ROOT_ACCESS_EXPIRED = _jwt(time.time() - 10 * DAY, "root")
ROOT_ACCESS_FRESH = _jwt(time.time() + 5 * DAY, "root")
POOL_ACCESS = [_jwt(time.time() + 9 * DAY, f"pool-{n}") for n in (1, 2, 3)]


def _root_login(access_token: str) -> dict:
    return {
        "tokens": {"access_token": access_token, "refresh_token": "root-rt-1"},
        "last_refresh": "2026-09-23T17:50:09Z",
        "auth_mode": "chatgpt",
        "label": "Codex Desktop account",
        "last_auth_error": {
            "provider": "openai-codex",
            "code": "refresh_token_reused",
            "at": "2026-06-26T12:00:00+00:00",
        },
    }


def _pool_rows(source: str = "manual:device_code") -> list:
    return [
        {
            "id": f"e{n}",
            "label": f"nfos-codex-{n}",
            "auth_type": "oauth",
            "priority": n - 1,
            "source": source,
            "access_token": POOL_ACCESS[n - 1],
            "refresh_token": f"pool-rt-{n}",
            "base_url": CODEX_BASE,
            "last_refresh": "2026-10-12T23:45:00.000000Z",
            "request_count": 0,
        }
        for n in (1, 2, 3)
    ]


SUPPRESSED = {"openai-codex": ["device_code", "manual:device_code"]}


class _TokenEndpoint:
    """Records every refresh token presented; answers with a new local pair."""

    def __init__(self):
        self.refreshed = []

    def __call__(self, access_token, refresh_token, *, timeout_seconds=20.0):
        self.refreshed.append(refresh_token)
        return {
            "access_token": _jwt(time.time() + 10 * DAY, "rotated"),
            "refresh_token": refresh_token + "-next",
            "last_refresh": "2026-10-13T16:55:43.660365Z",
        }


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    """Production layout: HERMES_HOME=<root>/profiles/<name>, root store at <root>/auth.json."""
    root = tmp_path / "hermes"
    profile = root / "profiles" / "hermes-project-factory"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-cli"))
    monkeypatch.delenv("HERMES_CODEX_BASE_URL", raising=False)
    endpoint = _TokenEndpoint()
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", endpoint)
    return root, profile, endpoint


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def _store(*, providers=None, pool=None, suppressed=None) -> dict:
    # ``hermes auth remove`` leaves an empty ``providers`` mapping behind, and
    # _load_auth_store discards a store that has neither providers nor a pool.
    store = {"version": 1, "active_provider": "openai-codex", "providers": providers or {}}
    if pool is not None:
        store["credential_pool"] = {"openai-codex": pool}
    if suppressed is not None:
        store["suppressed_sources"] = suppressed
    return store


def _arrange(layout, *, root_login=None, profile_login=None, pool=None, suppressed=None):
    root, profile, endpoint = layout
    root_providers = {"openai-codex": root_login} if root_login is not None else {}
    _write(root / "auth.json", _store(providers=root_providers))
    profile_providers = {"openai-codex": profile_login} if profile_login is not None else {}
    _write(profile / "auth.json", _store(providers=profile_providers, pool=pool, suppressed=suppressed))
    assert auth_mod._global_auth_file_path() == root / "auth.json"
    return root, profile, endpoint, (root / "auth.json").read_bytes()


def _profile_login(profile: Path):
    store = json.loads((profile / "auth.json").read_text())
    return (store.get("providers") or {}).get("openai-codex")


# --------------------------------------------------------------------------
# A profile with its own pool: the root's login is neither read nor written.
# --------------------------------------------------------------------------


def test_pool_profile_with_suppression_does_not_refresh_root_login(layout):
    """The production profile: three manual:device_code rows, device_code suppressed."""
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(), suppressed=SUPPRESSED,
    )

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == []
    assert creds["source"] == "credential_pool"
    assert creds["api_key"] == POOL_ACCESS[0]
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_pool_profile_without_suppression_does_not_refresh_root_login(layout):
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(),
    )

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == []
    assert creds["source"] == "credential_pool"
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_suppressed_device_code_blocks_root_login_without_a_pool(layout):
    """``hermes auth remove`` suppressing device_code also covers the root's login."""
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), suppressed=SUPPRESSED,
    )

    with pytest.raises(AuthError) as exc:
        auth_mod.resolve_codex_runtime_credentials()

    assert exc.value.code == "codex_auth_missing"
    assert endpoint.refreshed == []
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_pool_profile_reads_no_root_login(layout):
    _arrange(layout, root_login=_root_login(ROOT_ACCESS_FRESH), pool=_pool_rows(), suppressed=SUPPRESSED)

    with pytest.raises(AuthError) as exc:
        auth_mod._read_codex_tokens()

    assert exc.value.code == "codex_auth_missing"


def test_pool_profile_with_fresh_root_login_uses_its_pool(layout):
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_FRESH), pool=_pool_rows(), suppressed=SUPPRESSED,
    )

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == []
    assert creds["source"] == "credential_pool"
    assert creds["api_key"] == POOL_ACCESS[0]
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_save_codex_tokens_in_pool_profile_does_not_write_root_state(layout):
    root, profile, _endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(), suppressed=SUPPRESSED,
    )
    new_access = _jwt(time.time() + 10 * DAY, "profile-login")

    auth_mod._save_codex_tokens({"access_token": new_access, "refresh_token": "profile-rt-1"})

    saved = _profile_login(profile)
    assert saved["tokens"] == {"access_token": new_access, "refresh_token": "profile-rt-1"}
    assert "label" not in saved
    assert "last_auth_error" not in saved
    assert (root / "auth.json").read_bytes() == root_before


def test_runtime_provider_api_mode_comes_from_the_pool(layout):
    """The question the auxiliary fallback asks: which api_mode, with the explicit Codex base."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(), suppressed=SUPPRESSED,
    )

    runtime = resolve_runtime_provider(
        requested="openai-codex", explicit_base_url=CODEX_BASE, target_model="gpt-5.6-luna-900k",
    )

    assert runtime["api_mode"] == "codex_responses"
    assert runtime["api_key"] == POOL_ACCESS[0]
    assert endpoint.refreshed == []
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_auxiliary_fallback_destination_does_not_copy_root_login(layout):
    """The 13/10 16:55:43Z chain: _complete_fallback_destination only wants the api_mode."""
    from agent.auxiliary_client import _complete_fallback_destination

    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(), suppressed=SUPPRESSED,
    )

    dest = _complete_fallback_destination("openai-codex", CODEX_BASE, None, "gpt-5.6-luna-900k")

    assert dest.api_mode == "codex_responses"
    assert endpoint.refreshed == []
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


# --------------------------------------------------------------------------
# Controls: what stays as it was.
# --------------------------------------------------------------------------


def test_profile_without_pool_still_borrows_root_login(layout):
    root, _profile, endpoint, _root_before = _arrange(layout, root_login=_root_login(ROOT_ACCESS_EXPIRED))

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == ["root-rt-1"]
    assert creds["source"] == "hermes-auth-store"


def test_profile_with_only_the_device_code_mirror_still_borrows_root_login(layout):
    """A device_code row is the pool's mirror of the login itself, not a pool of the profile's own."""
    mirror = _pool_rows(source="device_code")[:1]
    mirror[0]["access_token"] = ROOT_ACCESS_EXPIRED
    mirror[0]["refresh_token"] = "root-rt-1"
    _root, _profile, endpoint, _root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=mirror,
    )

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == ["root-rt-1"]
    assert creds["source"] == "hermes-auth-store"


def test_pool_profile_keeps_using_its_own_login(layout):
    own = {
        "tokens": {"access_token": _jwt(time.time() - DAY, "own"), "refresh_token": "own-rt-1"},
        "last_refresh": "2026-10-13T16:55:43.660365Z",
        "auth_mode": "chatgpt",
        "label": "profile login",
    }
    root, profile, endpoint, root_before = _arrange(
        layout,
        root_login=_root_login(ROOT_ACCESS_EXPIRED),
        profile_login=own,
        pool=_pool_rows(),
        suppressed=SUPPRESSED,
    )

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == ["own-rt-1"]
    assert creds["source"] == "hermes-auth-store"
    saved = _profile_login(profile)
    assert saved["label"] == "profile login"
    assert saved["tokens"]["refresh_token"] == "own-rt-1-next"
    assert (root / "auth.json").read_bytes() == root_before


def test_pool_profile_without_root_login_uses_its_pool(layout):
    root, profile, endpoint, root_before = _arrange(layout, pool=_pool_rows(), suppressed=SUPPRESSED)

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert endpoint.refreshed == []
    assert creds["source"] == "credential_pool"
    assert creds["api_key"] == POOL_ACCESS[0]
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def test_classic_store_with_pool_and_suppression_keeps_its_login(tmp_path, monkeypatch):
    """Without a profile there is no root to borrow from: the store's own login is read."""
    home = tmp_path / "classic"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    own_access = _jwt(time.time() + 5 * DAY, "classic")
    _write(home / "auth.json", _store(
        providers={"openai-codex": {"tokens": {"access_token": own_access, "refresh_token": "classic-rt"}}},
        pool=_pool_rows(),
        suppressed=SUPPRESSED,
    ))
    assert auth_mod._global_auth_file_path() is None

    data = auth_mod._read_codex_tokens()

    assert data["tokens"]["access_token"] == own_access
