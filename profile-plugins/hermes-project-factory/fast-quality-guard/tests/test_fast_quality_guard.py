"""Regression tests for the fast-quality dispatcher guard.

Runs with stdlib unittest and can also be collected by pytest.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"


def load_plugin():
    name = "fast_quality_guard_test_subject"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class FastQualityGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_plugin()
        self.guard._reset_for_tests()

    def test_registers_dispatcher_hooks(self):
        ctx = FakeContext()
        self.guard.register(ctx)
        self.assertEqual(
            set(ctx.hooks),
            {"pre_llm_call", "pre_tool_call", "post_llm_call", "on_session_end"},
        )

    def test_reproduced_broad_pre_delivery_scan_redirects_without_blocking(self):
        sid = "delete-rejected-app"
        self.guard._pre_llm_call(
            session_id=sid,
            user_message="Delete completamente o Hermes Agent Project Ops agora.",
        )
        result = self.guard._pre_tool_call(
            "execute_code",
            {
                "code": (
                    "from pathlib import Path\n"
                    "active=Path(r'C:/Users/maiko/AppData/Local/hermes')\n"
                    "for p in active.rglob('*'): pass\n"
                )
            },
            session_id=sid,
        )
        self.assertIsNone(result)
        self.assertIn("broad discovery", self.guard._states[sid].last_redirect_reason)

    def test_bounded_target_inventory_is_allowed(self):
        sid = "bounded"
        self.guard._pre_llm_call(session_id=sid, user_message="Remova a pasta X.")
        result = self.guard._pre_tool_call(
            "execute_code",
            {"code": "from pathlib import Path\np=Path('C:/exact/target')\nprint(p.exists())"},
            session_id=sid,
        )
        self.assertIsNone(result)

    def test_sixth_discovery_redirects_without_blocking(self):
        sid = "discovery-budget"
        self.guard._pre_llm_call(session_id=sid, user_message="Corrija o arquivo exato.")
        for index in range(self.guard.DEFAULT_DISCOVERY_LIMIT):
            self.assertIsNone(
                self.guard._pre_tool_call(
                    "read_file", {"path": f"C:/exact/{index}.txt"}, session_id=sid
                )
            )
        result = self.guard._pre_tool_call(
            "read_file", {"path": "C:/exact/extra.txt"}, session_id=sid
        )
        self.assertIsNone(result)
        self.assertIn("discovery checkpoint", self.guard._states[sid].last_redirect_reason)

    def test_direct_action_is_allowed_within_budget(self):
        sid = "action"
        self.guard._pre_llm_call(session_id=sid, user_message="Corrija o arquivo exato.")
        result = self.guard._pre_tool_call(
            "patch",
            {"path": "C:/exact/file.txt", "old_string": "a", "new_string": "b"},
            session_id=sid,
        )
        self.assertIsNone(result)
        self.assertEqual(self.guard._states[sid].action_calls, 1)

    def test_literal_workspace_request_blocks_unrequested_security_bullshit(self):
        sid = "literal-workspace-request"
        request = (
            "Agora voltando ao hermes workspace app: vamos à próxima fase? Não consigo criar "
            "projeto, nem arquivar projeto ou renomear. Por exemplo: Project Factory é um time "
            "e não um projeto. Eu ia criar o DOVCRM lá ou renomear ou arquivar o Project Factory "
            "de lá. Também temos que conectar o hermes real lá de uma vez"
        )
        self.guard._pre_llm_call(session_id=sid, user_message=request)
        result = self.guard._pre_tool_call(
            "patch",
            {
                "path": "C:/Users/maiko/Projetos/Hermes Workspace Portal/serve.py",
                "old_string": "serve_workspace()",
                "new_string": "require_bearer_token_and_pairing()\nserve_workspace()",
            },
            session_id=sid,
        )
        self.assertEqual(result["action"], "block")
        self.assertIn("optional architecture/security expansion", result["reason"])

    def test_explicit_auth_request_allows_auth_work(self):
        sid = "requested-auth"
        self.guard._pre_llm_call(
            session_id=sid,
            user_message="Adicione autenticação por bearer token ao portal.",
        )
        result = self.guard._pre_tool_call(
            "patch",
            {
                "path": "C:/app/server.py",
                "old_string": "serve()",
                "new_string": "require_bearer_token()\nserve()",
            },
            session_id=sid,
        )
        self.assertIsNone(result)

    def test_removing_oversecurity_is_allowed(self):
        sid = "remove-oversecurity"
        self.guard._pre_llm_call(
            session_id=sid,
            user_message="Remova a camada desnecessária e preserve o app local.",
        )
        result = self.guard._pre_tool_call(
            "patch",
            {
                "path": "C:/app/server.py",
                "old_string": "require_bearer_token_and_pairing()\nserve()",
                "new_string": "serve()",
            },
            session_id=sid,
        )
        self.assertIsNone(result)

    def test_soft_deadline_redirects_more_discovery(self):
        sid = "soft-deadline"
        self.guard._pre_llm_call(session_id=sid, user_message="Resolva agora.")
        self.guard._age_for_tests(sid, self.guard.SOFT_SECONDS + 1)
        result = self.guard._pre_tool_call(
            "search_files", {"path": "C:/exact", "pattern": "x"}, session_id=sid
        )
        self.assertIsNone(result)
        self.assertIn("soft", self.guard._states[sid].last_redirect_reason)

    def test_hard_deadline_allows_current_tool_and_redirects_next_step(self):
        sid = "hard-deadline"
        self.guard._pre_llm_call(session_id=sid, user_message="Resolva agora.")
        self.guard._age_for_tests(sid, self.guard.HARD_SECONDS + 1)
        result = self.guard._pre_tool_call(
            "patch",
            {"path": "C:/exact/file.txt", "old_string": "a", "new_string": "b"},
            session_id=sid,
        )
        self.assertIsNone(result)
        self.assertEqual(self.guard._states[sid].action_calls, 1)
        self.assertIn("hard", self.guard._states[sid].last_redirect_reason)

    def test_research_turn_allows_calls_and_redirects_python_recursion(self):
        sid = "research"
        self.guard._pre_llm_call(
            session_id=sid,
            user_message="Pesquise e audite profundamente esta arquitetura.",
        )
        self.assertTrue(self.guard._states[sid].allow_broad)
        self.assertEqual(
            self.guard._states[sid].discovery_limit,
            self.guard.RESEARCH_DISCOVERY_LIMIT,
        )
        recursive = self.guard._pre_tool_call(
            "execute_code",
            {"code": "from pathlib import Path\nfor p in Path('.').rglob('*.py'): pass"},
            session_id=sid,
        )
        self.assertIsNone(recursive)
        self.assertIn("broad discovery", self.guard._states[sid].last_redirect_reason)
        indexed = self.guard._pre_tool_call(
            "search_files",
            {"path": "C:/Users/maiko/AppData/Local/hermes", "pattern": "*"},
            session_id=sid,
        )
        self.assertIsNone(indexed)


    def test_post_llm_clears_turn_state(self):
        sid = "cleanup"
        self.guard._pre_llm_call(session_id=sid, user_message="Faça X.")
        self.assertIn(sid, self.guard._states)
        self.guard._post_llm_call(session_id=sid)
        self.assertNotIn(sid, self.guard._states)


if __name__ == "__main__":
    unittest.main(verbosity=2)
