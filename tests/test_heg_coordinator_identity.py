"""Regression: a coordinator is not a dispatcher worker without a task."""
import os
import unittest
from unittest.mock import patch

from agent.delegation_context import non_dispatcher_owned_context
from tools.kanban_tools import _is_dispatcher_owned_worker


class KanbanWorkerIdentityTests(unittest.TestCase):
    def test_coordinator_without_task_is_not_worker(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_dispatcher_owned_worker())

    def test_blank_task_is_not_worker(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "  "}, clear=True):
            self.assertFalse(_is_dispatcher_owned_worker())

    def test_real_worker_remains_restricted(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_test"}, clear=True):
            self.assertTrue(_is_dispatcher_owned_worker())

    def test_cron_context_does_not_own_inherited_task(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_test"}, clear=True):
            with non_dispatcher_owned_context():
                self.assertFalse(_is_dispatcher_owned_worker())


if __name__ == "__main__":
    unittest.main()
