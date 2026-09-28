import unittest
from types import SimpleNamespace as NS
import importlib.util
import os
import sys
from pathlib import Path

# Load the same plugin package used by the runtime, including relative imports.
# Editable installs need not expose built-in plugins as top-level packages.
root = Path(os.environ.get('HERMES_TEST_CODE_ROOT', str(Path(__file__).resolve().parents[1])))
plugin = root / 'plugins/platforms/telegram'
spec = importlib.util.spec_from_file_location('_heg_telegram_test', plugin / '__init__.py', submodule_search_locations=[str(plugin)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
TelegramAdapter = sys.modules['_heg_telegram_test.adapter'].TelegramAdapter

class ForumReplyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = object.__new__(TelegramAdapter)
        self.adapter._bot = NS(id=8559490361)

    def message(self, reply_id=16, topic=True, service=None, sender=8559490361):
        return NS(is_topic_message=topic, message_thread_id=16 if topic else None,
                  reply_to_message=NS(message_id=reply_id, from_user=NS(id=sender),
                                      forum_topic_created=service))

    def test_observed_heg_topic_root_is_not_explicit_reply(self):
        self.assertFalse(self.adapter._is_reply_to_bot(self.message()))

    def test_topic_service_without_thread_metadata_is_not_reply(self):
        self.assertFalse(self.adapter._is_reply_to_bot(self.message(topic=False, service=NS(name='EG Concursos'))))

    def test_real_reply_in_topic_is_preserved(self):
        self.assertTrue(self.adapter._is_reply_to_bot(self.message(reply_id=73)))

    def test_ordinary_group_reply_is_preserved(self):
        self.assertTrue(self.adapter._is_reply_to_bot(self.message(reply_id=73, topic=False)))

    def test_other_sender_is_not_bot_reply(self):
        self.assertFalse(self.adapter._is_reply_to_bot(self.message(reply_id=73, sender=123)))

if __name__ == '__main__':
    unittest.main()
