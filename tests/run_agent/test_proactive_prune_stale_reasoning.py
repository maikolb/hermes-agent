"""NFOS_WORKER_CTX_20261016: the proactive prune also pads stale thinking text.

Measured on the NFOS workers (deepseek/deepseek-v4.1-flash through OpenRouter): one
card run is ONE long turn, and DeepSeek forces ``reasoning_content`` echo-back, so
the thinking text of every assistant message rode on the wire for the whole run
(about 40% of the live window; input per call = 0.91 x output of the previous
call + the tool results). The tool-result prune never touched it and full
compression only fires at 0.85 x 1M.

These tests drive ``run_conversation()`` with a REAL ``ContextCompressor`` and a REAL
``SessionDB``: the assertions read the payload the loop hands the provider and the
rows the prune persisted, not a helper called in isolation.

Round 2 (review of PR 353) added: the newest assistant message is never padded (parallel
fan-out), the no-echo guard is judged by the stored rows, signed thinking envelopes are
not half-stripped, and a failing provider read fails closed.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB
from run_agent import AIAgent

PAD = " "
N_TOOL_ITERATIONS = 12
PROTECT_LAST_N = 6
SESSION_ID = "worker-ctx-test"
SIGNED = [{"type": "reasoning.text", "text": "t", "signature": "sig-abc", "index": 0}]
PLAIN = [{"type": "reasoning.text", "text": "t", "index": 0}]


def _think(i: int) -> str:
    # ~1.2k chars of distinct thinking per assistant message
    return f"THINK-{i:02d} " + ("raciocinio " * 110)


def _tool_response(i: int, n_calls: int = 1, details=None):
    calls = [
        SimpleNamespace(
            id=f"call_{i}" if n_calls == 1 else f"call_{i}_{k}",
            type="function",
            function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": f"q{i}_{k}"})),
        )
        for k in range(n_calls)
    ]
    msg = SimpleNamespace(
        content=None,
        reasoning_content=_think(i),
        reasoning=_think(i),
        reasoning_details=details,
        tool_calls=calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")], model="test/model", usage=None
    )


def _stop_response():
    msg = SimpleNamespace(content="done", reasoning_content=_think(99), reasoning=_think(99), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model", usage=None)


def _tool_defs():
    return [
        {
            "type": "function",
            "function": {"name": "web_search", "description": "x", "parameters": {"type": "object", "properties": {}}},
        }
    ]


def _build_agent(tmp_path, *, provider: str, model: str, base_url: str, stale_reasoning: bool = True):
    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="dummy",
            base_url=base_url,
            provider=provider,
            model=model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=N_TOOL_ITERATIONS + 4,
            session_db=db,
            session_id=SESSION_ID,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True
    with patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        agent.context_compressor = ContextCompressor(
            model=model,
            provider=provider,
            base_url=base_url,
            threshold_percent=0.85,  # 850k on a 1M window: full compression never fires here
            protect_first_n=2,
            protect_last_n=PROTECT_LAST_N,
            quiet_mode=True,
            proactive_prune_tokens=1_000,
            proactive_prune_min_reclaim_tokens=500,
            proactive_prune_stale_reasoning=stale_reasoning,
        )
    # agent_init binds the session store to the engine it builds; this test swaps the engine.
    agent.context_compressor.bind_session_state(db, SESSION_ID)
    return agent, db


def _run(agent, responses=None):
    if responses is None:
        responses = [_tool_response(i) for i in range(N_TOOL_ITERATIONS)] + [_stop_response()]
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.title_generator.maybe_auto_title"),  # no network from the test
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        return agent.run_conversation("do a lot of tool work")


def _last_payload(agent):
    return agent.client.chat.completions.create.call_args_list[-1].kwargs["messages"]


def _wire_reasoning(agent):
    """reasoning_content of the assistant messages in the LAST payload sent to the provider, oldest first."""
    return [m.get("reasoning_content", "") for m in _last_payload(agent) if m.get("role") == "assistant"]


def _worker(tmp_path, **kw):
    return _build_agent(
        tmp_path,
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        base_url="https://openrouter.ai/api/v1",
        **kw,
    )


def _details(row):
    raw = row.get("reasoning_details")
    return json.loads(raw) if isinstance(raw, str) else raw


def _active_assistants(db):
    return [r for r in db.get_messages(SESSION_ID) if r.get("role") == "assistant"]


class TestStaleReasoningPadThroughTheRealLoop:
    def test_old_thinking_leaves_the_wire_and_the_tail_keeps_it(self, tmp_path):
        agent, _db = _worker(tmp_path)
        assert agent._needs_thinking_reasoning_pad() is True  # the NFOS worker route

        result = _run(agent)
        assert result["completed"] is True

        rc = _wire_reasoning(agent)
        # Every assistant message still carries reasoning_content (DeepSeek answers 400 without it) ...
        assert all(isinstance(x, str) and x != "" for x in rc)
        # ... the oldest ones were swapped for the one-space pad ...
        assert len([x for x in rc if x == PAD]) >= 4, [x[:8] for x in rc]
        # ... padded ones come first (oldest), real text after ...
        first_real = next(i for i, x in enumerate(rc) if x != PAD)
        assert all(x == PAD for x in rc[:first_real])
        assert all(x.startswith("THINK-") for x in rc[first_real:])
        # ... and the protected tail keeps the real text, newest included.
        assert rc[-1].startswith("THINK-") and len(rc[first_real:]) >= 2
        # The wire carries far less thinking text than the run produced.
        sent = sum(len(x) for x in rc)
        produced = sum(len(_think(i)) for i in range(N_TOOL_ITERATIONS))
        assert sent < 0.6 * produced

    def test_effects_survive_the_pad(self, tmp_path):
        """Class of what is cut: thinking text only. Tool calls and results stay."""
        agent, _db = _worker(tmp_path)
        _run(agent)
        wire = _last_payload(agent)
        calls = [tc["id"] for m in wire if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
        results = [m["tool_call_id"] for m in wire if m.get("role") == "tool"]
        assert calls == [f"call_{i}" for i in range(N_TOOL_ITERATIONS)]
        assert results == calls

    def test_padded_rows_are_persisted_and_the_originals_stay_archived(self, tmp_path):
        agent, db = _worker(tmp_path)
        _run(agent)
        assert [r for r in _active_assistants(db) if (r.get("reasoning_content") or "") == PAD], (
            "a resumed session would bring the old thinking back"
        )
        everything = db.get_messages(SESSION_ID, include_inactive=True)
        assert [r for r in everything if (r.get("reasoning_content") or "").startswith("THINK-")], (
            "the pre-prune rows must stay recoverable for audit"
        )

    def test_provider_without_echo_keeps_everything_stored(self, tmp_path):
        """Judged by what stays WRITTEN: the wire policy already drops reasoning_content for
        providers without echo, so only the stored rows prove the guard."""
        agent, db = _build_agent(
            tmp_path,
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1",
        )
        assert agent._needs_thinking_reasoning_pad() is False
        responses = [_tool_response(i, details=PLAIN) for i in range(N_TOOL_ITERATIONS)] + [_stop_response()]
        _run(agent, responses)
        rows = _active_assistants(db)
        assert len(rows) == N_TOOL_ITERATIONS + 1
        for r in rows[:-1]:
            assert (r.get("reasoning_content") or "").startswith("THINK-"), r.get("reasoning_content")
            assert (r.get("reasoning") or "").startswith("THINK-")
            assert _details(r) == PLAIN
        # no pad commit archived anything
        assert len(db.get_messages(SESSION_ID, include_inactive=True)) == len(db.get_messages(SESSION_ID))

    def test_parallel_fan_out_keeps_the_newest_assistant_thinking(self, tmp_path):
        """20 parallel tool calls put 20 tool results after their assistant message, so the
        message-counted tail no longer contains it. It is still the newest assistant message."""
        agent, db = _worker(tmp_path)
        responses = [_tool_response(i) for i in range(6)] + [_tool_response(50, n_calls=20)] + [_stop_response()]
        _run(agent, responses)
        asst = [m for m in _last_payload(agent) if m.get("role") == "assistant"]
        fan_out = asst[-1]
        assert len(fan_out["tool_calls"]) == 20
        assert fan_out["reasoning_content"].startswith("THINK-50")
        assert PAD in [m.get("reasoning_content") for m in asst[:-1]]  # older ones were still padded
        stored = [r for r in _active_assistants(db) if (r.get("reasoning_content") or "").startswith("THINK-50")]
        assert stored, "the fan-out assistant row must keep its thinking in the store too"

    def test_signed_thinking_envelope_is_not_half_stripped(self, tmp_path):
        agent, db = _worker(tmp_path)
        responses = [
            _tool_response(i, details=SIGNED if i == 1 else PLAIN if i == 2 else None) for i in range(N_TOOL_ITERATIONS)
        ] + [_stop_response()]
        _run(agent, responses)
        rows = _active_assistants(db)
        signed_row = next(r for r in rows if (r.get("reasoning_content") or "").startswith("THINK-01"))
        assert _details(signed_row) == SIGNED  # envelope intact, text intact
        plain_rows = [r for r in rows if r.get("reasoning_content") == PAD]
        assert plain_rows, "unsigned history should still have been padded"
        assert all(not r.get("reasoning_details") for r in plain_rows)

    def test_flag_read_failure_fails_closed(self, tmp_path):
        agent, db = _worker(tmp_path)
        agent.context_compressor.stale_reasoning_pad = True  # stale value from an earlier iteration
        real = agent._needs_thinking_reasoning_pad

        def flaky():
            if sys._getframe(1).f_code.co_name == "run_conversation":
                raise RuntimeError("provider unreadable")
            return real()

        agent._needs_thinking_reasoning_pad = flaky
        _run(agent)
        assert agent.context_compressor.stale_reasoning_pad is False
        assert PAD not in _wire_reasoning(agent)
        assert all((r.get("reasoning_content") or "").startswith("THINK-") for r in _active_assistants(db))

    def test_switch_off_keeps_the_old_behaviour(self, tmp_path):
        agent, _db = _worker(tmp_path, stale_reasoning=False)
        _run(agent)
        assert all(x.startswith("THINK-") for x in _wire_reasoning(agent))


class TestPadUnit:
    def test_pad_ignores_non_assistant_and_already_padded(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "reasoning_content": PAD, "tool_calls": []},
            {"role": "assistant", "content": "", "reasoning_content": "texto", "reasoning": "texto"},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
            {"role": "assistant", "content": "", "reasoning_content": "cauda"},
        ]
        n = ContextCompressor._pad_stale_reasoning(msgs, protect_tail_count=1)
        assert n == 1
        assert msgs[2]["reasoning_content"] == PAD and "reasoning" not in msgs[2]
        assert msgs[4]["reasoning_content"] == "cauda"

    def test_newest_assistant_is_never_padded_even_outside_the_tail(self):
        msgs = [{"role": "assistant", "content": "", "reasoning_content": "velho"}]
        msgs.append({"role": "assistant", "content": "", "reasoning_content": "fanout", "tool_calls": [{}] * 20})
        msgs += [{"role": "tool", "tool_call_id": str(k), "content": "ok"} for k in range(20)]
        n = ContextCompressor._pad_stale_reasoning(msgs, protect_tail_count=20)
        assert n == 1 and msgs[0]["reasoning_content"] == PAD
        assert msgs[1]["reasoning_content"] == "fanout"

    def test_only_plain_thinking_text_details_are_dropped(self):
        cases = [
            ("texto", True),
            ([{"type": "reasoning.text", "text": "x"}], True),
            ([{"type": "reasoning.text", "text": "x", "signature": "s"}], False),
            ([{"type": "reasoning.encrypted", "data": "blob"}], False),
            ([{"type": "reasoning.summary", "summary": "x"}], False),
        ]
        for details, dropped in cases:
            msgs = [
                {"role": "assistant", "content": "", "reasoning_content": "a", "reasoning_details": details},
                {"role": "assistant", "content": "", "reasoning_content": "fim"},  # newest: never padded
            ]
            n = ContextCompressor._pad_stale_reasoning(msgs, protect_tail_count=0)
            assert n == (1 if dropped else 0), (details, n)
            assert (msgs[0].get("reasoning_details") is None) is dropped, details
            assert msgs[0]["reasoning_content"] == (PAD if dropped else "a")

    def test_short_history_pads_nothing(self):
        msgs = [{"role": "assistant", "content": "", "reasoning_content": "x"}]
        assert ContextCompressor._pad_stale_reasoning(msgs, protect_tail_count=20) == 0
        assert msgs[0]["reasoning_content"] == "x"
