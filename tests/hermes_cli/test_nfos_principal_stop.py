"""A persisted Principal suspension is enforced even if its worker ignores it."""
import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as delivery, nfos_runtime as runtime
from tests.hermes_cli.test_nfos_worker_shutdown import board, worker, enrolled, assert_exited


def test_principal_human_decision_stops_uncooperative_worker_and_children(board, worker):
    conn, directory = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    saved = directory / 'evidence.txt'
    saved.write_text('Keep this work and its original report')
    decision = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Old operational question', context={'checkpoint': 'saved'})
    later = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='New question while the original is pending', context={})
    delivery.resolve_decision(conn, decision, action='human',
        answer='Owner requested stopping this task; retain work until a new owner instruction', author='Principal')
    # Do not call kanban_block for the model. The runtime must apply the decision.
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert_exited(identities)
    assert saved.read_text() == 'Keep this work and its original report'
    assert delivery.get_decision(conn, decision)['status'] == 'human'
    assert delivery.get_decision(conn, later)['status'] == 'pending'
    assert kb.claim_task(conn, task.id) is None
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_worker_exit_confirmed'").fetchone()[0] == 1


def test_suspension_prevents_more_work_before_the_next_dispatcher_tick(board, worker):
    conn, _ = board
    proc, _ = worker
    task = enrolled(conn, proc.pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Need a decision', context={})
    later = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Another question in parallel', context={})
    delivery.resolve_decision(conn, decision, action='human', answer='Stop pending owner instruction', author='Principal')
    with pytest.raises(delivery.OwnershipConflict, match='suspend'):
        delivery.advance(conn, task.id, task.current_run_id, 'analysis', next_action='Ignore the stop')
    # A worker waiting on another question sees the suspension rather than waiting forever.
    assert delivery.wait_decision(conn, later, timeout=0)['id'] == decision
    from hermes_cli import nfos_tool
    assert not nfos_tool._owned(conn, task.id, task.current_run_id)


def test_saved_suspension_cannot_be_claimed_as_new_work(board, worker):
    conn, _ = board
    proc, identities = worker
    task = enrolled(conn, proc.pid)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id,
        kind='impediment', question='Choose', context={})
    delivery.resolve_decision(conn, decision, action='human', answer='Await owner', author='Principal')
    runtime.reconcile_runtime(conn, worker_exit_grace_seconds=0)
    assert_exited(identities)
    # A generic requeue cannot erase a still-unresolved Principal suspension.
    kb.unblock_task(conn, task.id)
    assert kb.claim_task(conn, task.id) is None
    delivery.resume_after_answer(conn, task.id, answer='Resume with A', source={'platform':'telegram','message_id':'new-owner-answer'})
    assert delivery.get_decision(conn, decision)['status'] == 'resolved'
    assert kb.get_task(conn, task.id).status == 'ready'
