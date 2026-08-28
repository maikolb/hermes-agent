from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"


def load_plugin():
    name = "zero_ui_guard_test_subject"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self): self.hooks = {}
    def register_hook(self, name, callback): self.hooks[name] = callback


class ZeroUiGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_plugin()
        self.guard._is_windows = lambda: True

    def test_registers_hooks(self):
        ctx = FakeContext(); self.guard.register(ctx)
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "pre_tool_call", "post_llm_call", "on_session_end"})

    def test_linux_registers_no_hooks(self):
        self.guard._is_windows = lambda: False
        ctx = FakeContext(); self.guard.register(ctx)
        self.assertEqual(ctx.hooks, {})

    def test_linux_injects_no_context_and_blocks_no_subprocess(self):
        self.guard._is_windows = lambda: False
        self.assertIsNone(self.guard._pre_llm_call(session_id="linux", user_message="Execute o trabalho."))
        self.assertIsNone(
            self.guard._pre_tool_call(
                "execute_code",
                {"code": "import subprocess; subprocess.run(['ffprobe', '-version'], check=True)"},
                session_id="linux",
            )
        )

    def test_venv_pythonw_is_blocked(self):
        result = self.guard._pre_tool_call("write_file", {"path": "launcher.vbs", "content": r"C:\Users\maiko\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe"})
        self.assertEqual(result["action"], "block")

    def test_subprocess_without_no_window_is_blocked(self):
        result = self.guard._pre_tool_call("execute_code", {"code": "subprocess.run(['git','status'])"})
        self.assertIn("CREATE_NO_WINDOW", result["message"])

    def test_subprocess_with_no_window_is_allowed(self):
        result = self.guard._pre_tool_call("execute_code", {"code": "subprocess.run(['git','status'], creationflags=subprocess.CREATE_NO_WINDOW)"})
        self.assertIsNone(result)

    def test_foreground_requires_current_message_approval(self):
        sid = "ui"
        self.guard._pre_llm_call(session_id=sid, user_message="Verifique em background.")
        self.assertEqual(self.guard._pre_tool_call("computer_use", {"action": "click", "delivery_mode": "foreground"}, session_id=sid)["action"], "block")
        self.guard._pre_llm_call(session_id=sid, user_message="Pode abrir a janela e trazer para frente.")
        self.assertIsNone(self.guard._pre_tool_call("computer_use", {"action": "click", "delivery_mode": "foreground"}, session_id=sid))


if __name__ == "__main__": unittest.main(verbosity=2)
