"""``anthropic-workspace-id`` for identity-backed Anthropic Console keys.

Personal (``sk-ant-usr*``) and service-account (``sk-ant-svc*``) keys that are not
scoped to a single workspace get 400 invalid_request_error unless every request
names the workspace it acts in. Hermes reads that ID from
``providers.anthropic.workspace_id`` (config.yaml) or ``ANTHROPIC_WORKSPACE_ID``
and sends it only with Console API keys on the native Anthropic API. Every key
below is a synthetic string; no real credential is used or written.
"""

import json
import logging
from types import SimpleNamespace

import pytest
import yaml

from hermes_constants import get_hermes_home
from agent.anthropic_adapter import (
    _CLAUDE_CODE_SYSTEM_PREFIX,
    _is_oauth_token,
    anthropic_workspace_error_hint,
    anthropic_workspace_headers,
    build_anthropic_client,
    resolve_anthropic_workspace_id,
    with_anthropic_workspace_hint,
)

USR_KEY = "sk-" + "ant-usr01-" + "X" * 24
SVC_KEY = "sk-" + "ant-svc01-" + "X" * 24
OAT_KEY = "sk-" + "ant-oat01-" + "X" * 24
WORKSPACE = "wrkspc_01TestConfigWorkspace"
ENV_WORKSPACE = "wrkspc_01TestEnvWorkspace"

# The API's rejection of an unscoped identity key without the header: the text
# measured on GET /v1/models (12/10/2026) and the one in the authentication docs.
MISSING_WORKSPACE_MESSAGES = [
    "This API key is not scoped to a workspace, so this request must include the "
    "anthropic-workspace-id header",
    "anthropic-workspace-id is required when authenticating with an identity-linked "
    "API key; send the id of the workspace this request acts in.",
    "anthropic-workspace-id header must be a valid workspace ID.",
]


@pytest.fixture(autouse=True)
def _no_ambient_workspace(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    # Never shell out to `claude --version` when an OAuth client is built.
    monkeypatch.setattr("agent.anthropic_adapter._claude_code_version_cache", "0.0.0")


def _write_config(data: dict) -> None:
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _configure_workspace(workspace: str = WORKSPACE) -> None:
    _write_config({"providers": {"anthropic": {"workspace_id": workspace}}})


def wire_headers(client) -> dict:
    """Lower-cased headers the SDK would put on a /v1/messages POST."""
    from anthropic._models import FinalRequestOptions

    raw = client._build_headers(FinalRequestOptions(method="post", url="/v1/messages", json_data={}))
    return {k.lower(): v for k, v in dict(raw).items()}


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [USR_KEY, SVC_KEY])
def test_identity_backed_keys_are_api_keys_not_oauth(key):
    assert _is_oauth_token(key) is False


# ── where the workspace ID comes from ───────────────────────────────────────


def test_workspace_id_unset_resolves_empty():
    assert resolve_anthropic_workspace_id() == ""


def test_workspace_id_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", f"  {ENV_WORKSPACE}  ")
    assert resolve_anthropic_workspace_id() == ENV_WORKSPACE


def test_config_yaml_workspace_id_wins_over_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", ENV_WORKSPACE)
    _configure_workspace()
    assert resolve_anthropic_workspace_id() == WORKSPACE


def test_workspace_id_is_a_known_provider_key(caplog):
    """``providers.anthropic.workspace_id`` must not be reported as an ignored key."""
    from hermes_cli.config import _normalize_custom_provider_entry

    with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
        _normalize_custom_provider_entry({"workspace_id": WORKSPACE}, provider_key="anthropic")
    assert "unknown config keys" not in caplog.text


# ── the header on the wire ──────────────────────────────────────────────────


@pytest.mark.parametrize("key", [USR_KEY, SVC_KEY])
@pytest.mark.parametrize("base_url", [None, "https://api.anthropic.com", "https://api.anthropic.com/v1"])
def test_identity_key_on_native_api_sends_configured_workspace(key, base_url):
    pytest.importorskip("anthropic")
    _configure_workspace()
    headers = wire_headers(build_anthropic_client(key, base_url))
    assert headers.get("anthropic-workspace-id") == WORKSPACE
    assert headers.get("x-api-key") == key
    assert "authorization" not in headers


