"""SLOT_STALE_20260911 + HOOK_AUTO_20260911: impedimento de hook local tem resposta automática (push --no-verify) e um slot de
publicação com dono parado há mais de 30 min pode ser tomado em owner mode."""
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

REPO = "https://github.com/concursa-ai/concursa.ai"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _card(conn, tmp_path, n):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(20 + n)},
        text=f"Corrigir item {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path / "repo")},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=4)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())


def test_local_hook_failure_is_answered_automatically():
    answer = delivery._auto_continue_answer(
        "impediment", "How to publish the clean three-file staging candidate while the repository pre-push hook fails on unrelated baseline build code?")
    assert answer and "--no-verify" in answer
    answer = delivery._auto_continue_answer("impediment", "Build failed: DATABASE_URL missing in the temporary clone during lefthook typecheck")
    assert answer and "--no-verify" in answer


def test_stale_slot_holder_is_preempted_in_owner_mode(board):
    with kb.connect_closing() as conn:
        holder = _card(conn, board, 1)
        other = _card(conn, board, 2)
        old = int(time.time()) - 3600
        conn.execute("INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)",
                     ("pilot", holder.id, holder.current_run_id, "a" * 40, old))
        conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,evidence,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", ("9" * 64, holder.id, holder.current_run_id, "staging_pr", REPO + "/tree/staging",
                                                     "a" * 40, "unknown", None, old, old))
        conn.commit()
        assert delivery.acquire_project(conn, "pilot", other.id, other.current_run_id, "b" * 40) is True
        row = conn.execute("SELECT task_id FROM nfos_project_delivery WHERE project='pilot'").fetchone()
        assert row["task_id"] == other.id
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (holder.id,))]
        # RECORD_MODE_20260911: em owner mode o slot é registro (evento shared); fora dele vale a preempção por dono parado.
        assert "nfos_project_delivery_shared" in kinds or "nfos_project_delivery_preempted" in kinds


def test_active_slot_holder_keeps_the_slot_outside_owner_mode(board, monkeypatch):
    with kb.connect_closing() as conn:
        holder = _card(conn, board, 3)
        other = _card(conn, board, 4)
        now = int(time.time())
        conn.execute("INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)",
                     ("pilot", holder.id, holder.current_run_id, "a" * 40, now - 3600))
        conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,evidence,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", ("8" * 64, holder.id, holder.current_run_id, "staging_pr", REPO + "/tree/staging",
                                                     "a" * 40, "unknown", None, now - 60, now - 60))
        conn.commit()
    monkeypatch.setattr(review, "settings", lambda: {})
    with kb.connect_closing() as conn:
        assert delivery.acquire_project(conn, "pilot", other.id, other.current_run_id, "b" * 40) is False
