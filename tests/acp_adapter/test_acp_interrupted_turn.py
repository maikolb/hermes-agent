"""An ACP turn that ends abnormally must release the session for the next prompt.

Regression: a ``session/cancel`` while a tool runs ends the turn with
``final_response: None``; ``prompt()`` called ``None.startswith`` before
clearing ``state.is_running``, so the session stayed "running" forever and
every later prompt answered "Queued for the next turn" without running.
"""

import asyncio
from types import SimpleNamespace

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self.runs = []
        self.on_first_run = None

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        if len(self.runs) == 1 and self.on_first_run is not None:
            return self.on_first_run(messages)
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []
        self.fail_next_agent_message = False

    async def session_update(self, session_id, update, **kwargs):
        if self.fail_next_agent_message and getattr(update, "session_update", "") == "agent_message_chunk":
            self.fail_next_agent_message = False
            raise RuntimeError("client went away mid-delivery")
        self.updates.append((session_id, update))

    async def request_permission(self, *args, **kwargs):
        return SimpleNamespace(outcome="allow")

    def texts(self):
        return [getattr(getattr(update, "content", None), "text", "") for _sid, update in self.updates]


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None

    def replace_messages(self, *_args, **_kwargs):
        return None


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


def _prompt(text):
    return [TextContentBlock(type="text", text=text)]


@pytest.mark.asyncio
async def test_cancelled_turn_with_none_final_response_releases_session():
    acp_agent, state, fake, conn = make_agent_and_state()
    loop = asyncio.get_running_loop()

    def cancelled_mid_tool(messages):
        # session/cancel lands on the event loop while the agent thread is
        # inside a tool; the interrupted envelope carries final_response=None.
        asyncio.run_coroutine_threadsafe(acp_agent.cancel(state.session_id), loop).result(timeout=5)
        return {"final_response": None, "messages": messages, "interrupted": True}

    fake.on_first_run = cancelled_mid_tool

    first = await acp_agent.prompt(session_id=state.session_id, prompt=_prompt("run the long command"))

    assert first.stop_reason == "cancelled"
    assert state.is_running is False

    second = await acp_agent.prompt(session_id=state.session_id, prompt=_prompt("try again"))

    assert second.stop_reason == "end_turn"
    assert len(fake.runs) == 2
    assert fake.runs[1].endswith("try again")
    assert not any(text.startswith("Queued for the next turn") for text in conn.texts())
    assert state.is_running is False


@pytest.mark.asyncio
async def test_post_turn_exception_does_not_leave_session_running():
    acp_agent, state, fake, conn = make_agent_and_state()
    conn.fail_next_agent_message = True

    with pytest.raises(RuntimeError, match="client went away"):
        await acp_agent.prompt(session_id=state.session_id, prompt=_prompt("first"))

    assert state.is_running is False
    assert state.current_prompt_text == ""

    second = await acp_agent.prompt(session_id=state.session_id, prompt=_prompt("second"))

    assert second.stop_reason == "end_turn"
    assert fake.runs == ["first", "second"]
    assert not any(text.startswith("Queued for the next turn") for text in conn.texts())
