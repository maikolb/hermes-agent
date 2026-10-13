"""A profile with its own openai-codex pool does not borrow the root's Codex login.

``_load_provider_state`` lends a profile the root's ``providers.openai-codex``
when the profile has none.  On the Codex login path (``_read_codex_tokens``,
``resolve_codex_runtime_credentials``, ``_save_codex_tokens``) that loan let a
profile that authenticates through its own ``credential_pool.openai-codex``
refresh the ROOT's single-use refresh token and save the root's state (label,
``last_auth_error``) into the profile.  ``hermes auth remove`` suppressing
``device_code`` did not stop it, because only pool seeding read the
suppression.  The ``api_mode`` the auxiliary fallback asked for never depended
on the loan: the pool answers ``codex_responses`` on its own.  The pool read
the same loan too: seeding a ``device_code`` row from the root's login, a row
adopting the root's newer pair, and the terminal-refresh cleanup.

Every token here is a local stand-in, the token endpoint is a recording
double and the Codex route is a local HTTP double: nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli.auth import AuthError

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
DAY = 86400


def _jwt(exp: float, sub: str, account: str | None = None) -> str:
    claims = {"exp": int(exp), "sub": sub}
    if account:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": account}
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
    """Records every refresh token presented; answers with a new local pair.

    A token in ``spent`` gets the terminal ``refresh_token_reused`` answer.
    """

    def __init__(self):
        self.refreshed = []
        self.spent = set()

    def __call__(self, access_token, refresh_token, *, timeout_seconds=20.0):
        self.refreshed.append(refresh_token)
        if refresh_token in self.spent:
            raise AuthError(
                "refresh token already used",
                provider="openai-codex",
                code="refresh_token_reused",
                relogin_required=True,
            )
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


def test_legacy_mapping_suppression_blocks_root_login(layout):
    """Older stores keep a provider's suppressed sources as a mapping (see suppress_credential_source)."""
    root, profile, endpoint, root_before = _arrange(
        layout,
        root_login=_root_login(ROOT_ACCESS_EXPIRED),
        suppressed={"openai-codex": {"device_code": True, "manual:device_code": True}},
    )

    with pytest.raises(AuthError) as exc:
        auth_mod.resolve_codex_runtime_credentials()

    assert exc.value.code == "codex_auth_missing"
    assert endpoint.refreshed == []
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


# --------------------------------------------------------------------------
# The pool reads the login too: seeding, adoption by a row, terminal cleanup.
# --------------------------------------------------------------------------

NEXA_ACCOUNT = "acct-nexa"
ROOT_REFRESH_CHAIN = {"root-rt-1", "root-rt-1-next"}


def _pool_refresh_tokens(profile: Path) -> list:
    store = json.loads((profile / "auth.json").read_text())
    return [row.get("refresh_token") for row in (store.get("credential_pool") or {}).get("openai-codex", [])]


def _root_newer_than_row_one(*, root_access_expired: bool):
    """Row 1 and the root are one account; the root rotated after row 1, whose access token expired."""
    root_exp = time.time() + (-DAY if root_access_expired else 5 * DAY)
    root_login = _root_login(_jwt(root_exp, "user-nexa", NEXA_ACCOUNT))
    root_login["last_refresh"] = "2026-10-13T12:00:00Z"
    rows = _pool_rows()
    rows[0]["access_token"] = _jwt(time.time() - 3600, "user-nexa", NEXA_ACCOUNT)
    return root_login, rows


@pytest.mark.parametrize("root_access_expired", [True, False], ids=["root-expired", "root-fresh"])
def test_pool_row_does_not_adopt_a_newer_root_login(layout, root_access_expired):
    from agent.credential_pool import load_pool

    root_login, rows = _root_newer_than_row_one(root_access_expired=root_access_expired)
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=root_login, pool=rows, suppressed=SUPPRESSED,
    )

    load_pool("openai-codex").select()

    assert endpoint.refreshed == ["pool-rt-1"]
    assert _pool_refresh_tokens(profile) == ["pool-rt-1-next", "pool-rt-2", "pool-rt-3"]
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def _select_from_pool():
    from agent.credential_pool import load_pool

    load_pool("openai-codex").select()


