"""Productive gateway call-site coverage for Kanban worker rotation."""

import sys
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


class _HeartbeatAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="test-token"),
            Platform.TELEGRAM,
        )
        self.sent = []
        self.edited = []
        self._message_id = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        self._message_id += 1
        message_id = str(self._message_id)
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "metadata": metadata,
                "message_id": message_id,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": str(message_id),
                "content": content,
            }
        )
        return SendResult(success=True, message_id=str(message_id))

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class _SlowAgent:
    def __init__(self, **_kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 1}


def _install_agent_fakes(monkeypatch, tmp_path, config):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _SlowAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")


def _bare_agent_runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._active_profile_name = lambda: "default"
    return runner


@pytest.mark.asyncio
async def test_productive_heartbeat_sends_principal_plus_scoped_workers(
    monkeypatch, tmp_path,
):
    config = {
        "display": {
            "worker_rotation": True,
            "tool_progress": "off",
            "platforms": {
                "telegram": {
                    "activity_indicator": {
                        "initial_delay_seconds": 0,
                        "update_interval_seconds": 0.05,
                        "initial_text": "⏳ Trabalhando…",
                        "elapsed_text": "⏳ Trabalhando há {elapsed_human}…",
                    }
                }
            },
        }
    }
    _install_agent_fakes(monkeypatch, tmp_path, config)
    adapter = _HeartbeatAdapter()
    runner = _bare_agent_runner(adapter)

    def row(task_id, session_id):
        return {
            "task": SimpleNamespace(
                id=task_id,
                project_id="",
                session_id=session_id,
            )
        }

    runner._kanban_worker_focus_active = {
        ("project-factory", "telegram", "-1001", "", "default"): {
            "t_first": row("t_first", "principal-session"),
            "t_second": row("t_second", "principal-session"),
            "t_other": row("t_other", "other-session"),
        }
    }
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
    )

    result = await runner._run_agent(
        message="work",
        context_prompt="",
        history=[],
        source=source,
        session_id="principal-session",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done"
    heartbeat_texts = [item["content"] for item in adapter.sent]
    heartbeat_texts.extend(item["content"] for item in adapter.edited)
    assert "⏳ Trabalhando… · principal + 2 workers" in heartbeat_texts


def _handler_runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._resolve_project_context_for_message = lambda *_args: (None, None)
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:main-user",
        session_id="principal-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda *_args, **_kwargs: {"display": {"worker_rotation": True}},
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


@pytest.mark.asyncio
async def test_inbound_message_productive_path_reclaims_worker_display(
    monkeypatch, tmp_path,
):
    runner = _handler_runner(monkeypatch, tmp_path)
    runner._kanban_claim_worker_display = AsyncMock()
    runner._run_agent = AsyncMock(
        return_value={
            "failed": True,
            "final_response": None,
            "error": "simulated early failure",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="main-user",
    )
    event = MessageEvent(text="new principal work", source=source, message_id="m-1")

    await runner._handle_message_with_agent(
        event,
        source,
        "routing-key-before-session-recovery",
        7,
    )

    runner._kanban_claim_worker_display.assert_awaited_once_with(
        source,
        board="",
        project_id="",
        session_id="principal-session",
        principal_session_key="routing-key-before-session-recovery",
        run_generation=7,
    )
