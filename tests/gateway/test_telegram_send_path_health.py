"""TelegramAdapter send-path health gating after reconnect storms.

After sustained Bad Gateway / TimedOut reconnect cycles, the PTB httpx client
can enter a wedged state where ``bot.send_message()`` returns a valid Message
but nothing reaches the recipient.  ``_send_path_degraded`` short-circuits
``send()`` so cron's live-adapter branch falls through to standalone HTTP.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return adapter


def test_exact_text_capability_excludes_chunked_telegram_payloads():
    adapter = _make_adapter()

    assert adapter.can_deliver_exact_text("short exact response") is True
    assert adapter.can_deliver_exact_text("x" * 5000) is False


@pytest.mark.asyncio
async def test_disconnected_send_is_explicitly_pre_network():
    adapter = _make_adapter()
    adapter._bot = None

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.raw_response == {"send_attempted": False}


@pytest.mark.asyncio
async def test_exact_delivery_bypasses_unaudited_rich_fast_path():
    adapter = _make_adapter()
    adapter._should_attempt_rich = MagicMock(return_value=True)
    adapter._try_send_rich = AsyncMock()

    result = await adapter.send(
        "123",
        "| A | B |\n|---|---|\n| 1 | 2 |",
        metadata={"_hermes_exact_text_delivery": True},
    )

    assert result.success is True
    adapter._try_send_rich.assert_not_awaited()
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_short_circuits_when_path_degraded():
    """Degraded adapter returns failure WITHOUT calling send_message,
    so cron's live-adapter branch falls through to standalone HTTP."""
    adapter = _make_adapter()
    adapter._send_path_degraded = True

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "send_path_degraded"
    assert result.retryable is True
    assert result.raw_response == {"send_attempted": False}
    adapter._bot.send_message.assert_not_awaited()


class _FloodError(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


@pytest.mark.asyncio
async def test_send_long_flood_fails_closed_without_inline_sleep(monkeypatch):
    """A 97-minute RetryAfter must not pin send() for the full penalty."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(5827.0))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "flood_control:5827.0"
    assert result.retry_after == 5827.0
    assert result.retryable is False
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_short_flood_still_retries_inline(monkeypatch):
    """Waits of a few seconds keep the existing inline retry."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    ok = MagicMock(message_id=7)
    adapter._bot.send_message = AsyncMock(side_effect=[_FloodError(2.0), ok])
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is True
    assert result.message_id == "7"
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_exact_delivery_does_not_substitute_plain_text_on_parse_error():
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(
        side_effect=RuntimeError("Markdown parse error")
    )

    result = await adapter.send(
        "123",
        r"literal \\*markers\\* must survive",
        metadata={"_hermes_exact_text_delivery": True},
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_best_effort_delivery_retains_plain_text_parse_fallback():
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    ok = MagicMock(message_id=9)
    adapter._bot.send_message = AsyncMock(
        side_effect=[RuntimeError("Markdown parse error"), ok]
    )

    result = await adapter.send("123", "**bold**")

    assert result.success is True
    assert adapter._bot.send_message.await_count == 2
    assert adapter._bot.send_message.await_args_list[1].kwargs["parse_mode"] is None
