"""BUDGET_EXITS_20261010: a lineage out of budget leaves by an exit of the runtime, not by a question to a human."""
import json
import os
import sys
import time

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_principal_review as review
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_principal_acceptance import task_context  # noqa: F401
from tests.hermes_cli.test_nfos_escalation_budget import resumed, exhausted_idle  # noqa: F401
from tests.hermes_cli.test_nfos_execution_repair import paused  # noqa: F401


@pytest.fixture
def overrun(paused, monkeypatch):
    """Idle class M card (7200 s) whose lineage spent 7230 s and all 400 iterations, held by the pause of its timeout."""
    conn, task, _, args = paused
    first = review.worker_escalation(conn, task.id)['first_run_id']
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=100,ended_at=7330 WHERE id=?', (first,))
        conn.execute("UPDATE tasks SET block_kind='awaiting_principal' WHERE id=?", (task.id,))
    # The fixture run carries this test process as its worker; the worker of an idle card is gone.
    monkeypatch.setattr(d, '_run_process_alive', lambda conn, task_id, run_id: False)
    monkeypatch.setattr('hermes_cli.nfos_runtime.run_termination_pending', lambda conn, task_id, run_id: False)
    identity = dict(actor='Principal', reason='Balance of the request fits one class up',
                    expected_run_id=task.current_run_id, expected_instruction_revision=task.instruction_revision)
    return conn, task, identity, args


def spend(conn, task, seconds):
    """The lineage has spent this many seconds in all."""
    first = review.worker_escalation(conn, task.id)['first_run_id']
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=100,ended_at=? WHERE id=?', (100 + seconds, first))


def budget_decision(conn, task):
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        return repair.request_execution_budget_review(conn, current, repair.execution_budget(conn, current))


def pause_decision(conn, task):
    repair.reconcile_maintenance_recovery(conn)
    return conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?",
                        (task.id, task.current_run_id)).fetchone()['id']


def ask(conn, decision_id, to='Maikol'):
    question = 'Autoriza uma concessão adicional de 2700 segundos para este card?'
    d.resolve_decision(conn, decision_id, action='human', answer=f'PERGUNTA para {to}: ' + question, author='Principal',
                       public_message={'kind': 'question', 'text': question, 'to': to})


def events(conn, task, kind):
    return conn.execute('SELECT count(*) FROM task_events WHERE task_id=? AND kind=?', (task.id, kind)).fetchone()[0]


def open_decisions(conn, task):
    return conn.execute("SELECT count(*) FROM nfos_decisions WHERE task_id=? AND status IN ('pending','human')", (task.id,)).fetchone()[0]


def test_exhausted_lineage_is_offered_one_class_up_and_a_closing_allowance(overrun):
    conn, task, _, _ = overrun
    exits = review.budget_exits(conn, task.id)
    assert (exits['size'], exits['remaining_runtime_seconds'], exits['remaining_iterations']) == ('M', -30, 0)
    assert exits['reclassify'] == dict(from_class='M', to_class='G', runtime_seconds=7200, iterations=400, max_runtime_seconds=14400)
    assert exits['partial_closure'] == dict(runtime_seconds=2730, iterations=60, continuation=None)
    assert (exits['exhausted'], exits['terminal'], exits['used']) == (True, False, [])
    assert exits['exhausted_by'] == ['runtime', 'iterations']


def test_iteration_ceiling_alone_is_read_as_iterations_whatever_the_run_was_labelled(overrun):
    """The agent records an iteration ceiling as timed_out; the exits are decided by the measured balance, not by that label."""
    conn, task, identity, _ = overrun
    spend(conn, task, 3000)
    exits = review.budget_exits(conn, task.id)
    assert exits['exhausted_by'] == ['iterations'] and exits['remaining_runtime_seconds'] == 4200
    assert exits['partial_closure'] == dict(runtime_seconds=0, iterations=60, continuation=None)
    text = review.budget_exits_text(exits)
    assert 'Esgotou por iterações' in text and 'tempo 4200 s' in text
    receipt = review.partial_closure_budget(conn, task.id, **identity)
    assert (receipt['runtime_seconds'], receipt['iterations'], receipt['max_runtime_seconds']) == (0, 60, 7200)
    assert repair.execution_budget(conn, kb.get_task(conn, task.id))['remaining_iterations'] == 60


def test_question_survives_a_failure_to_read_the_offer(overrun, monkeypatch):
    conn, task, _, _ = overrun
    monkeypatch.setattr(review, 'budget_exits', lambda conn, task_id: 1 / 0)
    question = d.get_decision(conn, budget_decision(conn, task))['question']
    assert 'budget-exits' in question and 'grant-budget' in question and 'repair-execution' in question


def test_reclassify_is_sized_by_the_runtime_happens_once_and_releases_through_the_existing_repair(overrun):
    conn, task, identity, args = overrun
    receipt = review.reclassify_budget(conn, task.id, **identity)
    assert (receipt['exit'], receipt['runtime_seconds'], receipt['iterations']) == ('reclassify', 7200, 400)
    assert (receipt['previous_max_runtime_seconds'], receipt['max_runtime_seconds']) == (7200, 14400)
    assert receipt['remaining_runtime_seconds'] == 7170
    assert review.reclassify_budget(conn, task.id, **dict(identity, reason='asked again')) == receipt
    assert kb.get_task(conn, task.id).max_runtime_seconds == 14400
    assert len(review.iteration_grants(conn, task.id)) == 1
    exits = review.budget_exits(conn, task.id)
    assert (exits['exhausted'], exits['reclassify'], exits['partial_closure'], exits['used']) == (False, None, None, ['reclassify'])
    assert 'positivo' in review.budget_exits_text(exits)
    assert repair.repair_execution(conn, task.id, **args, apply=True)['status'] == 'ready'
    assert kb.claim_task(conn, task.id) is not None