def test_env_workspace_reaches_the_wire(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", ENV_WORKSPACE)
    headers = wire_headers(build_anthropic_client(USR_KEY, "https://api.anthropic.com"))
    assert headers.get("anthropic-workspace-id") == ENV_WORKSPACE


def test_no_workspace_configured_sends_no_header():
    """A single-workspace key needs no header, so nothing is invented."""
    pytest.importorskip("anthropic")
    headers = wire_headers(build_anthropic_client(USR_KEY, "https://api.anthropic.com"))
    assert "anthropic-workspace-id" not in headers
    assert headers.get("x-api-key") == USR_KEY


def test_oauth_token_never_carries_the_workspace_header():
    pytest.importorskip("anthropic")
    _configure_workspace()
    headers = wire_headers(build_anthropic_client(OAT_KEY, "https://api.anthropic.com"))
    assert "anthropic-workspace-id" not in headers
    assert headers.get("authorization") == "Bearer " + OAT_KEY


@pytest.mark.parametrize(
    "key, base_url",
    [
        ("minimax-key-1234567890", "https://api.minimax.io/anthropic"),
        (USR_KEY, "https://llm-proxy.example.com/anthropic"),
    ],
)
def test_third_party_endpoints_never_carry_the_workspace_header(key, base_url):
    pytest.importorskip("anthropic")
    _configure_workspace()
    assert "anthropic-workspace-id" not in wire_headers(build_anthropic_client(key, base_url))
    assert anthropic_workspace_headers(key, base_url) == {}


def test_native_anthropic_agent_with_identity_key_has_no_claude_code_identity(monkeypatch):
    """The real AIAgent path: provider ``anthropic`` + an ``sk-ant-usr`` key must go out as a
    plain API key (x-api-key, workspace header) with no Claude Code headers or system prompt."""
    pytest.importorskip("anthropic")
    from unittest.mock import patch

    from run_agent import AIAgent

    _configure_workspace()
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("agent.model_metadata._query_anthropic_context_length", return_value=None),
    ):
        agent = AIAgent(
            api_key=USR_KEY,
            base_url="https://api.anthropic.com",
            provider="anthropic",
            api_mode="anthropic_messages",
            model="claude-sonnet-5-5",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent._is_anthropic_oauth is False
    headers = wire_headers(agent._anthropic_client)
    assert headers.get("x-api-key") == USR_KEY
    assert headers.get("anthropic-workspace-id") == WORKSPACE
    assert "authorization" not in headers
    assert "x-app" not in headers
    assert "claude-code" not in headers.get("user-agent", "")
    assert "claude-code-20250219" not in headers.get("anthropic-beta", "")
    assert "oauth-2025-04-20" not in headers.get("anthropic-beta", "")

    kwargs = agent._build_api_kwargs(
        [{"role": "system", "content": "You are Hermes."}, {"role": "user", "content": "hi"}]
    )
    assert _CLAUDE_CODE_SYSTEM_PREFIX not in json.dumps(kwargs.get("system"))


# ── sibling /v1/models callers ──────────────────────────────────────────────


def test_model_catalog_fetch_sends_workspace_header(monkeypatch):
    import hermes_cli.models as models

    _configure_workspace()
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "claude-sonnet-5-5"}]}).encode()

    def _fake_urlopen(req, *, timeout, ssl_context=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(models, "_urlopen_model_catalog_request", _fake_urlopen)
    assert models._fetch_anthropic_models(api_key=USR_KEY) == ["claude-sonnet-5-5"]
    assert captured["headers"].get("anthropic-workspace-id") == WORKSPACE
    assert captured["headers"].get("x-api-key") == USR_KEY


def test_context_length_probe_sends_workspace_header(monkeypatch):
    import agent.model_metadata as mm

    _configure_workspace()
    captured = {}

    def _fake_get(url, headers=None, timeout=None, verify=None):
        captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": "claude-sonnet-5-5", "max_input_tokens": 1000000}]},
        )

    monkeypatch.setattr(mm, "requests", SimpleNamespace(get=_fake_get), raising=False)
    assert mm._query_anthropic_context_length("claude-sonnet-5-5", "https://api.anthropic.com", USR_KEY) == 1000000
    assert captured["headers"].get("anthropic-workspace-id") == WORKSPACE


# ── a clear error when the key needs a workspace ────────────────────────────


@pytest.mark.parametrize("message", MISSING_WORKSPACE_MESSAGES)
def test_missing_workspace_rejection_names_the_hermes_setting(message):
    summary = with_anthropic_workspace_hint(f"HTTP 400: {message}")
    assert summary.startswith(f"HTTP 400: {message}")
    assert "providers.anthropic.workspace_id" in summary
    assert "ANTHROPIC_WORKSPACE_ID" in summary
    assert with_anthropic_workspace_hint(summary) == summary  # idempotent


def test_unknown_workspace_rejection_points_at_the_configured_id():
    hint = anthropic_workspace_error_hint("HTTP 404: Workspace `wrkspc_01Nope` not found.")
    assert "does not exist" in hint
    assert "providers.anthropic.workspace_id" in hint


@pytest.mark.parametrize(
    "detail",
    [
        "HTTP 400: prompt is too long: 250000 tokens > 200000 maximum",
        "HTTP 404: model: claude-nope",
        # Another provider's workspace 404 must not get Anthropic advice.
        "HTTP 404: Workspace `acme-prod` not found.",
    ],
)
def test_unrelated_errors_are_left_alone(detail):
    assert with_anthropic_workspace_hint(detail) == detail


def test_agent_error_summary_carries_the_hint_for_a_real_sdk_error():
    anthropic = pytest.importorskip("anthropic")
    import httpx

    from run_agent import AIAgent

    message = MISSING_WORKSPACE_MESSAGES[0]
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(
        400, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    error = anthropic.BadRequestError(message, response=response, body=body)

    summary = AIAgent._summarize_api_error(error)
    assert summary.startswith(f"HTTP 400: {message}")
    assert "providers.anthropic.workspace_id" in summary
