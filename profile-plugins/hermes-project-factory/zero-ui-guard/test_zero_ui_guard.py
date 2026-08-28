"""Regression tests for capability-preserving zero-UI pre-dispatch policy."""

import importlib.util
from pathlib import Path


PLUGIN = Path(__file__).with_name("__init__.py")
SPEC = importlib.util.spec_from_file_location("zero_ui_guard_plugin", PLUGIN)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_process_tools_are_not_blocked_by_name():
    assert MODULE._pre_tool_call("terminal", {"command": "printf ok"}) is None
    assert MODULE._pre_tool_call("execute_code", {"code": "print('ok')"}) is None


def test_execute_code_unsafe_child_creation_is_still_blocked():
    result = MODULE._pre_tool_call(
        "execute_code",
        {"code": "import subprocess; subprocess.run(['example'])"},
    )
    assert result and result["action"] == "block"


def test_static_patch_text_is_not_treated_as_execution():
    fixture = "windows " + "terminal"
    result = MODULE._pre_tool_call(
        "patch",
        {"path": "module.py", "old_string": "x", "new_string": fixture},
    )
    assert result is None


def test_foreground_control_still_requires_explicit_approval():
    result = MODULE._pre_tool_call(
        "computer_use",
        {"action": "click", "delivery_mode": "foreground"},
        session_id="s1",
    )
    assert result and result["action"] == "block"
