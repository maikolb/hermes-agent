import asyncio

from plugins.platforms.telegram.adapter import TelegramAdapter


class _RecordingBot:
    def __init__(self):
        self.calls = []

    async def send_chat_action(self, **kwargs):
        self.calls.append(kwargs)


def _bare_adapter():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._bot = _RecordingBot()
    adapter._telegram_typing_cooldown_until = {}
    adapter._telegram_typing_cooldown_seconds = 30.0
    return adapter


def test_typing_cooldown_isolated_between_topics_in_same_chat():
    async def scenario():
        adapter = _bare_adapter()
        chat_id = "-1004309874643"
        topic_a = {"thread_id": "101"}
        topic_b = {"thread_id": "202"}

        adapter._record_typing_cooldown(
            chat_id,
            TimeoutError("temporary Telegram failure"),
            topic_a,
        )

        assert adapter._typing_in_cooldown(chat_id, topic_a) is True
        assert adapter._typing_in_cooldown(chat_id, topic_b) is False

        await adapter.send_typing(chat_id, topic_b)

        assert adapter._bot.calls == [
            {
                "chat_id": -1004309874643,
                "action": "typing",
                "message_thread_id": 202,
            }
        ]

    asyncio.run(scenario())
