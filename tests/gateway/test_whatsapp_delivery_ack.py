import asyncio
from unittest.mock import AsyncMock

from gateway.config import Platform
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def post(self, *_args, **_kwargs):
        return _ResponseContext(_Response(self.payload))


def _adapter(payload):
    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._running = True
    adapter._http_session = _Session(payload)
    adapter._bridge_port = 9876
    adapter._bridge_process = None
    adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
    adapter.format_message = lambda value: value
    adapter.truncate_message = lambda value, _limit: [value]
    adapter._outgoing_chunk_limit = lambda: 4096
    return adapter


def test_server_ack_is_required_for_success():
    result = asyncio.run(_adapter({
        "success": True,
        "messageId": "message-1",
        "messageIds": ["message-1"],
        "ackStatuses": [2],
    }).send("123@lid", "response"))
    assert result.success is True
    assert result.message_id == "message-1"


def test_http_200_without_ack_is_not_delivered():
    result = asyncio.run(_adapter({
        "success": True,
        "messageId": "message-2",
        "messageIds": ["message-2"],
    }).send("123@lid", "response"))
    assert result.success is False
    assert "acknowledgement" in (result.error or "").lower()


def test_pending_status_is_not_delivered():
    result = asyncio.run(_adapter({
        "success": True,
        "messageId": "message-3",
        "messageIds": ["message-3"],
        "ackStatuses": [1],
    }).send("123@lid", "response"))
    assert result.success is False
    assert "acknowledgement" in (result.error or "").lower()