def test_no_second_class_for_a_card_that_already_rose(overrun):
    conn, task, identity, _ = overrun
    review.grant_iteration_budget(conn, task.id, grant_id='owner-once', iterations=1, runtime_seconds=2700, **identity)
    spend(conn, task, 9950)
    exits = review.budget_exits(conn, task.id)
    assert exits['exhausted'] and exits['reclassify'] is None and exits['partial_closure']
    with pytest.raises(d.WorkflowError, match='(?s)reclassify-budget is not available.*partial-closure-budget'):
        review.reclassify_budget(conn, task.id, **identity)
    assert [g['grant_id'] for g in review.iteration_grants(conn, task.id)] == ['owner-once']


def test_largest_class_has_only_the_closing_exit(overrun):
    conn, task, _, _ = overrun
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET max_runtime_seconds=14400 WHERE id=?', (task.id,))
    spend(conn, task, 14430)
    exits = review.budget_exits(conn, task.id)
    assert exits['size'] == 'G' and exits['reclassify'] is None and exits['partial_closure']['runtime_seconds'] == 2730


def test_class_comes_from_the_size_the_spec_declared(overrun, monkeypatch):
    conn, task, _, _ = overrun
    monkeypatch.setattr(d, 'get_spec', lambda conn, task_id: {'content': json.dumps({'size': 'g'})})
    exits = review.budget_exits(conn, task.id)
    assert exits['size'] == 'G' and exits['reclassify'] is None and exits['partial_closure']


def test_class_up_that_would_still_leave_no_balance_is_not_offered(paused):
    conn, task, _, _ = paused  # the fixture spent 24052 s on a 7200 s card: G (14400 s) would start already exhausted
    exits = review.budget_exits(conn, task.id)
    assert exits['reclassify'] is None and exits['partial_closure']['runtime_seconds'] == 2700 + 24052 - 7200


def test_closing_exit_is_not_offered_while_the_balance_fits_a_closing_run(overrun):
    conn, task, identity, _ = overrun
    review.reclassify_budget(conn, task.id, **identity)
    with pytest.raises(d.WorkflowError, match='partial-closure-budget is not available'):
        review.partial_closure_budget(conn, task.id, **identity)
    assert d.continuation_links(conn, task.id)['children'] == []
    assert len(review.iteration_grants(conn, task.id)) == 1


def test_closing_exit_moves_the_request_first_and_the_resumed_worker_is_told_only_to_close(overrun):
    conn, task, identity, args = overrun
    assert 'Budget closing run' not in d.worker_context(conn, task.id)
    receipt = review.partial_closure_budget(conn, task.id, **identity)
    children = d.continuation_links(conn, task.id)['children']
    assert len(children) == 1 and kb.get_task(conn, children[0]).status not in ('done', 'archived')
    assert (receipt['exit'], receipt['runtime_seconds'], receipt['iterations']) == ('partial_closure', 2730, 60)
    assert receipt['continuation'] == children[0]
    assert receipt['remaining_runtime_seconds'] == review.BUDGET_CLOSING_SECONDS == 2700
    assert review.partial_closure_budget(conn, task.id, **identity) == receipt
    assert d.continuation_links(conn, task.id)['children'] == children
    # At the front of what the worker reads: the case context is cut at 9000 characters.
    context = d.worker_context(conn, task.id)
    assert context.startswith('\n\nCase context (this request):\nBudget closing run')
    assert f'continuation="{children[0]}"' in context and 'partial_delivery=true' in context
    exits = review.budget_exits(conn, task.id)
    assert (exits['exhausted'], exits['partial_closure'], exits['used']) == (False, None, ['partial_closure'])
    assert repair.repair_execution(conn, task.id, **args, apply=True)['status'] == 'ready'
    assert kb.claim_task(conn, task.id) is not None
    # The report the closing run saves is the partial delivery the existing acceptance already knows.
    assert d._continuation_allows_partial(conn, task.id, {'partial_delivery': True, 'continuation': children[0]})


def test_card_that_took_the_closing_exit_only_closes_no_class_up_after_it(overrun):
    conn, task, identity, _ = overrun
    review.partial_closure_budget(conn, task.id, **identity)
    spend(conn, task, 10000)
    exits = review.budget_exits(conn, task.id)
    assert exits['exhausted'] and exits['reclassify'] is None and exits['partial_closure'] is None and exits['terminal']
    with pytest.raises(d.WorkflowError, match='reclassify-budget is not available'):
        review.reclassify_budget(conn, task.id, **identity)
    review.grant_iteration_budget(conn, task.id, grant_id='owner-extends', iterations=10, runtime_seconds=600, **identity)
    assert 'Budget closing run' in d.worker_context(conn, task.id)


