"""Operational questions survive spec progress; publication reviews do not."""
import json
import os
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery


@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = tmp_path / 'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with kb.connect_closing(db) as conn:
        delivery.init_schema(conn)
        request_id = delivery.receive_request(conn,
            source={'platform': 'telegram', 'chat_id': '1', 'thread_id': '2', 'message_id': '3'},
            text='Reconcile the existing report.',
            project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'})
        request = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, request_id, request['claim_token'], pid=os.getpid())
    return db, task


def save_spec(conn, task):
    return delivery.save_spec(conn, task.id, task.current_run_id,
        {'goal': 'Reconcile the report', 'criteria': [{'id': 'C1', 'text': 'Source preserved'}],
         'steps': ['Read and reconcile'], 'delivery_type': 'report'},
        author='Claude TL', evidence={'session': 'test-tl'})


@pytest.mark.parametrize('action', ['continue', 'changes', 'human'])
def test_answer_original_operational_question_after_spec_progress(card, action):
    db, task = card
    with kb.connect_closing(db) as conn:
        decision_id = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind='impediment', question='Which existing source applies?', context={'tried': ['config']})
        save_spec(conn, task)
    # Reconnect like the independent Principal. No duplicate question or manual repair.
    with kb.connect_closing(db) as conn:
        delivery.resolve_decision(conn, decision_id, action=action,
            answer='Use the saved project source' if action != 'human' else 'Owner must identify the source',
            author='Principal')
        decision = delivery.wait_decision(conn, decision_id, timeout=0)
        assert decision['action'] == action
        assert decision['status'] == ('human' if action == 'human' else 'resolved')
        assert decision['spec_revision'] == 0  # Keep the original question's provenance.
        event = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='nfos_principal_resolved'").fetchone()[0])
        assert event['asked_spec_revision'] == 0
        assert event['resolved_spec_revision'] == 1
        current = kb.get_task(conn, task.id)
        assert (current.status, current.current_run_id, current.worker_pid) == ('running', task.current_run_id, task.worker_pid)
        assert conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 1
        assert not delivery.completion_ready(conn, task.id)
        delivery.resolve_decision(conn, decision_id, action=action,
            answer=decision['answer'], author='Principal')
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_principal_resolved'").fetchone()[0] == 1


def test_spec_progress_does_not_make_operational_question_a_review(card):
    db, task = card
    with kb.connect_closing(db) as conn:
        save_spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind='impediment', question='Which source?', context={})
        save_spec(conn, task)
        with pytest.raises(delivery.WorkflowError):
            delivery.resolve_decision(conn, decision, action='approve', answer='Looks good', author='Principal')
        assert delivery.get_decision(conn, decision)['status'] == 'pending'


def test_stale_delivery_review_still_requires_current_spec(card):
    db, task = card
    with kb.connect_closing(db) as conn:
        save_spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind='review', question='Approve this report?', context={})
        save_spec(conn, task)
        with pytest.raises(delivery.WorkflowError, match='Spec changed during review'):
            delivery.resolve_decision(conn, decision, action='approve', answer='Approved', author='Principal')
        assert delivery.get_decision(conn, decision)['status'] == 'pending'


def test_operational_answer_and_provenance_event_commit_together(card):
    db, task = card
    with kb.connect_closing(db) as conn:
        decision = delivery.ask_principal(conn, task.id, task.current_run_id,
            kind='impediment', question='Which source?', context={})
        save_spec(conn, task)
        conn.execute("CREATE TRIGGER fail_resolution BEFORE INSERT ON task_events "
                     "WHEN NEW.kind='nfos_principal_resolved' BEGIN SELECT RAISE(ABORT,'power-loss'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match='power-loss'):
            delivery.resolve_decision(conn, decision, action='continue', answer='Saved source', author='Principal')
        assert delivery.get_decision(conn, decision)['status'] == 'pending'
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='nfos_principal_resolved'").fetchone()
