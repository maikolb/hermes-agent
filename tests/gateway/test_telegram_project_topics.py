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


@pytest.mark.parametrize(
    ("service_attr", "metadata_key"),
    [
        ("forum_topic_closed", "telegram_forum_topic_closed"),
        ("forum_topic_reopened", "telegram_forum_topic_reopened"),
    ],
)
def test_group_forum_topic_lifecycle_exposes_service_metadata(
    service_attr,
    metadata_key,
):
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
        forum_topic_created=None,
        forum_topic_closed=None,
        forum_topic_reopened=None,
        reply_to_message=None,
        message_id=11,
        date=None,
    )
    setattr(message, service_attr, SimpleNamespace())

    event = adapter._build_message_event(message, MessageType.TEXT)

    assert event.source.thread_id == "77"
    assert event.metadata == {metadata_key: True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_attr",
    ["forum_topic_closed", "forum_topic_reopened"],
)
async def test_forum_topic_lifecycle_handler_forwards_service_event(service_attr):
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
        from_user=SimpleNamespace(id=456, full_name="Alice", is_bot=False),
        message_thread_id=77,
        is_topic_message=True,
        forum_topic_created=None,
        forum_topic_closed=None,
        forum_topic_reopened=None,
        reply_to_message=None,
        message_id=12,
        date=None,
    )
    setattr(message, service_attr, SimpleNamespace())
    update = SimpleNamespace(message=message, effective_message=message, update_id=99)
    adapter.handle_message = AsyncMock()

    await adapter._handle_forum_topic_lifecycle(update, None)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.thread_id == "77"
    assert event.metadata == {
        f"telegram_{service_attr}": True,
    }


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