def test_budget_exits_belong_to_the_principal_outside_a_worker(overrun, monkeypatch):
    conn, task, identity, _ = overrun
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    for exit_call in (review.reclassify_budget, review.partial_closure_budget):
        with pytest.raises(d.WorkflowError, match='outside a worker'):
            exit_call(conn, task.id, **identity)
    assert review.iteration_grants(conn, task.id) == [] and d.continuation_links(conn, task.id)['children'] == []


def test_stale_card_identity_takes_no_exit_and_opens_no_continuation(overrun):
    conn, task, identity, _ = overrun
    stale = dict(identity, expected_run_id=identity['expected_run_id'] + 1)
    for exit_call in (review.reclassify_budget, review.partial_closure_budget):
        with pytest.raises(d.WorkflowError, match='changed'):
            exit_call(conn, task.id, **stale)
    assert review.iteration_grants(conn, task.id) == [] and d.continuation_links(conn, task.id)['children'] == []


def test_closing_exit_waits_for_the_previous_executor_before_opening_the_continuation(overrun, monkeypatch):
    conn, task, identity, _ = overrun
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *args: True)
    with pytest.raises(d.WorkflowError, match='termination'):
        review.partial_closure_budget(conn, task.id, **identity)
    assert review.iteration_grants(conn, task.id) == [] and d.continuation_links(conn, task.id)['children'] == []


def test_question_of_an_exhausted_budget_lists_the_exits_and_no_longer_sends_after_an_authorization(overrun):
    conn, task, _, _ = overrun
    question = d.get_decision(conn, budget_decision(conn, task))['question']
    assert 'reclassify-budget' in question and 'de M para G' in question and 'partial-closure-budget' in question
    assert 'não é pergunta ao dono' in question and 'Verifique a autorização existente' not in question
    assert 'grant-budget' in question and 'repair-execution' in question  # the wake of the Principal still names both


def test_pause_of_an_exhausted_budget_asks_with_the_exits_and_names_its_pause(overrun):
    conn, task, _, _ = overrun
    row = d.get_decision(conn, pause_decision(conn, task))
    assert json.loads(row['context'])['maintenance_recovery']['resume_route'] == 'repair-execution'
    assert 'partial-closure-budget' in row['question'] and f'para a pausa {task.current_run_id}' in row['question']
    assert 'Execute o reparo desta pausa' not in row['question'] and '"dependency"' in row['question']


@pytest.mark.parametrize('action', ['continue', 'changes'])
def test_timeout_opens_one_obligation_and_an_explanation_does_not_orphan_the_card(resumed, monkeypatch, action):
    """The real timeout. On 24/09/2026 this review was answered with changes and the card stayed 12 days with no open decision."""
    conn, task, _ = resumed
    monkeypatch.setattr(kb.time, 'time', lambda: 1041)
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *args: None) == [task.id]
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ('blocked', 'awaiting_principal')
    obligations = d.open_budget_obligations(conn, task.id)
    assert len(obligations) == 1
    d.sweep_awaiting_principal(conn)
    assert d.open_budget_obligations(conn, task.id) == obligations  # the pause does not ask a second time
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    d.resolve_decision(conn, obligations[0], action=action, answer='Pausa técnica mantida: saldo esgotado.', author='Principal')
    row = d.get_decision(conn, obligations[0])
    context = json.loads(row['context'])
    assert row['status'] == 'pending' and context['maintenance_deferrals'] == 1
    assert d._reminder_gap(context) == 2 * d.DECISION_REMINDER_GAP
    assert events(conn, task, 'nfos_maintenance_recovery_deferred') == 1
    d.sweep_awaiting_principal(conn)
    assert kb.get_task(conn, task.id).status == 'blocked' and d.open_budget_obligations(conn, task.id) == obligations


def test_claim_refused_for_budget_without_a_pause_does_not_loop_after_an_explanation(overrun):
    conn, task, _, _ = overrun
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (task.current_run_id,))
        conn.execute("UPDATE tasks SET status='ready',block_kind=NULL WHERE id=?", (task.id,))
    refusal = []
    assert kb.claim_task(conn, task.id, refusal=refusal) is None and refusal == ['execution_budget_exhausted']
    obligations = d.open_budget_obligations(conn, task.id)
    assert len(obligations) == 1
    d.resolve_decision(conn, obligations[0], action='continue', answer='Sem saldo; mantenho a retenção.', author='Principal')
    assert d.get_decision(conn, obligations[0])['status'] == 'pending'
    for _ in range(2):
        assert task.id not in d.sweep_awaiting_principal(conn)
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert events(conn, task, 'nfos_execution_budget_exhausted') == 1


def test_balance_review_answered_after_a_grant_is_resolved_and_the_pause_then_asks_for_its_repair(overrun):
    conn, task, identity, _ = overrun
    decision_id = budget_decision(conn, task)
    review.reclassify_budget(conn, task.id, **identity)
    d.resolve_decision(conn, decision_id, action='continue', answer='Classe subiu; sigo para o reparo.', author='Principal')
    assert d.get_decision(conn, decision_id)['status'] == 'resolved'
    question = d.get_decision(conn, pause_decision(conn, task))['question']
    assert 'positivo' in question and f'repair-execution dry-run/apply para a pausa {task.current_run_id}' in question


