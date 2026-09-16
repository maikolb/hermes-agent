"""The existing dispatcher closes accepted delivery without another model turn."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_principal_acceptance import (
    task_context, accept, ask, save_report,
)


def tick(conn):
    def no_worker(*args, **kwargs):
        pytest.fail('Accepted delivery must not spawn another worker')
    return kb.dispatch_once(conn, spawn_fn=no_worker, max_spawn=0,
                            reconcile_orphans=False)


def approve_publication(conn, task):
    decision = ask(conn, task, 'review')
    d.resolve_decision(conn, decision, action='approve', answer='Report approved', author='Principal')


def test_accepted_delivery_closes_on_dispatch_without_worker(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    approve_publication(conn, task)
    accept(conn, task, 'final_review', artifact)
    assert d.completion_ready(conn, task.id)
    tick(conn)
    current = kb.get_task(conn, task.id)
    assert current.status == 'done'
    assert current.current_run_id is None
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
    assert run['status'] == 'done'
    assert json.loads(run['metadata'])['completion_actor'] == 'Principal dispatcher'
    assert current.result == 'Count verified'
    tick(conn)
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completed'",
                        (task.id,)).fetchone()[0] == 1


@pytest.mark.parametrize('state', ['pending', 'changes', 'stale_report', 'stale_evidence'])
def test_unaccepted_or_stale_delivery_does_not_close(task_context, state):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    approve_publication(conn, task)
    decision = ask(conn, task, 'final_review')
    if state == 'changes':
        d.resolve_decision(conn, decision, action='changes', answer='Count needs correction', author='Principal')
    elif state.startswith('stale'):
        accept(conn, task, 'final_review', artifact)
        if state == 'stale_evidence':
            artifact.write_text('count=32\n')
        else:
            report = json.loads(d._artifact(conn, task.id, 'report')['content'])
            report['summary'] = 'Count verification changed: an additional source needs reconciliation'
            d.save_report(conn, task.id, task.current_run_id, report)
    tick(conn)
    assert kb.get_task(conn, task.id).status != 'done'


def test_dry_run_does_not_complete_accepted_delivery(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    approve_publication(conn, task)
    accept(conn, task, 'final_review', artifact)
    kb.dispatch_once(conn, dry_run=True, max_spawn=0, reconcile_orphans=False)
    assert kb.get_task(conn, task.id).status != 'done'


def test_final_acceptance_does_not_bypass_publication_gate(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    accept(conn, task, 'final_review', artifact)
    tick(conn)
    assert kb.get_task(conn, task.id).status != 'done'


def test_urgent_accepted_delivery_closes_before_older_normal(task_context):
    conn, normal, spec, artifact = task_context
    rid = d.receive_request(conn, source={'platform': 'fixture', 'chat_id': 'local', 'thread_id': 'test', 'message_id': 'urgent'},
        text='Urgent count', project={'profile': 'default', 'delivery_type': 'report', 'priority': 100})
    claim = d.reserve_request(conn, capacity=3)
    urgent = d.bootstrap_card(conn, rid, claim['claim_token'], pid=os.getpid())
    urgent_workspace = artifact.parent.parent / urgent.id
    urgent_workspace.mkdir()
    urgent_artifact = urgent_workspace / 'count.txt'
    urgent_artifact.write_text('count=31\n')
    kb.set_workspace_path(conn, urgent.id, str(urgent_workspace))
    d.save_spec(conn, urgent.id, urgent.current_run_id, spec, author='worker', evidence={'fixture': True})
    for task in (normal, urgent):
        evidence = artifact if task.id == normal.id else urgent_artifact
        accept(conn, task, 'spec_review')
        save_report(conn, task, evidence)
        approve_publication(conn, task)
        accept(conn, task, 'final_review', evidence)
    tick(conn)
    assert [r[0] for r in conn.execute("SELECT task_id FROM task_events WHERE kind='completed' ORDER BY id")] == [urgent.id, normal.id]


def test_gateway_real_dispatch_tick_completes_after_acceptance(task_context, monkeypatch, tmp_path):
    import asyncio
    import time
    from gateway.run import GatewayRunner
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review')
    save_report(conn, task, artifact)
    approve_publication(conn, task)
    config = {'kanban': {'dispatch_in_gateway': True, 'dispatch_interval_seconds': 1,
                         'max_spawn': 0, 'auto_decompose': False,
                         'delivery': {'principal_validation': True}}}
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(kb, 'list_boards', lambda **kwargs: [{'slug': 'default'}])
    first_tick = []
    real_dispatch = kb.dispatch_once
    def dispatch(*args, **kwargs):
        kwargs['reconcile_orphans'] = False  # Fixture PID is pytest, not a spawned worker.
        value = real_dispatch(*args, **kwargs)
        first_tick.append(time.monotonic())
        return value
    monkeypatch.setattr(kb, 'dispatch_once', dispatch)
    runner = object.__new__(GatewayRunner)
    runner._running = True
    async def run():
        watcher = asyncio.create_task(runner._kanban_dispatcher_watcher())
        try:
            async def started():
                while not first_tick:
                    await asyncio.sleep(.01)
            # The real gateway has a five-second adapter-wiring startup delay.
            # Measure acceptance latency only after startup and the first tick.
            await asyncio.wait_for(started(), 10)
            accepted_at = time.monotonic()
            accept(conn, task, 'final_review', artifact)
            async def completed():
                while kb.get_task(conn, task.id).status != 'done':
                    await asyncio.sleep(.01)
            await asyncio.wait_for(completed(), 4)
            elapsed = time.monotonic() - accepted_at
            (tmp_path/'accepted-to-done.json').write_text(json.dumps({
                'dispatch_interval_seconds': 1, 'accepted_to_done_seconds': elapsed,
                'model_turns_after_acceptance': 0}))
            assert elapsed < 1.5  # One configured tick plus scheduling/DB overhead.
        finally:
            runner._running = False
            await asyncio.wait_for(watcher, 3)
    asyncio.run(run())