def _resolve_main_runtime():
    """Main agent, gateway, cron and delegation: resolve_runtime_provider without an explicit base."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    resolve_runtime_provider(requested="openai-codex")


@pytest.mark.parametrize("resolve", [_select_from_pool, _resolve_main_runtime], ids=["pool", "main-runtime"])
def test_pool_profile_without_suppression_does_not_seed_root_login(layout, resolve):
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=_pool_rows(),
    )

    resolve()

    assert _pool_refresh_tokens(profile) == ["pool-rt-1", "pool-rt-2", "pool-rt-3"]
    assert endpoint.refreshed == []
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


def _dying_row(rows: list, dying: str) -> tuple:
    """The row whose refresh is refused for good, and its spent refresh token."""
    if dying == "mirror":
        # A device_code row copied from the root before the profile got its pool.
        rows.append(dict(
            rows[0],
            id="mirror",
            label="Codex Desktop account",
            source="device_code",
            priority=3,
            access_token=ROOT_ACCESS_EXPIRED,
            refresh_token="root-rt-1",
            last_refresh="2026-09-23T17:50:09Z",
        ))
        return rows, "root-rt-1"
    # One of the profile's own logins, expired and revoked.
    rows[0]["access_token"] = _jwt(time.time() - 3600, "user-nexa", NEXA_ACCOUNT)
    return rows, "pool-rt-1"


@pytest.mark.parametrize("dying", ["mirror", "manual"])
def test_terminal_refresh_cleanup_writes_no_login_block_into_pool_profile(layout, dying):
    """A row dies on refresh in a pool profile without its own login while the root has one."""
    from agent.credential_pool import load_pool

    rows, spent = _dying_row(_pool_rows(), dying)
    root, profile, endpoint, root_before = _arrange(
        layout, root_login=_root_login(ROOT_ACCESS_EXPIRED), pool=rows, suppressed=SUPPRESSED,
    )
    endpoint.spent.add(spent)

    load_pool("openai-codex").select()

    assert spent in endpoint.refreshed
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before

    # An empty block would send the next resolution to the Codex CLI login.
    cli_home = Path(os.environ["CODEX_HOME"])
    cli_home.mkdir(parents=True, exist_ok=True)
    _write(cli_home / "auth.json", {"tokens": {
        "access_token": _jwt(time.time() + 5 * DAY, "cli"), "refresh_token": "cli-rt-1",
    }})

    creds = auth_mod.resolve_codex_runtime_credentials()

    assert creds["source"] == "credential_pool"
    assert _profile_login(profile) is None
    assert (root / "auth.json").read_bytes() == root_before


# --------------------------------------------------------------------------
# The production path end to end: call_llm(task="compression") gets the
# Codex 400 and walks auxiliary.compression.fallback_chain.
# --------------------------------------------------------------------------

COMPRESSION_CONFIG = """model:
  default: gpt-6-astra
  provider: openai-codex
  base_url: https://chatgpt.com/backend-api/codex
fallback_providers: []
auxiliary:
  free_only: true
  compression:
    provider: openai-codex
    model: gpt-5.4-900k
    fallback_chain:
      - provider: openai-codex
        model: gpt-5.6-luna-900k
      - provider: openai-codex
        model: gpt-5.6-terra-900k
      - provider: openai-codex
        model: gpt-5.6-sol-900k
    base_url: ''
    timeout: 60
codex:
  use_credits: false
"""


class _CodexRoute:
    """Local Codex responses route: every model gets the 13/10 400; records model and bearer."""

    def __init__(self):
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    model = json.loads(raw.decode() or "{}").get("model") or ""
                except ValueError:
                    model = ""
                auth = str(self.headers.get("Authorization") or "")
                calls.append((model, auth[7:].strip() if auth.lower().startswith("bearer ") else auth))
                body = json.dumps({
                    "detail": f"The '{model}' model is not supported when using Codex with a ChatGPT account."
                }).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/backend-api/codex"


@pytest.fixture()
def codex_route(monkeypatch):
    import agent.auxiliary_client as aux_mod

    route = _CodexRoute()
    create = aux_mod._create_openai_client

    def _local(*args, **kwargs):
        base = str(kwargs.get("base_url") or "").rstrip("/")
        if base.startswith(CODEX_BASE):
            kwargs["base_url"] = route.base + base[len(CODEX_BASE):]
        return create(*args, **kwargs)

    monkeypatch.setattr(aux_mod, "_create_openai_client", _local)
    aux_mod.shutdown_cached_clients()
    aux_mod._aux_unhealthy_until.clear()
    aux_mod._aux_unhealthy_logged_at.clear()
    yield route
    aux_mod.shutdown_cached_clients()
    aux_mod._aux_unhealthy_until.clear()
    aux_mod._aux_unhealthy_logged_at.clear()
    route.server.shutdown()
    route.server.server_close()


@pytest.mark.parametrize("setup", ["suppressed", "unsuppressed", "root-newer"])
def test_compression_fallback_in_pool_profile_leaves_root_login_alone(layout, codex_route, setup):
    from agent.auxiliary_client import call_llm

    if setup == "root-newer":
        root_login, rows = _root_newer_than_row_one(root_access_expired=True)
    else:
        root_login, rows = _root_login(ROOT_ACCESS_EXPIRED), _pool_rows()
    root, profile, endpoint, root_before = _arrange(
        layout,
        root_login=root_login,
        pool=rows,
        suppressed=None if setup == "unsuppressed" else SUPPRESSED,
    )
    (profile / "config.yaml").write_text(COMPRESSION_CONFIG)
    root_access = root_login["tokens"]["access_token"]

    try:
        call_llm(
            task="compression",
            main_runtime={
                "model": "gpt-6-astra",
                "provider": "openai-codex",
                "base_url": CODEX_BASE,
                "api_key": POOL_ACCESS[1],
                "api_mode": "codex_responses",
            },
            messages=[{"role": "user", "content": "summarise the conversation"}],
        )
    except Exception:
        pass  # every model answers the 400, as on 13/10

    models = [model for model, _bearer in codex_route.calls]
    assert any(model.startswith("gpt-5.4") for model in models)
    assert any(model.startswith("gpt-5.6-luna") for model in models)
    assert not ROOT_REFRESH_CHAIN & set(endpoint.refreshed)
    assert root_access not in [bearer for _model, bearer in codex_route.calls]
    assert _profile_login(profile) is None
    assert not ROOT_REFRESH_CHAIN & set(_pool_refresh_tokens(profile))
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