@pytest.mark.parametrize('to', ['Maikol', 'Ana'])
def test_no_human_is_asked_for_budget_owner_or_requester(overrun, to):
    conn, task, _, _ = overrun
    for obligation in (budget_decision, pause_decision_after):
        decision_id = obligation(conn, task)
        with pytest.raises(d.WorkflowError, match='(?s)BUDGET_EXITS_20261010.*reclassify-budget'):
            ask(conn, decision_id, to=to)
        assert d.get_decision(conn, decision_id)['status'] == 'pending'


def test_budget_refusal_comes_before_the_general_refusal_of_owner_questions(overrun, monkeypatch):
    conn, task, _, _ = overrun
    monkeypatch.setattr(d, '_owner_questions_refused', lambda conn, task_id: True)
    with pytest.raises(d.WorkflowError, match='(?s)BUDGET_EXITS_20261010.*partial-closure-budget'):
        ask(conn, budget_decision(conn, task))


def pause_decision_after(conn, task):
    """The obligation of the pause, born once the balance review is gone."""
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='superseded' WHERE task_id=? AND status='pending'", (task.id,))
    return pause_decision(conn, task)


def test_open_owner_question_about_budget_returns_to_the_principal_and_the_repair_closes_it(overrun):
    conn, task, identity, args = overrun
    old = pause_decision(conn, task)
    with kb.write_txn(conn):
        asked = json.loads(d.get_decision(conn, old)['context'])
        asked['maintenance_deferrals'] = 3
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=?,context=? WHERE id=?",
                     ('PERGUNTA para Maikol: Autoriza uma única concessão adicional de 2700 segundos?\nPergunta interna.',
                      json.dumps(asked), old))
    moved = d.sweep_budget_exits(conn)
    assert [pair[0] for pair in moved] == [old]
    new = d.get_decision(conn, moved[0][1])
    context = json.loads(new['context'])
    assert (new['status'], new['kind']) == ('pending', 'impediment')
    assert context['supersedes'] == old and context['budget_exits'] is True
    assert context['maintenance_recovery']['pause_run_id'] == task.current_run_id and context['maintenance_deferrals'] == 3
    assert 'reclassify-budget' in new['question'] and 'Autoriza uma única concessão adicional' in new['question']
    superseded = d.get_decision(conn, old)
    assert superseded['status'] == 'superseded' and json.loads(superseded['context'])['superseded_reason'] == 'budget_exits'
    refusal = []
    assert kb.get_task(conn, task.id).status == 'blocked'
    assert kb.claim_task(conn, task.id, refusal=refusal) is None and refusal == ['maintenance_pause']
    assert d.sweep_budget_exits(conn) == [] and d.open_budget_obligations(conn, task.id) == [new['id']]
    d.sweep_awaiting_principal(conn)
    assert d.open_budget_obligations(conn, task.id) == [new['id']]  # the pause does not open a second obligation
    # The Principal takes an exit alone and the existing repair releases the card and ends the obligation.
    review.partial_closure_budget(conn, task.id, **identity)
    assert repair.repair_execution(conn, task.id, **args, apply=True)['status'] == 'ready'
    repair.reconcile_maintenance_recovery(conn)
    closed = d.get_decision(conn, new['id'])
    assert (closed['status'], closed['author']) == ('resolved', 'NFOS automation')
    assert d.open_budget_obligations(conn, task.id) == []


def test_requester_question_keeps_its_own_deadline_and_an_answer_already_given_is_not_thrown_away(overrun):
    conn, task, _, _ = overrun
    decision_id = budget_decision(conn, task)
    with kb.write_txn(conn):
        context = json.loads(d.get_decision(conn, decision_id)['context'])
        context['public_message'] = {'kind': 'question', 'text': 'Pode esperar mais um dia?', 'to': 'Ana'}
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=?,context=? WHERE id=?",
                     ('PERGUNTA para Ana: Pode esperar mais um dia?', json.dumps(context), decision_id))
    assert d.review_budget_human_decisions(conn) == []
    with kb.write_txn(conn):
        context.pop('public_message')
        context['human_reply'] = {'answer': 'Autorizo', 'author': 'Maikol'}
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (json.dumps(context), decision_id))
    assert d.review_budget_human_decisions(conn) == []
    assert d.get_decision(conn, decision_id)['status'] == 'human'


def test_human_question_that_is_not_about_budget_is_left_alone(overrun):
    conn, task, _, _ = overrun
    with kb.write_txn(conn):
        conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,author,answer) "
                     "VALUES('dec_other',?,?,'impediment','Which server?','{}',1,1,'human','human','Principal','PERGUNTA para Ana: qual servidor?')",
                     (task.id, task.current_run_id))
    assert d.sweep_budget_exits(conn) == []
    assert d.get_decision(conn, 'dec_other')['status'] == 'human'


