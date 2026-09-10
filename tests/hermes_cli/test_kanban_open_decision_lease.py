"""LEASE_RELEASE_20260910: card ready com decisão aberta (pending/human) não é relançado e não deixa o lease do
workspace preso em nome do dispatcher (bug do OPEN_DECISION_SKIP #126: t_998efb26 e t_35bd4a15 presos em 10/09)."""
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review


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
    return tmp_path / "kanban.db"


@pytest.fixture
def worker_process():
    # O run precisa de um pid de worker que não seja o próprio pytest: a reconciliação do dispatcher
    # encerra o processo de um run fechado (SIGKILL), e com os.getpid() ela mataria o runner.
    proc = subprocess.Popen(["sleep", "120"])
    try:
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _ready_card_with_open_question(conn, worker_pid):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": "11"},
        text="Corrigir escolaridade do cargo 133.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=worker_pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="Autoriza corrigir o dado pelo mecanismo oficial?", context={})
    # O worker saiu sem fechar (protocol_violation) com a pergunta ainda aberta: run encerrado, card de volta a ready.
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
                 (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?",
                 (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id), decision


def test_open_decision_skip_releases_the_workspace_lease(board, all_assignees_spawnable, worker_process):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kb.connect_closing() as conn:
        task, decision = _ready_card_with_open_question(conn, worker_process)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert task.id not in spawns
        assert task.id not in [s[0] for s in res.spawned]
        workspace = kb.resolve_workspace(kb.get_task(conn, task.id))
        lease, owner = kb._try_acquire_workspace_lease(workspace, task_id="probe")
        assert lease is not None, f"lease do workspace ficou preso pelo dispatcher: {owner}"
        kb._release_workspace_lease(lease)
        # Pergunta respondida: o card volta a ser candidato, sem cair em skipped_workspace_leased.
        delivery.resolve_decision(conn, decision, action="continue",
                                  answer="CONTINUE: corrija e faça o readback.", author="Principal")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert task.id not in [s[0] for s in res.skipped_workspace_leased]
