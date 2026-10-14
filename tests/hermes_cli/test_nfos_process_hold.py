"""PROCESS_HOLD_20261014: a card in a decision/wait step of its project process is not claimed by a worker.

Case t_45ae522c (concursa-ai, 14/10/2026): the Principal moved the card to "bloqueio" (kind wait, Principal lane) and told
the worker to close its segment. For the Kanban the card stayed running/ready; the timed-out run was continued and each
CHANGES answer relaunched the worker, which could not leave the step and asked again. Real cards in a temporary
HERMES_HOME; the process engine (project_workflow, outside this repository) is reached through ``nfos_process.position``,
which these tests answer, except the last one, which uses the real module when PROJECT_WORKFLOW_SRC points to it.
"""
import os
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_process
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_waits
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')

BLOQUEIO = {'mode': 'enforce', 'governed': True, 'current': 'bloqueio', 'current_name': 'Bloqueio (Principal)',
            'current_kind': 'wait', 'current_lane': 'principal'}
P6 = dict(BLOQUEIO, current='p6', current_name='6 · Validar', current_kind='task', current_lane='workers')
QUESTION = 'Reconciliação documental concluída: falta cobertura integral. Próxima decisão do Principal.'
CHANGES = ('CHANGES: mantenho NOT_VERIFIED. Registrei p7->bloqueio no processo nativo. Finalize o segmento com este '
           'checkpoint; a próxima transição p6 pertence ao Principal.')


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_SESSION_ID', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False})
    # The card's worker pid is this test process, which never exits: the previous-run termination is not under test.
    from hermes_cli import nfos_runtime
    monkeypatch.setattr(nfos_runtime, 'previous_runs_termination_pending', lambda conn, task_id: False)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return home


@pytest.fixture
def hold():
    from hermes_cli import nfos_process_hold
    return nfos_process_hold


@pytest.fixture
def process(monkeypatch):
    """Where the process engine says the card is: the position the tests set, read on every call."""
    state = {'position': BLOQUEIO, 'reads': 0}

    def position(conn, task_id):
        state['reads'] += 1
        if isinstance(state['position'], Exception):
            raise state['position']
        return state['position']

    monkeypatch.setattr(nfos_process, 'position', position)
    return state


def _answered_card(conn, monkeypatch):
    """The relaunch door: the worker asked, left cleanly with its question open, and the Principal answered CHANGES."""
    rid = delivery.receive_request(
        conn, source={'platform': 'telegram', 'chat_id': '-10001', 'thread_id': '41', 'message_id': '17'},
        text='Corrigir a causa do erro no envio do edital PMMG.',
        project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    task = delivery.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
    run_id = task.current_run_id
    kb.block_task(conn, task.id, reason=QUESTION, expected_run_id=run_id)
    decision = kb._nfos_pending_decision(conn, task.id, run_id)
    with monkeypatch.context() as gone:
        gone.setattr(kb, '_pid_alive', lambda pid: False)
        gone.setattr(kb, '_classify_worker_exit', lambda pid: ('clean_exit', 0))
        gone.setattr(kb, '_resolve_crash_grace_seconds', lambda: 0)
        assert kb.detect_crashed_workers(conn) == []
    delivery.resolve_decision(conn, decision, action='changes', answer=CHANGES, author='Principal')
    task = kb.get_task(conn, task.id)
    assert task.status == 'ready' and not kb._nfos_decision_open(conn, task.id)
    return task


def _claim(conn, task_id):
    refusal = []
    return kb.claim_task(conn, task_id, refusal=refusal), refusal


def _waits(conn, task_id):
    if not nfos_waits._table(conn):
        return []
    return [nfos_waits.get(conn, row[0]) for row in conn.execute(
        "SELECT id FROM nfos_waits WHERE task_id=? AND reason='process_hold' ORDER BY created_at,id", (task_id,))]


def test_the_relaunch_after_changes_is_held_while_the_card_is_in_a_principal_step(board, process, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)

        claimed, refusal = _claim(conn, task.id)

        assert claimed is None and refusal == ['process_hold']
        assert kb.get_task(conn, task.id).status == 'ready'
        [wait] = _waits(conn, task.id)
        assert wait['status'] == 'open'
        assert wait['predicate'] == {'kind': 'process_step', 'step': 'bloqueio'}
        assert wait['executor']['kind'] == 'principal' and 'Bloqueio (Principal)' in wait['executor']['name']
        assert wait['deadline_at'] - wait['created_at'] == nfos_waits.policy('process_hold')['deadline']
        events = [row[0] for row in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,))]
        assert 'nfos_wait_declared' in events

        # The open wait holds the claim by itself: the step is not read again on every tick.
        reads = process['reads']
        assert _claim(conn, task.id) == (None, ['process_hold'])
        assert process['reads'] == reads
        assert len(_waits(conn, task.id)) == 1


