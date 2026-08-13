from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter():
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))


def test_group_forum_topic_created_exposes_topic_name_and_service_metadata():
    adapter = _adapter()
    message = SimpleNamespace(
        text=None,
        caption=None,
        chat=SimpleNamespace(
            id=-100123,
            type="supergroup",
            is_forum=True,
            title="Projects",
        ),
        from_user=SimpleNamespace(
            id=456,
            full_name="Alice",
            is_bot=False,
        ),
        message_thread_id=77,
        is_topic_message=True,
        forum_topic_created=SimpleNamespace(name="Mulher +Segura"),
        reply_to_message=None,
        message_id=10,
        date=None,
    )

    event = adapter._build_message_event(message, MessageType.TEXT)

    assert event.source.chat_topic == "Mulher +Segura"
    assert event.source.thread_id == "77"
    assert event.metadata == {"telegram_forum_topic_created": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["123", "-100123"])
async def test_ensure_forum_topic_accepts_dm_or_supergroup(chat_id):
    adapter = _adapter()
    adapter._bot = SimpleNamespace(
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=88))
    )

    result = await adapter.ensure_forum_topic(chat_id, "Alpha")

    assert result == "88"
    adapter._bot.create_forum_topic.assert_awaited_once_with(
        chat_id=int(chat_id),
        name="Alpha",
    )
