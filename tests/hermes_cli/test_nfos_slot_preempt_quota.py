"""SLOT_PREEMPT_20260910 and QUOTA_BACKOFF_20260910."""
import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def two_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    project = {"board": "pilot", "profile": "default", "delivery_type": "code", "repo_path": str(tmp_path)}
    cards = []
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        for i in (1, 2):
            rid = d.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "4", "message_id": str(i)},
                                    text=f"[Japa|1] pedido {i}", project=project)
            req = d.reserve_request(conn, capacity=4)
            cards.append(d.bootstrap_card(conn, rid, req["claim_token"], pid=os.getpid()))
    return cards


def _slot(conn):
    return conn.execute("SELECT task_id FROM nfos_project_delivery WHERE project='pilot'").fetchone()


def test_urgent_card_preempts_idle_holder(two_cards):
    holder, urgent = two_cards
    with kb.connect_closing() as conn:
        assert d.acquire_project(conn, "pilot", holder.id, holder.current_run_id, SHA_A) is True
        # não urgente não toma
        assert d.acquire_project(conn, "pilot", urgent.id, urgent.current_run_id, SHA_B) is False
        conn.execute("UPDATE tasks SET priority=100 WHERE id=?", (urgent.id,))
        conn.commit()
        assert d.acquire_project(conn, "pilot", urgent.id, urgent.current_run_id, SHA_B) is True
        assert _slot(conn)["task_id"] == urgent.id
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (holder.id,))]
        assert "nfos_project_delivery_preempted" in kinds
        body = conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (holder.id,)).fetchone()[0]
        assert "[slot]" in body
        # o dono não consegue publicar sem readquirir
        with pytest.raises(d.WorkflowError, match="publication slot"):
            d._project_owned(conn, holder.id, holder.current_run_id, SHA_A)


def test_holder_with_publication_in_flight_is_not_preempted(two_cards):
    holder, urgent = two_cards
    with kb.connect_closing() as conn:
        assert d.acquire_project(conn, "pilot", holder.id, holder.current_run_id, SHA_A) is True
        conn.execute("UPDATE tasks SET priority=100 WHERE id=?", (urgent.id,))
        conn.execute(
            "INSERT INTO nfos_effects (id, task_id, run_id, operation, target, candidate, status, created_at, updated_at) "
            "VALUES ('e1', ?, ?, 'homolog', 'https://hml', ?, 'unknown', ?, ?)",
            (holder.id, holder.current_run_id, SHA_A, int(time.time()), int(time.time())),
        )
        conn.commit()
        assert d.acquire_project(conn, "pilot", urgent.id, urgent.current_run_id, SHA_B) is False
        assert _slot(conn)["task_id"] == holder.id


def test_quota_backoff_doubles_per_consecutive_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "600")
    conn = kb.connect(tmp_path / "k.db")
    now = int(time.time())
    conn.execute("INSERT INTO tasks (id, title, status, created_at, task_role) VALUES ('t_q', 'a', 'ready', ?, 'work')", (now - 7200,))
    # uma saída por cota há 15 min: espera 600 s já passou
    conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) VALUES ('t_q', 'rate_limited', 'rate_limited', ?, ?)",
                 (now - 1200, now - 900))
    conn.commit()
    assert kb.check_respawn_guard(conn, "t_q") != "rate_limit_cooldown"
    # três saídas nas últimas 3 h, a última há 15 min: espera efetiva 2400 s
    for k in (2, 3):
        conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) VALUES ('t_q', 'rate_limited', 'rate_limited', ?, ?)",
                     (now - 900 * k - 300, now - 900 * k))
    conn.commit()
    assert kb.check_respawn_guard(conn, "t_q") == "rate_limit_cooldown"
