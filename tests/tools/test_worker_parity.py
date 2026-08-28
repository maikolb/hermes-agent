"""Worker parity with the principal (TARGET_ARCHITECTURE gap 10).

Production evidence (27/08): the principal stopped its own worker because
"the worker gets an isolated context, without the continuity and decisions
I hold in this session". Workers now inherit the principal's SOUL, a
read-only memory prefetch for their goal, and a session brief.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    _build_child_system_prompt,
    _collect_parent_parity_blocks,
    worker_parity_enabled,
)


def _parent(messages=None, memory=""):
    parent = MagicMock()
    parent.messages = messages or []
    manager = MagicMock()
    manager.prefetch_all.return_value = memory
    parent._memory_manager = manager
    return parent


class TestParityCollector(unittest.TestCase):
    def test_collects_soul_memory_and_brief(self):
        import tools.delegate_tool as dt

        parent = _parent(
            messages=[
                {"role": "user", "content": "decidimos usar o board dovcrm"},
                {"role": "assistant", "content": "fechado, sigo com dovcrm"},
                {
                    "role": "user",
                    "content": "[CONTEXT COMPACTION] resumo antigo",
                },
                {"role": "user", "content": "agora delega a auditoria"},
            ],
            memory="- fato durável: release pinado com prova",
        )
        fake_home = MagicMock()
        soul_file = MagicMock()
        soul_file.is_file.return_value = True
        soul_file.read_text.return_value = "# SOUL\nDisciplina card-first."
        fake_home.__truediv__ = lambda self, name: soul_file

        with patch("hermes_constants.get_hermes_home", return_value=fake_home):
            blocks = _collect_parent_parity_blocks(parent, "auditar limites")

        self.assertIn("card-first", blocks["soul"])
        self.assertIn("release pinado", blocks["memory"])
        parent._memory_manager.prefetch_all.assert_called_once_with(
            "auditar limites"
        )
        self.assertIn("delega a auditoria", blocks["session_brief"])
        # Compaction markers never enter the recent-exchanges tail; the
        # LATEST compaction summary is carried as its own section.
        self.assertIn("[CONTEXT COMPACTION] resumo antigo", blocks["session_brief"])
        self.assertNotIn(
            "user: [CONTEXT COMPACTION", blocks["session_brief"]
        )

    def test_collector_is_best_effort_per_block(self):
        parent = _parent()
        parent._memory_manager.prefetch_all.side_effect = RuntimeError("x")
        with patch(
            "hermes_constants.get_hermes_home",
            side_effect=RuntimeError("no home"),
        ):
            blocks = _collect_parent_parity_blocks(parent, "g")
        self.assertEqual(blocks["soul"], "")
        self.assertEqual(blocks["memory"], "")

    def test_blocks_are_size_capped(self):
        parent = _parent(memory="M" * 50_000)
        with patch(
            "hermes_constants.get_hermes_home",
            side_effect=RuntimeError("no home"),
        ):
            blocks = _collect_parent_parity_blocks(parent, "g")
        self.assertLess(len(blocks["memory"]), 10_000)
        self.assertIn("truncated for size", blocks["memory"])


class TestParityPrompt(unittest.TestCase):
    def test_prompt_carries_inherited_sections(self):
        prompt = _build_child_system_prompt(
            "Tarefa",
            parity={
                "soul": "Disciplina AOF",
                "memory": "fatos",
                "session_brief": "user: decidimos X",
            },
        )
        self.assertIn("Profile Operating Instructions", prompt)
        self.assertIn("Disciplina AOF", prompt)
        self.assertIn("Inherited Memory (read-only)", prompt)
        self.assertIn("Session Brief", prompt)
        self.assertIn("decidimos X", prompt)
        # Parity precedes the work protocol so conventions frame the task.
        self.assertLess(
            prompt.index("Profile Operating Instructions"),
            prompt.index("Delegated Work Protocol"),
        )

    def test_prompt_unchanged_without_parity(self):
        prompt = _build_child_system_prompt("Tarefa", parity=None)
        self.assertNotIn("Profile Operating Instructions", prompt)
        self.assertNotIn("Inherited Memory", prompt)
        self.assertNotIn("Session Brief", prompt)


class TestParityConfigGate(unittest.TestCase):
    def test_default_on_and_explicit_off(self):
        with patch(
            "tools.delegate_tool._load_config", return_value={}
        ):
            self.assertTrue(worker_parity_enabled())
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"worker_parity": False},
        ):
            self.assertFalse(worker_parity_enabled())


if __name__ == "__main__":
    unittest.main()
