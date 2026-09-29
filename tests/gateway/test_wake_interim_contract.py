"""Wake publication rules must cover previews, not only the last answer."""
import time

import pytest

from tests.gateway.test_run_progress_topics import _run_with_agent

CHECK = "Validei os arquivos de decisão: 27 testes de formato JSON passaram, exit 0."
OUTCOME = "Pronto: handoff revisado entregue."


class WakePreviewAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback(CHECK, already_streamed=False)
            self.interim_assistant_callback(OUTCOME, already_streamed=False)
        if self.stream_delta_callback:
            self.stream_delta_callback(CHECK)
        time.sleep(0.12)
        return {"final_response": OUTCOME, "messages": [], "api_calls": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("wake", [False, True])
async def test_wake_checks_do_not_escape_through_interim_or_stream(
    monkeypatch, tmp_path, streaming, wake,
):
    adapter, result = await _run_with_agent(
        monkeypatch, tmp_path, WakePreviewAgent,
        session_id="wake-preview-contract",
        message="[kanban] Task example worker aguardando sua decisão." if wake else "Mostre os checks.",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": True},
            "streaming": {"enabled": streaming, "edit_interval": 0.01, "buffer_threshold": 1},
        },
    )
    sent = [x["content"] for x in adapter.sent + adapter.edits]
    assert any(OUTCOME in x for x in sent), sent
    assert any(CHECK in x for x in sent) is not wake, sent
    assert result["final_response"] == OUTCOME
