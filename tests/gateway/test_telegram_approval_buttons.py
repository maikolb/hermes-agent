"""Tests for Telegram inline keyboard approval buttons."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import Platform, PlatformConfig


def _make_adapter(extra=None):
    """Create a TelegramAdapter with mocked internals."""
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_operator_link_is_consumed_without_gateway_authorization_or_agent_turn(monkeypatch):
    adapter = _make_adapter({'support_approval_callback_url':'http://127.0.0.1:8790/api/suporte/telegram/callback'})
    msg = SimpleNamespace(text='/start balcao_'+'a'*43, chat=SimpleNamespace(type='private', id=1234),
                          from_user=SimpleNamespace(id=1234, username='operator', is_bot=False), reply_text=AsyncMock())
    monkeypatch.setattr(adapter, '_effective_update_message', lambda update: msg)
    monkeypatch.setattr(adapter, '_should_process_message', lambda *a, **k: True)
    authorization = MagicMock(return_value=False)
    monkeypatch.setattr(adapter, '_is_user_authorized_from_message', authorization)
    bridge = MagicMock(return_value={'decision':'linked'})
    monkeypatch.setattr(adapter, '_submit_support_approval_callback', bridge)
    adapter.handle_message = AsyncMock()
    await adapter._handle_command(SimpleNamespace(update_id=1), None)
    assert bridge.call_args[0][0] == {'action':'link','token':'a'*43,'chat_type':'private',
                                    'chat_id':'1234','telegram_user_id':'1234','username':'operator'}
    authorization.assert_not_called()
    adapter.handle_message.assert_not_called()
    assert 'conectado' in msg.reply_text.call_args[0][0]
    # A normal DM does not acquire gateway access by connecting an operator account.
    msg.text = '/start'
    await adapter._handle_command(SimpleNamespace(update_id=2), None)
    authorization.assert_called_once()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_operator_link_in_group_is_never_claimed_or_dispatched(monkeypatch):
    adapter = _make_adapter({'support_approval_callback_url':'http://127.0.0.1:8790/api/suporte/telegram/callback'})
    msg = SimpleNamespace(text='/start balcao_'+'a'*43, chat=SimpleNamespace(type='supergroup', id=-1234),
                          from_user=SimpleNamespace(id=1234, username='operator', is_bot=False), reply_text=AsyncMock())
    bridge = MagicMock()
    monkeypatch.setattr(adapter, '_submit_support_approval_callback', bridge)
    assert await adapter._handle_support_link(msg)
    bridge.assert_not_called()
    msg.reply_text.assert_not_called()


class _AuthRunner:
    """Minimal runner shim for callback auth tests."""

    def __init__(self, authorized: bool):
        self.authorized = authorized
        self.last_source = None

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        self.last_source = source
        return self.authorized


# ===========================================================================
# send_exec_approval — inline keyboard buttons
# ===========================================================================

class TestTelegramExecApproval:
    """Test the send_exec_approval method sends InlineKeyboard buttons."""

    @pytest.mark.asyncio
    async def test_sends_inline_keyboard(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="rm -rf /important",
            session_key="agent:main:telegram:group:12345:99",
            description="dangerous deletion",
        )

        assert result.success is True
        assert result.message_id == "42"

        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "rm -rf /important" in kwargs["text"]
        assert "dangerous deletion" in kwargs["text"]
        assert kwargs["reply_markup"] is not None  # InlineKeyboardMarkup


    @pytest.mark.asyncio
    async def test_non_smart_allow_permanent_false_keeps_session(self, monkeypatch):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        buttons = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: buttons.append(text) or text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
            allow_permanent=False,
        )

        assert buttons == ["✅ Allow Once", "✅ Session", "❌ Deny"]

    @pytest.mark.asyncio
    async def test_full_approval_keyboard_is_two_by_two(self, monkeypatch):
        """Regression: d48bf743f flattened all buttons into one row (4x1)."""
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        captured_rows = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: captured_rows.extend(rows) or rows,
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
        )

        assert captured_rows == [
            ["✅ Allow Once", "✅ Session"],
            ["✅ Always", "❌ Deny"],
        ]


    @pytest.mark.asyncio
    async def test_smart_deny_two_buttons_share_one_row(self, monkeypatch):
        """smart_deny yields 2 buttons — they pair into a single readable row."""
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        captured_rows = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: captured_rows.extend(rows) or rows,
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
            allow_permanent=False, smart_denied=True,
        )

        assert captured_rows == [
            ["✅ Allow Once", "❌ Deny"],
        ]


    @pytest.mark.asyncio
    async def test_send_update_prompt_escapes_dynamic_prompt(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=55)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_update_prompt(
            chat_id="12345",
            prompt="Fix [issue]_1 and verify *markdown*",
            default="alpha_beta",
            metadata={"thread_id": "999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "Fix \\[issue\\]\\_1" in sent["text"]
        assert "alpha\\_beta" in sent["text"]


class TestSupportApprovalButtons:
    @pytest.mark.asyncio
    async def test_sends_approve_reject_and_vigilia_in_project_topic(self, monkeypatch):
        adapter = _make_adapter()
        captured_rows = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data=None, url=None: {"text": text, "callback_data": callback_data, "url": url},
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: captured_rows.extend(rows) or rows,
        )
        adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=88))
        payload = {
            "ticket": "CA-0042", "project": "concursa", "project_name": "Concursa AI",
            "title": "Falha ao salvar", "reporter": "Ana",
            "vigilia_url": "https://vigilia.test/lux/#kanban/pilot/t_deadbeef",
        }

        result = await adapter.send_support_approval("-1004309874643", payload, metadata={"thread_id": "41"})

        assert result.success is True and result.message_id == "88"
        sent = adapter._send_message_with_thread_fallback.call_args.kwargs
        assert sent["chat_id"] == -1004309874643 and sent["message_thread_id"] == 41
        assert "CA-0042" in sent["text"] and payload["vigilia_url"] in sent["text"]
        assert captured_rows == [
            [
                {"text": "✅ Aprovar", "callback_data": "sa:a:concursa:CA-0042", "url": None},
                {"text": "❌ Reprovar", "callback_data": "sa:r:concursa:CA-0042", "url": None},
            ],
            [{"text": "🔎 Vigília", "callback_data": None, "url": payload["vigilia_url"]}],
        ]

    @pytest.mark.asyncio
    async def test_real_callback_identity_is_forwarded_and_keyboard_is_closed(self, monkeypatch):
        adapter = _make_adapter({"support_approval_callback_url": "http://127.0.0.1:8790/api/suporte/telegram/callback"})
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *args, **kwargs: True)
        submit = MagicMock(return_value={
            "decision": "approved", "actor": "Jhonatan", "ticket": "CA-0042",
            "vigilia_url": "https://vigilia.test/lux/#kanban/pilot/t_deadbeef",
        })
        monkeypatch.setattr(adapter, "_submit_support_approval_callback", submit)
        query = AsyncMock()
        query.data = "sa:a:concursa:CA-0042"
        query.message = MagicMock(chat_id=-1004309874643, message_thread_id=41, message_id=88)
        query.message.chat.type = "supergroup"
        query.from_user = MagicMock(id=7550030839, first_name="Jhonatan")
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock(callback_query=query)

        await adapter._handle_callback_query(update, MagicMock())

        submit.assert_called_once_with({
            "action": "approve", "project": "concursa", "ticket": "CA-0042",
            "telegram_user_id": "7550030839", "chat_id": "-1004309874643",
            "thread_id": "41", "message_id": "88",
        })
        assert query.edit_message_text.call_args.kwargs["reply_markup"] is None
        assert "Jhonatan" in query.edit_message_text.call_args.kwargs["text"]

# _handle_callback_query — approval button clicks
# ===========================================================================

class TestTelegramApprovalCallback:
    """Test the approval callback handling in _handle_callback_query."""


    @pytest.mark.asyncio
    async def test_resume_typing_after_inline_approval(self):
        """Clicking an inline approval button must un-pause the chat's typing.

        Regression for #27853: the text /approve path resumed typing, but the
        ea: callback path did not, so the typing indicator stayed gone for the
        rest of a long-running turn after a button click.
        """
        adapter = _make_adapter()
        adapter._approval_state[5] = "agent:main:telegram:group:12345:99"
        adapter.pause_typing_for_chat("12345")
        assert "12345" in adapter._typing_paused

        query = AsyncMock()
        query.data = "ea:once:5"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.from_user.id = "12345"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        assert "12345" not in adapter._typing_paused


    @pytest.mark.asyncio
    async def test_approval_callback_escapes_dynamic_user_name(self):
        adapter = _make_adapter()
        adapter._approval_state[3] = "agent:main:telegram:group:12345:99"

        query = AsyncMock()
        query.data = "ea:once:3"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Alice_Bob"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "Alice\\_Bob" in edit_kwargs["text"]
        assert "Approved once" in edit_kwargs["text"]


    @pytest.mark.asyncio
    async def test_update_prompt_callback_not_affected(self, tmp_path):
        """Ensure update prompt callbacks still work."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 123
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
                # Allow the caller — the new fail-closed allowlist gate
                # (#24457) rejects empty TELEGRAM_ALLOWED_USERS, but this
                # test isn't exercising that gate; it's verifying the
                # update_prompt callback still writes the response.
                with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
                    await adapter._handle_callback_query(update, context)

        # Should NOT have triggered approval resolution
        mock_resolve.assert_not_called()
        assert (tmp_path / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_unauthorized_user(self, tmp_path):
        """Update prompt buttons should honor TELEGRAM_ALLOWED_USERS."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_user_blocked_by_global_allowlist(self, tmp_path):
        adapter = _make_adapter()
        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()
        assert runner.last_source is not None
        assert runner.last_source.platform == Platform.TELEGRAM
        assert runner.last_source.user_id == "222"
