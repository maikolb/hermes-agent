"""Auth-boundary shard for the Anthropic adapter: the headers a real ``build_anthropic_client``
puts on the wire. Kept out of ``test_anthropic_adapter.py`` for the per-file line ceiling.
"""

import pytest

from agent.anthropic_adapter import build_anthropic_client


def wire_headers(client) -> dict:
    """Headers the SDK would put on a /v1/messages POST (the Omit() default is resolved here)."""
    from anthropic._models import FinalRequestOptions

    return dict(client._build_headers(FinalRequestOptions(method="post", url="/v1/messages", json_data={})))


@pytest.mark.parametrize(("family", "oauth"), [("api03", False), ("usr01", False), ("svc01", False), ("admin01", False), ("oat01", True)])
def test_console_key_families_carry_x_api_key_without_claude_code_identity(monkeypatch, family, oauth):
    """#133856: Console key families must not ride Bearer + the Claude Code beta/user-agent (that request
    shape bills the subscription bucket: 400 "credit balance too low"); OAuth setup tokens still do."""
    pytest.importorskip("anthropic")
    monkeypatch.setattr("agent.anthropic_adapter._claude_code_version_cache", "0.0.0")  # skip the `claude --version` probe
    key = "sk-" + "ant-" + family + "-" + "X" * 24
    headers = {k.lower(): v for k, v in wire_headers(build_anthropic_client(key, "https://api.anthropic.com")).items()}
    assert (headers.get("authorization") == "Bearer " + key) is oauth
    assert (headers.get("x-api-key") == key) is (not oauth)
    assert ("claude-code-20250219" in headers.get("anthropic-beta", "")) is oauth
    assert ("claude-code/" in headers.get("user-agent", "")) is oauth
