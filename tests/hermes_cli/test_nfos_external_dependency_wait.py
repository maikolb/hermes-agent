"""An external technical impediment is not a satisfied task dependency."""
import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept


@pytest.mark.parametrize('completed_parent', [False, True])
def test_external_wait_keeps_run_and_requests_one_internal_decision(task_context, completed_parent):
    conn, task, _, _ = task_context
    accept(conn, task, 'spec_review')
    if completed_parent:
        parent = kb.create_task(conn, title='Already delivered', assignee='default', requires_repo=False)
        kb.complete_task(conn, parent, summary='Delivered')
        kb.link_tasks(conn, parent, task.id)
    for _ in range(3):
        assert kb.block_task(conn, task.id, kind='dependency', reason='Runtime technical defect prevents merge; wait for maintainer fix', expected_run_id=task.current_run_id)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    current = kb.get_task(conn, task.id)
    assert current.current_run_id == task.current_run_id
    assert current.status == 'running' and current.claim_lock == task.claim_lock
    pending = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,)).fetchall()
    assert len(pending) == 1 and pending[0]['kind'] == 'impediment'
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='dependency_wait'", (task.id,)).fetchone()
    d.resolve_decision(conn, pending[0]['id'], action='continue', answer='Runtime corrected; retry the preserved candidate', author='Principal')
    assert d.wait_decision(conn, pending[0]['id'], timeout=0)['action'] == 'continue'
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


def test_real_unfinished_dependency_waits_until_parent_completes(task_context):
    conn, task, _, _ = task_context
    parent = kb.create_task(conn, title='Needed delivery', assignee='default', requires_repo=False)
    kb.link_tasks(conn, parent, task.id)
    assert kb.block_task(conn, task.id, kind='dependency', reason='Wait for parent delivery', expected_run_id=task.current_run_id)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task.id).status == 'todo'
    assert not conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND kind='impediment'", (task.id,)).fetchone()
    kb.complete_task(conn, parent, summary='Delivered')
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task.id).status == 'ready'