def test_closing_run_that_runs_out_again_is_retained_by_the_runtime_and_nobody_is_asked(overrun, monkeypatch):
    """The whole way: closing exit, release, the closing run times out, and the runtime ends it alone."""
    conn, task, identity, args = overrun
    receipt = review.partial_closure_budget(conn, task.id, **identity)
    repair.repair_execution(conn, task.id, **args, apply=True)
    closing = kb.claim_task(conn, task.id)
    kb._set_worker_pid(conn, task.id, os.getpid())
    started = conn.execute('SELECT started_at FROM task_runs WHERE id=?', (closing.current_run_id,)).fetchone()[0]
    monkeypatch.setattr(kb.time, 'time', lambda: started + 2800)
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda *a: None) == [task.id]
    exits = review.budget_exits(conn, task.id)
    assert exits['terminal'] and 'Não há pergunta a fazer' in review.budget_exits_text(exits)
    obligations = d.open_budget_obligations(conn, task.id)
    assert len(obligations) == 1
    assert d.sweep_budget_exits(conn) == [(task.id, 'terminal')]
    pause = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (closing.current_run_id,)).fetchone()[0])['maintenance_pause']
    assert pause['terminal']['kind'] == 'budget_exits_used' and pause['terminal']['continuation'] == receipt['continuation']
    closed = d.get_decision(conn, obligations[0])
    assert (closed['status'], closed['author']) == ('resolved', 'NFOS automation') and receipt['continuation'] in closed['answer']
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ('blocked', 'awaiting_principal')
    assert receipt['continuation'] in d.get_workflow(conn, task.id)['next_action']
    # Nothing reopens it: no obligation, no question, no claim, and the sweeps repeat nothing.
    for _ in range(2):
        d.sweep_awaiting_principal(conn)
        assert d.sweep_budget_exits(conn) == []
    assert open_decisions(conn, task) == 0 and events(conn, task, 'nfos_budget_terminal') == 1
    refusal = []
    assert kb.claim_task(conn, task.id, refusal=refusal) is None and refusal == ['maintenance_pause']
    with pytest.raises(d.WorkflowError, match='BUDGET_EXITS_20261010'):
        ask(conn, obligations[0])
    assert kb.get_task(conn, receipt['continuation']).status not in ('done', 'archived')


def test_owner_question_of_a_lineage_with_no_exit_left_is_ended_by_the_runtime_not_returned(overrun):
    conn, task, identity, _ = overrun
    old = pause_decision(conn, task)
    review.partial_closure_budget(conn, task.id, **identity)
    spend(conn, task, 10000)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=? WHERE id=?",
                     ('PERGUNTA para Maikol: Autoriza mais orçamento?', old))
    assert d.sweep_budget_exits(conn) == [(old, 'terminal')]
    row = d.get_decision(conn, old)
    assert row['status'] == 'superseded' and json.loads(row['context'])['superseded_reason'] == 'budget_terminal'
    assert open_decisions(conn, task) == 0 and kb.get_task(conn, task.id).status == 'blocked'
    d.sweep_awaiting_principal(conn)
    assert open_decisions(conn, task) == 0


def test_chain_at_its_limit_gets_no_other_card_and_ends_retained(overrun):
    conn, task, identity, _ = overrun
    d._ensure_continuations(conn)
    with kb.write_txn(conn):
        conn.execute("INSERT INTO nfos_continuations(child_id,parent_id,created_at) VALUES(?,'t_second',1),('t_second','t_first',1)", (task.id,))
    exits = review.budget_exits(conn, task.id)
    assert len(exits['chain']) == review.BUDGET_CHAIN_LIMIT and exits['partial_closure'] is None and exits['reclassify']
    review.reclassify_budget(conn, task.id, **identity)
    spend(conn, task, 14500)
    assert review.budget_exits(conn, task.id)['terminal']
    marker = review.budget_terminal(conn, task.id)
    assert marker['continuation'] is None and 'Não há card de continuação' in d.get_workflow(conn, task.id)['next_action']
    assert review.budget_terminal(conn, task.id) is None


def test_runtime_does_not_retain_while_the_previous_executor_is_leaving_or_another_pause_holds_the_card(overrun, monkeypatch):
    conn, task, identity, _ = overrun
    review.partial_closure_budget(conn, task.id, **identity)
    spend(conn, task, 10000)
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        metadata = json.loads(row['metadata'])
        budget_pause = dict(metadata['maintenance_pause'])
        metadata['maintenance_pause'] = {'actor': 'Principal', 'reason': 'Workspace binding under repair'}
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
    assert review.budget_terminal(conn, task.id) is None
    with kb.write_txn(conn):
        metadata['maintenance_pause'] = budget_pause
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *args: True)
    assert review.budget_terminal(conn, task.id) is None
    monkeypatch.setattr('hermes_cli.nfos_runtime.previous_runs_termination_pending', lambda *args: False)
    assert review.budget_terminal(conn, task.id)['kind'] == 'budget_exits_used'


def test_retention_of_a_claim_refusal_keeps_the_record_of_a_pause_already_repaired(overrun):
    conn, task, identity, _ = overrun
    review.partial_closure_budget(conn, task.id, **identity)
    spend(conn, task, 10000)
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        metadata = json.loads(row['metadata'])
        metadata['maintenance_pause'].update(repaired_at=5, repaired_by='Principal', repair_kind='execution')
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
    assert review.budget_terminal(conn, task.id)['kind'] == 'budget_exits_used'
    metadata = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0])
    assert metadata['maintenance_pause_history'][0]['repaired_by'] == 'Principal'
    assert metadata['maintenance_pause']['terminal'] and repair.maintenance_pause_pending(conn, task.id)


