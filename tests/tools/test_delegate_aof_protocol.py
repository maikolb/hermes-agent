"""Worker prompts carry the delegated work protocol.

TARGET_ARCHITECTURE gap 6 (27/08): workers must run the SAME cycle as the
principal — scope, duplicate preflight, evidence-backed work, structured
closeout — instead of receiving a bare goal.
"""

from __future__ import annotations

import unittest

from tools.delegate_tool import _build_child_system_prompt


class TestDelegatedWorkProtocol(unittest.TestCase):
    def test_leaf_prompt_carries_full_cycle(self):
        prompt = _build_child_system_prompt("Corrigir o painel de atividade")
        self.assertIn("Delegated Work Protocol", prompt)
        self.assertIn("SCOPE", prompt)
        self.assertIn("PREFLIGHT", prompt)
        self.assertIn("EVIDENCE", prompt)
        self.assertIn("CLOSEOUT", prompt)

    def test_closeout_sections_are_spelled_out(self):
        prompt = _build_child_system_prompt("Qualquer tarefa")
        for section in ("Scope:", "Done:", "Evidence:", "Limitations:"):
            self.assertIn(section, prompt)

    def test_blocked_closeout_is_a_valid_outcome(self):
        # Spec: "Bloqueou? Move o card, manda closeout com o motivo, e a vida
        # dos outros segue" — the prompt must frame blocked as reportable.
        prompt = _build_child_system_prompt("Tarefa que pode bloquear")
        self.assertIn("blocked", prompt.lower())
        self.assertIn("valid outcome", prompt)

    def test_workers_are_told_cards_are_system_managed(self):
        # Workers have the kanban toolset blocked; their card is the mirror
        # card created by delegate_task. The prompt must prevent the worker
        # from wasting turns trying to manage cards it cannot reach.
        prompt = _build_child_system_prompt("Tarefa")
        self.assertIn("managed by the system", prompt)

    def test_orchestrator_keeps_protocol_and_delegation_block(self):
        prompt = _build_child_system_prompt(
            "Decompor auditoria", role="orchestrator",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertIn("Delegated Work Protocol", prompt)
        self.assertIn("Orchestrator Role", prompt)


if __name__ == "__main__":
    unittest.main()
