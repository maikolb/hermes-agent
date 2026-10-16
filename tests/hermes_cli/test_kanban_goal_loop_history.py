"""COMPRESSION_LOOP_20261016 (caso 1): o goal loop do worker -Q tem de levar cada turno ao seguinte.

Run 1700 (t_1f500f9f, sessao 20261014_013150_70efd6), 16/10: nove turnos no mesmo processo, todos com
``conversation turn ... history=456`` no agent.log. ``_run_turn`` passava ``cli.conversation_history`` a
``run_conversation`` e nunca guardava ``result["messages"]``; a lista era a restaurada do state.db no inicio do
processo. Cada turno partia do transcript velho, sem o que o turno anterior fez nem a compressao que ele
gravou, e com 540 mil tokens sobre o limiar de 314.572 cada turno comprimiu de novo (8 resumos repetidos no
Codex, 457 -> 13 mensagens em todos).

O caminho interativo (``HermesCLI.chat``) ja guarda ``result["messages"]`` em ``conversation_history``; o goal loop
do -Q tem de fazer o mesmo, a partir do primeiro turno (``first_result``).
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return home


def _running_card(conn):
    rid = delivery.receive_request(
        conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": "17"},
        text="Corrigir disciplinas vinculadas ao cargo Delegado de Policia Civil.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[],
    )
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def test_each_goal_loop_turn_starts_from_the_previous_turn_transcript(board, monkeypatch):
    import cli as cli_mod

    with kb.connect_closing() as conn:
        task = _running_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "not done yet", False, None, False))
    # Two more turns, then the worker has closed the card.
    statuses = ["running", "running", "done"]
    monkeypatch.setattr(kb, "goal_run_status", lambda conn, task_id, run_id=None: statuses.pop(0))

    restored = [  # what --resume loaded from state.db when the process started
        {"role": "user", "content": "work kanban task (earlier run)"},
        {"role": "assistant", "content": "earlier work"},
    ]
    # The first turn compacted the session: its result carries the compacted transcript plus the turn.
    first_messages = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "work kanban task"},
        {"role": "assistant", "content": "turn 1 done"},
    ]
    seen = []

    def run_conversation(user_message, conversation_history=None, **_):
        history = list(conversation_history or [])
        seen.append(history)
        messages = history + [{"role": "user", "content": user_message},
                              {"role": "assistant", "content": f"turn {len(seen) + 1} done"}]
        return {"final_response": f"turn {len(seen) + 1} done", "messages": messages}

    worker = SimpleNamespace(agent=SimpleNamespace(session_id="worker", run_conversation=run_conversation),
                             conversation_history=list(restored), session_id="worker")
    first_result = {"final_response": "turn 1 done", "messages": first_messages}
    cli_mod._run_kanban_goal_loop_q(worker, "turn 1 done", first_result)

    assert len(seen) == 2
    # Turn 2 starts from what turn 1 left (compacted), never from the restored snapshot.
    assert seen[0] == first_messages
    # Turn 3 starts from what turn 2 left.
    assert seen[1][:len(first_messages)] == first_messages
    assert len(seen[1]) == len(first_messages) + 2
    assert worker.conversation_history[-1]["content"] == "turn 3 done"


def _real_agent(monkeypatch, tmp_path):
    """Real AIAgent and SessionDB, provider stubbed (harness of tests/run_agent/test_preflight_compression_cap_e2e.py)."""
    import contextlib
    import io
    from unittest.mock import MagicMock, patch

    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        contextlib.redirect_stdout(io.StringIO()),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(base_url="https://openrouter.ai/api/v1", api_key="test-key", model="test/model",
                        enabled_toolsets=[], disabled_toolsets=[], quiet_mode=True, skip_memory=True,
                        skip_context_files=True, session_db=db, session_id="goal-loop-history")
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    msg = SimpleNamespace(content="worked on it", reasoning_content=None, reasoning=None, tool_calls=None)
    agent.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model", usage=None)
    return agent


def test_a_compacted_session_is_not_compacted_again_on_the_next_goal_loop_turn(board, monkeypatch, tmp_path):
    """The loop of run 1700 with a real agent: a restored session over the threshold compacts on turn 1 and the
    following goal-loop turns start from the compacted transcript, so the summary is requested once, not per turn."""
    from unittest.mock import patch

    import cli as cli_mod

    with kb.connect_closing() as conn:
        task = _running_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "not done yet", False, None, False))
    statuses = ["running", "running", "done"]
    monkeypatch.setattr(kb, "goal_run_status", lambda conn, task_id, run_id=None: statuses.pop(0))

    agent = _real_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 100_000
    restored = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"restored {i}"} for i in range(60)]
    compress_inputs = []

    def fake_compress(messages, system_message, **_kwargs):
        compress_inputs.append(len(messages))
        return [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}] + list(messages[-2:]), "compressed prompt"

    # Big while the restored transcript is in the request, small once it was compacted.
    estimate = lambda messages, **_k: 500_000 if len(messages) > 30 else 1_000
    turn_histories = []
    real_run = agent.run_conversation

    def recording_run(user_message, conversation_history=None, **kwargs):
        turn_histories.append(len(conversation_history or []))
        return real_run(user_message, conversation_history=conversation_history, **kwargs)

    worker = SimpleNamespace(agent=agent, conversation_history=list(restored), session_id=agent.session_id)
    with (
        patch("agent.turn_context.estimate_request_tokens_rough", side_effect=estimate),
        patch.object(agent, "_compress_context", side_effect=fake_compress),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "run_conversation", side_effect=recording_run),
    ):
        first = agent.run_conversation("work kanban task", conversation_history=worker.conversation_history)
        cli_mod._run_kanban_goal_loop_q(worker, first.get("final_response", ""), first)

    assert turn_histories[0] == 60
    # Before the fix: [60, 60, 60] and three summaries (one per turn), the 457 -> 13 loop of run 1700.
    assert turn_histories[1] == len(first["messages"]) < 30, turn_histories
    assert turn_histories[2] > turn_histories[1], turn_histories
    assert len(compress_inputs) == 1, compress_inputs