def test_spec_saved_again_does_not_lower_a_limit_an_exit_raised(overrun, monkeypatch):
    conn, task, identity, args = overrun
    assert d._granted_runtime_limit(conn, task.id) == 0
    review.reclassify_budget(conn, task.id, **identity)
    assert d._granted_runtime_limit(conn, task.id) == 14400
    repair.repair_execution(conn, task.id, **args, apply=True)
    current = kb.claim_task(conn, task.id)
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(current.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', current.claim_lock)
    spec = json.loads(d.get_spec(conn, task.id)['content'])
    d.save_spec(conn, task.id, current.current_run_id, dict(spec, size='M', goal=spec['goal'] + ' (revised)'),
                author='Claude TL', evidence={'session': 'synthetic-tl'})
    assert kb.get_task(conn, task.id).max_runtime_seconds == 14400


def test_cli_reads_the_exits_and_takes_one(overrun, tmp_path, monkeypatch, capsys):
    conn, task, identity, _ = overrun
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'budget-exits', '--task', task.id])
    d.main()
    assert json.loads(capsys.readouterr().out)['reclassify']['to_class'] == 'G'
    path = tmp_path / 'exit.json'
    path.write_text(json.dumps(identity))
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'partial-closure-budget', '--task', task.id, '--input', str(path)])
    d.main()
    assert json.loads(capsys.readouterr().out)['exit'] == 'partial_closure'


# ---- Cards already held before this rule: the engine moves them alone, through the real tick.

OLD_REVIEW = ('Saldo de execução esgotado. Verifique a autorização existente para concessão finita com grant-budget; após saldo '
              'positivo e saída confirmada, use repair-execution dry-run/apply.')


def legacy_timeout(conn, task):
    """The state the old timeout left: pause with its reason, block kind untouched, balance review answered and closed."""
    decision_id = budget_decision(conn, task)
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        metadata = json.loads(row['metadata'])
        metadata['maintenance_pause'].update(at=1000, reason='Cumulative runtime exhausted; grant-budget then repair-execution before resuming')
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
        conn.execute("UPDATE tasks SET block_kind=NULL WHERE id=?", (task.id,))
        context = json.loads(d.get_decision(conn, decision_id)['context'])
        context.pop('budget_exits')
        conn.execute("UPDATE nfos_decisions SET status='resolved',action='changes',author='Principal',answer='Pausa mantida.',"
                     "resolved_at=1100,question=?,context=? WHERE id=?", (OLD_REVIEW, json.dumps(context), decision_id))
    return decision_id


def test_card_orphaned_by_the_old_timeout_gets_its_obligation_and_its_reminder_from_the_real_tick(overrun, monkeypatch):
    """24/09/2026, t_56d65bd8: review answered `changes`, then 12 days blocked with nothing open."""
    from hermes_cli import nfos_runtime
    conn, task, _, _ = overrun
    # The fixture run records this test process as its worker: the tick must not end it.
    monkeypatch.setattr(nfos_runtime, 'reconcile_terminal_workers', lambda conn, **kwargs: [])
    legacy_timeout(conn, task)
    assert open_decisions(conn, task) == 0
    nfos_runtime.reconcile_runtime(conn, lab_sweep=False)
    held = kb.get_task(conn, task.id)
    assert (held.status, held.block_kind) == ('blocked', 'awaiting_principal') and events(conn, task, 'nfos_budget_hold_restored') == 1
    obligations = d.open_budget_obligations(conn, task.id)
    assert len(obligations) == 1
    row = d.get_decision(conn, obligations[0])
    assert 'reclassify-budget' in row['question'] and json.loads(row['context'])['budget_exits'] is True
    woken = events(conn, task, 'nfos_principal_requested')
    nfos_runtime.reconcile_runtime(conn, lab_sweep=False)
    assert d.open_budget_obligations(conn, task.id) == obligations and events(conn, task, 'nfos_principal_requested') == woken
    assert events(conn, task, 'nfos_budget_hold_restored') == 1
    now = int(time.time())
    with kb.write_txn(conn):  # an hour later, three reminders unanswered
        context = json.loads(row['context'])
        context['reminders'] = [now - 4000, now - 3000, now - 2000]
        conn.execute('UPDATE nfos_decisions SET created_at=?,context=? WHERE id=?', (now - 5000, json.dumps(context), row['id']))
    nfos_runtime.reconcile_runtime(conn, lab_sweep=False)
    assert events(conn, task, 'nfos_principal_requested') == woken + 1 and events(conn, task, 'nfos_decision_stalled') == 0