def test_the_wait_ends_when_the_principal_registers_the_next_step(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        start = wait['created_at']

        assert nfos_waits.sweep(conn, now=start + 1) == []  # not due yet
        # Still in the step: a false check, one attempt, the next one scheduled (the step has no plan hook).
        assert nfos_waits.sweep(conn, now=start + hold.RECHECK_SECONDS) == [(wait['id'], 'wait')]
        stepped = nfos_waits.get(conn, wait['id'])
        assert stepped['status'] == 'open' and stepped['attempts'] == 1
        assert stepped['next_check_at'] == start + 2 * hold.RECHECK_SECONDS
        assert stepped['evidence']['process_step'] == 'bloqueio'

        process['position'] = P6
        assert nfos_waits.sweep(conn, now=start + 2 * hold.RECHECK_SECONDS) == [(wait['id'], 'satisfied')]
        assert nfos_waits.get(conn, wait['id'])['status'] == 'satisfied'

        claimed, refusal = _claim(conn, task.id)
        assert claimed is not None and claimed.status == 'running' and refusal == []


def test_at_the_deadline_the_wait_records_still_held_and_the_step_keeps_holding(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)

        assert nfos_waits.sweep(conn, now=wait['deadline_at']) == [(wait['id'], 'expired')]
        expired = nfos_waits.get(conn, wait['id'])
        assert expired['status'] == 'expired' and expired['outcome'] == 'still_held'
        assert expired['evidence']['expired_because'] == 'deadline'

        # The expired wait is not declared again in this round, and no worker is released into the Principal's step.
        assert _claim(conn, task.id) == (None, ['process_hold'])
        assert [w['status'] for w in _waits(conn, task.id)] == ['expired']
        assert kb.get_task(conn, task.id).status == 'ready'


def test_a_task_step_an_unread_process_or_a_motor_out_of_enforce_does_not_hold(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        for position in (P6, RuntimeError('store locked'), {'mode': 'enforce', 'error': 'OperationalError: locked'},
                         dict(BLOQUEIO, mode='shadow'), dict(BLOQUEIO, governed=False), None):
            process['position'] = position
            assert hold.claim_hold(conn, kb.get_task(conn, task.id)) is None, position
        assert _waits(conn, task.id) == []


def test_turning_the_motor_off_withdraws_the_wait(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)

        process['position'] = None  # nfos_process.position: motor off on the board
        assert nfos_waits.sweep(conn, now=wait['created_at'] + hold.RECHECK_SECONDS) == [(wait['id'], 'withdrawn')]
        assert nfos_waits.get(conn, wait['id'])['outcome'] == 'process_off'
        claimed, _ = _claim(conn, task.id)
        assert claimed is not None


def test_a_card_outside_the_nfos_workflow_is_claimed_without_reading_the_process(board, process):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='plain kanban card', assignee='default')
        claimed, refusal = _claim(conn, tid)
        assert claimed is not None and refusal == []
        assert process['reads'] == 0


def test_a_timed_out_run_is_not_continued_on_the_goal_loops_own_question(board, process, monkeypatch):
    """The loop's finalize question is not the worker's progress; a real progress mark still is."""
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(
            conn, source={'platform': 'telegram', 'chat_id': '-10001', 'thread_id': '41', 'message_id': '18'},
            text='Corrigir a extração do edital.', project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'},
            attachments=[])
        request = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
        loop_question = [None]

        def block(reason):
            loop_question[0] = reason
            kb.block_task(conn, task.id, reason=reason, expected_run_id=task.current_run_id)

        monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('done', 'segment closed', False, None, False))
        goals.run_kanban_goal_loop(task_id=task.id, goal_text='corrigir', run_turn=lambda prompt: 'retido',
                                   task_status_fn=lambda: 'running', block_fn=block, max_turns=2,
                                   pending_decision_fn=lambda: None)
        assert loop_question[0] and loop_question[0].startswith("Goal-mode worker's output looked complete")
        kinds = [row[0] for row in conn.execute("SELECT kind FROM task_events WHERE task_id=? AND run_id=?",
                                                (task.id, task.current_run_id))]
        assert 'nfos_principal_requested' in kinds

        assert kb._budget_continuation(conn, task.id, 'elapsed 7250s > limit 7200s', outcome='timed_out',
                                       release_claim=True, end_run=True) is False

        with kb.write_txn(conn):
            kb._append_event(conn, task.id, 'nfos_progress', {'stage': 'implement'}, run_id=task.current_run_id)
        assert kb._budget_continuation(conn, task.id, 'elapsed 7250s > limit 7200s', outcome='timed_out',
                                       release_claim=True, end_run=True) is True


