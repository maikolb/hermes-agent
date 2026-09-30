import json
import os

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as delivery


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path / 'kanban.db'


def receive(conn, name, *, portal=False, approved=False):
    rid = delivery.receive_request(conn,
        source={'platform': 'telegram', 'chat_id': '-10001', 'thread_id': '8', 'message_id': name,
                'chat_type': 'portal' if portal else 'group'},
        text='Corrigir o acesso solicitado',
        project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'},
        origin={'portal': {'chamado': name}} if portal else None)
    if portal:
        payload = json.loads(delivery.get_request(conn, rid)['payload'])
        payload['support_approval'] = {'required': True, 'approved_at': 123 if approved else None,
                                     'approved_by': 'owner' if approved else None}
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), rid))
        conn.commit()
    return rid


def materialize(conn, name, *, approved=False):
    rid = receive(conn, name, portal=True, approved=approved)
    with kb.write_txn(conn):
        tid = kb.create_task(conn, title=name, assignee='default', requires_repo=False, delivery_type='report')
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        conn.execute('INSERT INTO nfos_workflows(task_id,request_id,updated_at) VALUES(?,?,123)', (tid, rid))
        conn.execute("UPDATE nfos_requests SET status='attached',task_id=? WHERE id=?", (tid, rid))
    return rid, tid


def test_dispatcher_preserves_unapproved_portal_in_todo(board):
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'pending-portal')
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == 'todo'
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_reservation_skips_portal_and_bootstraps_telegram(board):
    with kb.connect_closing(board) as conn:
        portal = receive(conn, 'a-portal', portal=True)
        telegram = receive(conn, 'b-telegram')
        request = delivery.reserve_request(conn, capacity=2)
        assert request['id'] == telegram
        task = delivery.bootstrap_card(conn, telegram, request['claim_token'], pid=os.getpid())
        assert task.status == 'running'
        assert delivery.get_request(conn, portal)['status'] == 'pending'


@pytest.mark.parametrize('approved', [False, True])
def test_claim_checks_approval_even_after_stale_ready_write(board, approved):
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'claim-portal', approved=approved)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        task = kb.claim_task(conn, tid)
        assert (task is not None) is approved
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == int(approved)


def test_approved_portal_promotes_and_claims(board):
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'approved-portal', approved=True)
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, tid).status == 'running'


def test_manual_force_does_not_replace_portal_approval(board):
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'force-portal')
        ok, reason = kb.promote_task(conn, tid, actor='operator', force=True)
        assert ok is False
        assert 'approval' in reason.lower()
        assert kb.get_task(conn, tid).status == 'todo'


def test_portal_without_approval_metadata_cannot_be_reserved(board):
    with kb.connect_closing(board) as conn:
        rid = receive(conn, 'missing-approval', portal=True)
        payload = json.loads(delivery.get_request(conn, rid)['payload'])
        del payload['support_approval']
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), rid))
        conn.commit()
        assert delivery.reserve_request(conn, capacity=2) is None


def test_unapproved_bootstrap_rejects_stale_reservation(board):
    with kb.connect_closing(board) as conn:
        rid = receive(conn, 'stale-reservation', portal=True)
        conn.execute("UPDATE nfos_requests SET status='starting',claim_token='old-token' WHERE id=?", (rid,))
        conn.commit()
        with pytest.raises(delivery.WorkflowError, match='approval'):
            delivery.bootstrap_card(conn, rid, 'old-token', pid=os.getpid())
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_resume_and_review_do_not_bypass_portal_approval(board):
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'resume-portal')
        assert kb._landing_status_after_parents(conn, tid) == 'todo'
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        assert kb.claim_review_task(conn, tid) is None


def test_watchdog_recognizes_portal_hold(board):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    with kb.connect_closing(board) as conn:
        _, tid = materialize(conn, 'watchdog-portal')
        assert 'Balcão' in GatewayKanbanWatchersMixin._ready_watchdog_guard_reason(conn, tid)


def test_real_dispatch_tick_starts_only_approved_card(board):
    with kb.connect_closing(board) as conn:
        _, pending = materialize(conn, 'pending-tick')
        _, approved = materialize(conn, 'approved-tick', approved=True)
        spawned = []
        def spawn(task, workspace_path, board):
            spawned.append(task.id)
            return os.getpid()
        kb.dispatch_once(conn, spawn_fn=spawn, board='pilot', max_spawn=2)
        assert spawned == [approved]
        assert kb.get_task(conn, pending).status == 'todo'
        assert kb.get_task(conn, approved).status == 'running'