def test_review_opened_before_this_rule_gets_the_exits_in_its_text_once(overrun):
    conn, task, _, _ = overrun
    decision_id = legacy_timeout(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='pending',action=NULL,author=NULL,answer=NULL,resolved_at=NULL WHERE id=?", (decision_id,))
    woken = events(conn, task, 'nfos_principal_requested')
    d.reconcile_human_answers(conn, lab_sweep=False)
    row = d.get_decision(conn, decision_id)
    assert row['status'] == 'pending' and 'reclassify-budget' in row['question'] and 'Verifique a autorização existente' in row['question']
    assert json.loads(row['context'])['budget_exits'] is True
    assert events(conn, task, 'nfos_principal_requested') == woken + 1
    d.reconcile_human_answers(conn, lab_sweep=False)
    assert d.get_decision(conn, decision_id)['question'] == row['question'] and events(conn, task, 'nfos_principal_requested') == woken + 1
    assert d.open_budget_obligations(conn, task.id) == [decision_id]  # and the pause opens no second one


def test_owner_question_already_returned_by_the_general_rule_still_gets_the_exits(overrun):
    """When the universal owner-question rule runs first, its successor carries the pause but not the exits."""
    conn, task, _, _ = overrun
    old = pause_decision(conn, task)
    with kb.write_txn(conn):
        context = json.loads(d.get_decision(conn, old)['context'])
        context.pop('budget_exits')
        conn.execute("UPDATE nfos_decisions SET status='superseded' WHERE id=?", (old,))
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     ('dec_general_rule', task.id, task.current_run_id, 'impediment', 'Revisão de autonomia: ' + d.OWNER_QUESTION_REFUSAL,
                      json.dumps({'supersedes': old, 'no_owner_questions': True, 'maintenance_recovery': context['maintenance_recovery']}),
                      1, int(time.time())))
    assert ('dec_general_rule', 'exits') in d.sweep_budget_exits(conn)
    row = d.get_decision(conn, 'dec_general_rule')
    assert 'partial-closure-budget' in row['question'] and f'para a pausa {task.current_run_id}' in row['question']
    assert d.sweep_budget_exits(conn) == [] and d.open_budget_obligations(conn, task.id) == ['dec_general_rule']


def test_dependency_wait_on_a_budget_obligation_gets_the_text_and_no_wake(overrun):
    conn, task, _, _ = overrun
    decision_id = pause_decision(conn, task)
    with kb.write_txn(conn):
        context = json.loads(d.get_decision(conn, decision_id)['context'])
        context.pop('budget_exits')
        context['lab_wait'] = {'kind': 'dependency', 'hold': True, 'until': int(time.time()) + 3600}
        conn.execute("UPDATE nfos_decisions SET question='Execute o reparo desta pausa',context=? WHERE id=?", (json.dumps(context), decision_id))
    woken = events(conn, task, 'nfos_principal_requested')
    assert (decision_id, 'exits') in d.sweep_budget_exits(conn)
    assert 'reclassify-budget' in d.get_decision(conn, decision_id)['question'] and events(conn, task, 'nfos_principal_requested') == woken


def test_budget_question_to_the_owner_goes_through_the_whole_tick_as_a_budget_obligation(overrun):
    """Order inside the tick: the budget sweep runs before the general review of owner questions."""
    conn, task, _, _ = overrun
    old = pause_decision(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=? WHERE id=?",
                     ('PERGUNTA para Maikol: Autoriza mais 2700 segundos?', old))
    d.reconcile_human_answers(conn, lab_sweep=False)
    successors = [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='pending'", (task.id,))]
    assert len(successors) == 1
    context = json.loads(successors[0]['context'])
    assert context['supersedes'] == old and context['budget_exits'] is True and 'no_owner_questions' not in context
    assert 'reclassify-budget' in successors[0]['question'] and kb.get_task(conn, task.id).status == 'blocked'


def test_human_question_that_carries_only_the_balance_review_returns_and_the_repair_closes_it(overrun):
    conn, task, identity, args = overrun
    old = budget_decision(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer=? WHERE id=?",
                     ('PERGUNTA para Maikol: Autoriza mais orçamento?', old))
    moved = d.sweep_budget_exits(conn)
    new = [pair[1] for pair in moved if pair[0] == old][0]
    assert json.loads(d.get_decision(conn, new)['context'])['execution_budget_identity']
    d.resolve_decision(conn, new, action='continue', answer='Sem saldo ainda.', author='Principal')
    assert d.get_decision(conn, new)['status'] == 'pending'
    review.reclassify_budget(conn, task.id, **identity)
    repair.repair_execution(conn, task.id, **args, apply=True)
    assert d.get_decision(conn, new)['status'] == 'resolved' and d.open_budget_obligations(conn, task.id) == []


def test_claim_refused_again_after_a_review_closed_with_the_balance_unchanged_asks_once_more_not_every_tick(overrun):
    conn, task, _, _ = overrun
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (task.current_run_id,))
    old = budget_decision(conn, task)
    with kb.write_txn(conn):
        conn.execute("UPDATE nfos_decisions SET status='resolved',action='changes',author='Principal',answer='Mantida.',resolved_at=1100 "
                     "WHERE id=?", (old,))
        conn.execute("UPDATE tasks SET status='ready',block_kind=NULL WHERE id=?", (task.id,))
    for _ in range(3):
        assert kb.claim_task(conn, task.id) is None
        d.sweep_awaiting_principal(conn)
    obligations = d.open_budget_obligations(conn, task.id)
    assert len(obligations) == 1 and obligations[0] != old
    assert kb.get_task(conn, task.id).status == 'blocked' and events(conn, task, 'nfos_execution_budget_exhausted') == 1


def test_review_of_an_earlier_instruction_does_not_hold_the_card_after_the_repair(overrun):
    conn, task, identity, args = overrun
    stale = budget_decision(conn, task)
    with kb.write_txn(conn):
        context = json.loads(d.get_decision(conn, stale)['context'])
        context['execution_budget_identity']['spec_revision'] -= 1
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (json.dumps(context), stale))
    review.reclassify_budget(conn, task.id, **identity)
    assert repair.repair_execution(conn, task.id, **args, apply=True)['status'] == 'ready'
    assert d.get_decision(conn, stale)['status'] == 'resolved' and open_decisions(conn, task) == 0


