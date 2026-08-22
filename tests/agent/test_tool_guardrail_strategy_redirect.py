"""Regression: failed tool routes redirect the same turn instead of halting it."""

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    append_toolguard_guidance,
)


def _controller(**overrides):
    values = {
        "hard_stop_enabled": True,
        "warnings_enabled": True,
        "same_tool_failure_halt_after": 2,
    }
    values.update(overrides)
    return ToolCallGuardrailController(ToolCallGuardrailConfig(**values))


def test_structural_read_failure_redirects_after_first_failure():
    guard = _controller()
    decision = guard.after_call(
        "read_file",
        {"path": "C:/missing/plugin.py"},
        '{"error":"File not found: C:/missing/plugin.py"}',
        failed=True,
    )
    assert decision.action == "redirect"
    assert decision.code == "structural_failure_redirect"
    assert decision.should_redirect
    assert not decision.should_halt

    same_route = guard.before_call("read_file", {"path": "C:/missing/plugin.py"})
    different_route = guard.before_call("read_file", {"path": "C:/known/plugin.py"})
    assert same_route.action == "redirect"
    assert different_route.action == "allow"


def test_second_distinct_structural_read_failure_redirects_whole_tool_route():
    guard = _controller()
    guard.after_call(
        "read_file",
        {"path": "C:/missing/one.py"},
        '{"error":"File not found: C:/missing/one.py"}',
        failed=True,
    )
    guard.after_call(
        "read_file",
        {"path": "C:/missing/two.py"},
        '{"error":"File not found: C:/missing/two.py"}',
        failed=True,
    )
    assert guard.before_call("read_file", {"path": "C:/third.py"}).action == "redirect"
    assert guard.before_call("skill_view", {"name": "hermes-agent"}).action == "allow"


def test_redirect_blocks_same_tool_but_keeps_other_tools_available():
    guard = _controller()
    guard.after_call(
        "search_files",
        {"pattern": "("},
        '{"error":"regex parse error: unclosed group"}',
        failed=True,
    )
    same_tool = guard.before_call("search_files", {"pattern": "different"})
    alternative = guard.before_call("skill_view", {"name": "hermes-agent"})
    assert same_tool.action == "redirect"
    assert not same_tool.allows_execution
    assert not same_tool.should_halt
    assert alternative.allows_execution


def test_reset_for_turn_clears_prior_redirects():
    guard = _controller()
    guard.after_call(
        "search_files",
        {"pattern": "("},
        '{"error":"regex parse error: unclosed group"}',
        failed=True,
    )
    assert guard.before_call("search_files", {"pattern": "different"}).should_redirect

    guard.reset_for_turn()

    assert guard.before_call("search_files", {"pattern": "different"}).allows_execution
    assert guard.before_call("skill_view", {"name": "hermes-agent"}).allows_execution


def test_repeated_generic_failure_redirects_instead_of_halting():
    guard = _controller(same_tool_failure_halt_after=2)
    first = guard.after_call("web_extract", {"urls": ["a"]}, "timeout", failed=True)
    second = guard.after_call("web_extract", {"urls": ["b"]}, "timeout", failed=True)
    assert first.action == "allow"
    assert second.action == "redirect"
    assert second.code == "same_tool_failure_redirect"
    assert not second.should_halt


def test_redirect_guidance_is_not_labelled_as_hard_stop():
    guard = _controller()
    decision = guard.after_call(
        "browser_navigate",
        {"url": "https://example.com"},
        '{"error":"Auto-launch failed: Chrome exited early"}',
        failed=True,
    )
    rendered = append_toolguard_guidance("failed", decision)
    assert "Tool route redirect" in rendered
    assert "hard stop" not in rendered.lower()
