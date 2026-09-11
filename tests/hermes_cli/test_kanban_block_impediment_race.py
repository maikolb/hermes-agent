"""IMPEDIMENT_RACE_20260911: a question pending with the Principal means the run waits or ends as
``awaiting_decision``; it is never a protocol violation and never a second impediment.

Case: t_b443ec2f on 10/09 (19:28 to 19:43Z) asked the same question 13 times across 5 runs. kanban_block
on an NFOS card files an impediment and returns True; the worker believed the card was blocked and exited;
the goal-mode judge nudged it (another kanban_block, another impediment) and then blocked (another one);
the process left rc=0; the dispatcher counted a protocol violation and relaunched; the new run repeated it.
"""

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

QUESTION = "PERGUNTA para Maikol: pode enviar o link ou print do cargo Delegado em que aparecem disciplinas erradas?"
PAUSE = "Capacidade: o runtime recusou o fechamento do relatório com NOT_RUN; como fechar?"


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
        text="Corrigir disciplinas vinculadas ao cargo Delegado de Polícia Civil.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[],
    )
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def _decisions(conn, task_id):
    return conn.execute(
        "SELECT id, status, kind FROM nfos_decisions WHERE task_id=? ORDER BY rowid", (task_id,)
    ).fetchall()


def _last_event(conn, task_id, kind=None):
    if kind:
        row = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (task_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    return (row[0], json.loads(row[1] or "{}")) if row else (None, {})


def _worker_gone_cleanly(monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)


# ----------------------------------------------------------------------------- block_task


def test_second_block_in_the_same_run_reuses_the_pending_impediment(board):
    with kb.connect_closing() as conn:
        task = _running_card(conn)
        run_id = task.current_run_id
        assert kb.block_task(conn, task.id, reason=PAUSE, kind="capability", expected_run_id=run_id) is True
        first = kb._nfos_pending_decision(conn, task.id, run_id)
        assert first
        # Judge nudge or rephrased reason: same run, same open question.
        assert kb.block_task(conn, task.id, reason=QUESTION, kind="needs_input", expected_run_id=run_id) is True
        rows = _decisions(conn, task.id)
        assert [(r["status"], r["kind"]) for r in rows] == [("pending", "impediment")]
        assert kb.get_task(conn, task.id).status == "running"
        kind, payload = _last_event(conn, task.id)
        assert kind == "nfos_impediment_repeated"
        assert payload["decision_id"] == first
        assert payload["requested_block_kind"] == "needs_input"


# ------------------------------------------------------------------ detect_crashed_workers


def test_clean_exit_with_a_pending_question_is_held_not_a_violation(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _running_card(conn)
        run_id = task.current_run_id
        kb.block_task(conn, task.id, reason=QUESTION, kind="needs_input", expected_run_id=run_id)
        decision = kb._nfos_pending_decision(conn, task.id, run_id)
        _worker_gone_cleanly(monkeypatch)

        crashed = kb.detect_crashed_workers(conn)

        assert crashed == []
        assert kb.detect_crashed_workers._last_awaiting_decision == [task.id]
        current = kb.get_task(conn, task.id)
        assert current.status == "ready"
        assert current.consecutive_failures == 0
        assert "protocol violation" not in (current.last_failure_error or "")
        run = conn.execute("SELECT outcome, status, metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["outcome"] == "awaiting_decision"
        assert json.loads(run["metadata"])["decision_id"] == decision
        assert kb._protocol_violation_streak(conn, task.id) == 0
        kind, payload = _last_event(conn, task.id, "awaiting_decision")
        assert kind == "awaiting_decision" and payload["decision_status"] == "pending"
        # The card stays held while the question is open, and is free once answered.
        assert kb._nfos_decision_open(conn, task.id) is True
        delivery.resolve_decision(
            conn, decision, action="continue", answer="CONTINUE: use o print anexado ao card.", author="Principal"
        )
        assert kb._nfos_decision_open(conn, task.id) is False
        assert kb.get_task(conn, task.id).status == "ready"


def test_clean_exit_after_a_human_answer_blocks_with_the_question(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _running_card(conn)
        run_id = task.current_run_id
        kb.block_task(conn, task.id, reason=QUESTION, kind="needs_input", expected_run_id=run_id)
        decision = kb._nfos_pending_decision(conn, task.id, run_id)
        delivery.resolve_decision(conn, decision, action="human", answer=QUESTION, author="Principal")
        if kb.get_task(conn, task.id).status == "running":
            # The worker was alive at answer time and then left without calling kanban_block.
            _worker_gone_cleanly(monkeypatch)
            assert kb.detect_crashed_workers(conn) == []
        current = kb.get_task(conn, task.id)
        assert current.status == "blocked"
        assert current.block_kind == "needs_input"
        assert current.consecutive_failures == 0
        kind, payload = _last_event(conn, task.id, "blocked")
        assert kind == "blocked" and "Maikol" in payload["reason"]


def test_clean_exit_without_a_question_is_still_a_protocol_violation(board, monkeypatch):
    with kb.connect_closing() as conn:
        task = _running_card(conn)
        run_id = task.current_run_id
        _worker_gone_cleanly(monkeypatch)

        crashed = kb.detect_crashed_workers(conn)

        assert crashed == [task.id]
        assert kb.detect_crashed_workers._last_awaiting_decision == []
        run = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["outcome"] == "crashed"
        assert json.loads(run["metadata"]).get("protocol_violation") is True
        assert kb._protocol_violation_streak(conn, task.id) == 1


# ------------------------------------------------------------------------ goal loop


def _no_judge(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("judge_goal must not run while a decision is pending")

    monkeypatch.setattr(goals, "judge_goal", _boom)


def test_goal_loop_waits_one_turn_then_stops_awaiting_the_decision(monkeypatch):
    _no_judge(monkeypatch)
    prompts, blocks = [], []

    result = goals.run_kanban_goal_loop(
        task_id="t_race",
        goal_text="Corrigir disciplinas do cargo Delegado",
        run_turn=lambda prompt: prompts.append(prompt) or "aguardando o principal",
        task_status_fn=lambda: "running",
        block_fn=blocks.append,
        max_turns=5,
        first_response="Card bloqueado, aguardando Maikol.",
        pending_decision_fn=lambda: "dec_1",
    )

    assert result["outcome"] == "awaiting_decision"
    assert result["turns_used"] == 2
    assert len(prompts) == 1
    assert "dec_1" in prompts[0] and "wait --decision dec_1" in prompts[0]
    assert blocks == []


def test_goal_loop_resumes_once_the_decision_is_answered(monkeypatch):
    _no_judge(monkeypatch)
    statuses = iter(["running", "done"])
    pendings = iter(["dec_1", None])
    prompts = []

    result = goals.run_kanban_goal_loop(
        task_id="t_race",
        goal_text="goal",
        run_turn=lambda prompt: prompts.append(prompt) or "continuei e fechei",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
        max_turns=5,
        first_response="perguntei",
        pending_decision_fn=lambda: next(pendings),
    )

    assert result["outcome"] == "completed_by_worker"
    assert len(prompts) == 1 and "dec_1" in prompts[0]


def test_goal_loop_without_the_hook_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(goals, "judge_goal", lambda goal, response: ("done", "looks done", False, False, False))
    prompts, blocks = [], []

    result = goals.run_kanban_goal_loop(
        task_id="t_old",
        goal_text="goal",
        run_turn=lambda prompt: prompts.append(prompt) or "ok",
        task_status_fn=lambda: "running",
        block_fn=blocks.append,
        max_turns=5,
        first_response="feito",
    )

    assert result["outcome"] == "blocked_budget"
    assert len(prompts) == 1 and "task is still open" in prompts[0]
    assert len(blocks) == 1 and "never called kanban_complete" in blocks[0]