@pytest.mark.parametrize('waiting', ['answered', 'requester'])
def test_runtime_does_not_throw_away_an_answer_on_its_way_nor_a_question_to_the_requester(overrun, waiting):
    conn, task, identity, _ = overrun
    old = pause_decision(conn, task)
    review.partial_closure_budget(conn, task.id, **identity)
    spend(conn, task, 10000)
    assert review.budget_exits(conn, task.id)['terminal']
    with kb.write_txn(conn):
        context = json.loads(d.get_decision(conn, old)['context'])
        if waiting == 'answered':
            context['human_reply'] = {'answer': 'Autorizo', 'author': 'Maikol'}
        else:
            context['public_message'] = {'kind': 'question', 'text': 'Pode esperar mais um dia?', 'to': 'Ana'}
        conn.execute("UPDATE nfos_decisions SET status='human',action='human',author='Principal',answer='PERGUNTA',context=? WHERE id=?",
                     (json.dumps(context), old))
    assert d.sweep_budget_exits(conn) == [] and d.get_decision(conn, old)['status'] == 'human'
    assert events(conn, task, 'nfos_budget_terminal') == 0


def test_goal_turns_have_no_exit_so_the_runtime_moves_the_request_and_retains_the_card(overrun):
    conn, task, identity, _ = overrun
    first = review.worker_escalation(conn, task.id)['first_run_id']
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET goal_max_turns=3 WHERE id=?', (task.id,))
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                     (json.dumps({'worker_session_id': 'retained-session', 'escalation_usage': {'iterations': 400, 'turns': 3}}), first))
    exits = review.budget_exits(conn, task.id)
    assert 'goal_turns' in exits['exhausted_by'] and exits['reclassify'] is None and exits['partial_closure'] is None and exits['terminal']
    text = review.budget_exits_text(exits)
    assert 'Turnos do objetivo não têm concessão' in text and 'continua pelo grant-budget' not in text
    with pytest.raises(d.WorkflowError, match='not available'):
        review.partial_closure_budget(conn, task.id, **identity)
    assert d.sweep_budget_exits(conn) == [(task.id, 'terminal')]
    children = d.continuation_links(conn, task.id)['children']
    assert len(children) == 1 and kb.get_task(conn, children[0]).status not in ('done', 'archived', 'blocked')
    assert children[0] in d.get_workflow(conn, task.id)['next_action']
    assert d.sweep_budget_exits(conn) == [] and d.continuation_links(conn, task.id)['children'] == children


def test_idle_small_card_with_a_balance_is_not_offered_the_closing_exit(overrun):
    conn, task, identity, _ = overrun
    first = review.worker_escalation(conn, task.id)['first_run_id']
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET max_runtime_seconds=2700 WHERE id=?', (task.id,))
        conn.execute('UPDATE task_runs SET started_at=100,ended_at=200,metadata=? WHERE id=?',
                     (json.dumps({'worker_session_id': 'retained-session', 'escalation_usage': {'iterations': 10}}), first))
    exits = review.budget_exits(conn, task.id)
    assert (exits['exhausted'], exits['remaining_runtime_seconds'], exits['partial_closure']) == (False, 2600, None)
    with pytest.raises(d.WorkflowError, match='partial-closure-budget is not available'):
        review.partial_closure_budget(conn, task.id, **identity)
    assert d.continuation_links(conn, task.id)['children'] == []


def test_card_with_no_time_limit_and_no_spec_still_reads_its_exits(overrun, monkeypatch):
    conn, task, _, _ = overrun
    monkeypatch.setattr(d, 'get_spec', lambda conn, task_id: None)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET max_runtime_seconds=NULL WHERE id=?', (task.id,))
    exits = review.budget_exits(conn, task.id)
    assert exits['exhausted_by'] == ['iterations'] and exits['remaining_runtime_seconds'] is None and exits['reclassify'] is None
    assert exits['partial_closure'] == dict(runtime_seconds=0, iterations=60, continuation=None)
    assert 'partial-closure-budget' in review.budget_exits_text(exits)


def test_continuation_of_a_card_held_for_another_reason_is_not_born_blocked(overrun):
    conn, task, identity, _ = overrun
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (task.id,))
        kb._append_event(conn, task.id, 'blocked', {'reason': 'Qual servidor?', 'kind': 'needs_input'})
    receipt = review.partial_closure_budget(conn, task.id, **identity)
    assert kb.get_task(conn, receipt['continuation']).status != 'blocked'
    assert kb.get_task(conn, task.id).block_kind == 'awaiting_principal'


def test_standing_instructions_of_the_principal_name_the_exits():
    from pathlib import Path
    from hermes_cli import nfos_runtime
    watcher = (Path(nfos_runtime.__file__).resolve().parents[1] / 'gateway' / 'kanban_watchers.py').read_text(encoding='utf-8')
    assert '`budget-exits --task ID`' in watcher and 'exige autorização existente para a concessão limitada' not in watcher
    source = Path(nfos_runtime.__file__).read_text(encoding='utf-8')
    assert 'budget is never a human question: read `budget-exits`' in source
