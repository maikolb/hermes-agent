"""Authorized cancellation closes a task obligation without claiming delivery."""
from __future__ import annotations

import json
import time


def requested(metadata):
    return isinstance(metadata, dict) and metadata.get('disposition') == 'cancelled_by_owner'


def cancel_task(conn, task_id, *, metadata, expected_run_id=None):
    from hermes_cli import kanban_db as kb

    if not requested(metadata):
        raise ValueError('Cancellation requires disposition=cancelled_by_owner')
    for key in ('reason', 'author', 'source', 'authorization_message'):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f'Cancellation requires {key} from the owner authorization')
    if metadata.get('functional_delivery') is not False:
        raise ValueError('Cancellation must explicitly record functional_delivery=false')
    revision = metadata.get('expected_instruction_revision')
    if type(revision) is not int:
        raise ValueError('Read the card and provide expected_instruction_revision before cancelling')
    receipt = {k: metadata[k] for k in ('reason', 'author', 'source', 'authorization_message')}
    receipt.update(disposition='cancelled_by_owner', functional_delivery=False,
                   dependency_satisfied=True, expected_instruction_revision=revision)
    summary = '[CANCELADO] ' + receipt['reason']
    now = int(time.time())
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError(f'Unknown task: {task_id}')
        prior = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrative_cancelled' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if task.status == 'done':
            if prior and json.loads(prior['payload']).get('source') == receipt['source']:
                return True
            raise ValueError('Task is already closed; cancellation cannot rewrite a prior delivery')
        if task.instruction_revision != revision:
            raise ValueError('Card instruction changed after this cancellation was prepared; reconcile the latest owner order')
        if expected_run_id is not None and task.current_run_id != expected_run_id:
            raise ValueError('Execution changed; the stale worker cannot cancel the current run')
        receipt.update(previous_status=task.status, instruction_revision=revision + 1,
                       previous_title=task.title, previous_body=task.body)
        body = (task.body or '') + '\n\n' + summary + '\nAutorizado por: ' + receipt['author'] + '\nMensagem: ' + receipt['source'] + '\n' + receipt['authorization_message']
        title = task.title if task.title.startswith('[CANCELADO]') else '[CANCELADO] ' + task.title
        # The board's existing title/body trigger advances instruction_revision.
        conn.execute("UPDATE tasks SET status='done',title=?,body=?,result=?,completed_at=?,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL,block_kind=NULL WHERE id=?",
                     (title, body, summary, now, task_id))
        run_id = kb._end_run(conn, task_id, outcome='cancelled', status='done', summary=summary, metadata=receipt)
        if run_id is None:
            run_id = kb._synthesize_ended_run(conn, task_id, outcome='cancelled', summary=summary, metadata=receipt)
        # Supersede obsolete questions, retaining their original question,
        # answer, author and timestamps. They can no longer wake this card.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_decisions'").fetchone():
            for row in conn.execute("SELECT id,context FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')", (task_id,)).fetchall():
                context = json.loads(row['context'])
                context['superseded_by_cancellation'] = {'source': receipt['source'], 'at': now}
                conn.execute("UPDATE nfos_decisions SET status='superseded',context=? WHERE id=?", (json.dumps(context, ensure_ascii=False), row['id']))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_workflows'").fetchone():
            row = conn.execute('SELECT state_json FROM nfos_workflows WHERE task_id=?', (task_id,)).fetchone()
            if row:
                state = json.loads(row['state_json']);state['administrative_closure'] = receipt
                conn.execute("UPDATE nfos_workflows SET stage='cancelled',next_action=?,state_json=?,updated_at=? WHERE task_id=?",
                             (summary, json.dumps(state, ensure_ascii=False), now, task_id))
        kb._append_event(conn, task_id, 'administrative_cancelled', receipt, run_id=run_id)
        kb._append_event(conn, task_id, 'completed', dict(receipt, summary=summary, outcome='cancelled'), run_id=run_id)
    # Done satisfies this obligation. Other unfinished parents still gate each
    # child through the normal scheduler. No delivery verifier is disabled.
    kb.recompute_ready(conn)
    kb.notify_task_updated(conn, task_id, ('status', 'title', 'result'))
    # Keep workspace and artifacts. NFOS terminal-run reconciliation owns
    # worker/child termination, including restart recovery and PID identity.
    return True


def completion_refusal(conn, task_id, expected_run_id=None):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None:
        return 'unknown_task: the card does not exist in this board'
    if expected_run_id is not None and task.current_run_id != expected_run_id:
        return 'stale_execution: the current run differs from the caller run'
    if task.status in {'done', 'archived'}:
        return 'already_terminal: status=' + task.status
    if not kb._parents_satisfied(conn, task_id):
        return 'unfinished_dependencies: at least one parent obligation is still open'
    from hermes_cli.nfos_delivery import get_workflow
    if get_workflow(conn, task_id):
        return 'functional_delivery_not_validated: current spec, evidence or Principal acceptance is missing; an owner cancellation must use disposition=cancelled_by_owner'
    return 'completion_precondition_failed: inspect the card state and completion_blocked_delivery events'