@pytest.mark.parametrize('verdict', ['done', 'continue'])
def test_every_question_the_goal_loop_files_is_one_of_its_own(monkeypatch, verdict):
    # The finalize block (judge done) and the turn-budget block (judge continue) are what the progress count leaves out.
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: (verdict, 'scripted', False, None, False))
    reasons = []
    goals.run_kanban_goal_loop(task_id='t', goal_text='g', run_turn=lambda prompt: 'working',
                               task_status_fn=lambda: 'running', block_fn=reasons.append, max_turns=2)
    assert reasons and any(reasons[0].startswith(own) for own in kb._GOAL_LOOP_QUESTIONS), reasons


MODULE_SRC = os.environ.get('PROJECT_WORKFLOW_SRC', '')
needs_module = pytest.mark.skipif(
    not (MODULE_SRC and (Path(MODULE_SRC) / 'project_workflow' / 'enforce.py').is_file()),
    reason='precisa de PROJECT_WORKFLOW_SRC apontando para o módulo project_workflow')


@needs_module
def test_the_real_process_names_the_kind_and_lane_of_the_current_step(tmp_path, monkeypatch, hold):
    """With the real engine: the position carries the kind and the lane the hold reads, and they match the spec."""
    from tests.hermes_cli import test_nfos_process as motor

    home = tmp_path / 'home'
    db = home / 'kanban' / 'boards' / 'concursa-ai' / 'kanban.db'
    db.parent.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    pw = tmp_path / 'pw' / 'project_workflow.db'
    pw.parent.mkdir()
    monkeypatch.setenv('PROJECT_WORKFLOW_DB', str(pw))
    motor.policy(pw, 'enforce')
    motor.seed(pw)
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(conn, source={'platform': 'fixture', 'chat_id': 'local', 'thread_id': 't',
                                                     'message_id': '1'}, text='Corrigir o cargo',
                                       project={'board': 'concursa-ai', 'profile': 'default', 'delivery_type': 'code',
                                                'repo_path': str(tmp_path)})
        claim = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, claim['claim_token'], pid=os.getpid())
        position = nfos_process.position(conn, task.id)
        assert position and not position.get('error'), position
        assert isinstance(position['current_kind'], str) and 'current_lane' in position, position
        assert (hold.held_step(conn, task.id) is None) == (position['current_kind'] not in hold.DECIDER_KINDS)
        from project_workflow import enforce
        assert tuple(hold.DECIDER_KINDS) == tuple(enforce.DECIDER_KINDS)
