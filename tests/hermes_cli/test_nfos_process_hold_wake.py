"""PROCESS_HOLD_WAKE_20261016: a card held in a ``decision`` step of the Principal's lane wakes the Principal once.

The hold (PROCESS_HOLD_20261014) was silent: nothing told the Principal that the card waited for him, and an expired hold
could not ask at all. The process engine is answered by ``nfos_process.position``; the step below is synthetic, with no
project in it: any ``decision`` step in the Principal's lane behaves the same, any ``wait`` step never asks.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_waits
from tests.hermes_cli.test_nfos_process_hold import (  # noqa: F401 - fixtures and helpers of the hold's own tests
    BLOQUEIO, P6, _answered_card, _claim, _waits, board, hold, owner_questions_allowed, process)

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')

CHOOSE = {'mode': 'enforce', 'governed': True, 'current': 'choose', 'current_name': 'Choose the route',
          'current_kind': 'decision', 'current_lane': 'principal',
          'next': [{'step': 'build', 'name': 'Build', 'when': None}, {'step': 'drop', 'name': 'Drop', 'when': None}]}
BUILD = dict(CHOOSE, current='build', current_name='Build', current_kind='task', current_lane='workers')


def _requests(conn, task_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM nfos_decisions WHERE task_id=? AND json_extract(context,'$.process_hold') IS NOT NULL", (task_id,))]


def _asked(conn, task_id):
    return conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_principal_requested' "
                        "AND json_extract(payload,'$.retained_card')=1", (task_id,)).fetchone()[0]


def test_a_held_decision_step_opens_one_request_after_the_grace(board, process, hold, monkeypatch):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        assert _claim(conn, task.id) == (None, ['process_hold'])
        [wait] = _waits(conn, task.id)
        assert wait['next_check_at'] - wait['created_at'] == hold.GRACE_SECONDS
        assert _requests(conn, task.id) == []

        assert nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS - 1) == []
        assert _requests(conn, task.id) == []
        assert nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS) == [(wait['id'], 'wait')]

        [request] = _requests(conn, task.id)
        assert request['kind'] == 'impediment' and request['status'] == 'pending'
        context = json.loads(request['context'])['process_hold']
        assert context['step'] == 'choose' and context['wait_id'] == wait['id']
        assert context['exits'] == ['build (Build)', 'drop (Drop)']
        assert 'Choose the route' in request['question'] and _asked(conn, task.id) == 1

        # Later checks of the same hold ask nothing more, and the open request keeps the dispatcher off the card.
        assert nfos_waits.sweep(conn, now=wait['created_at'] + 1000) == [(wait['id'], 'wait')]
        assert len(_requests(conn, task.id)) == 1 and _asked(conn, task.id) == 1
        assert kb._nfos_decision_open(conn, task.id)
        delivery.nudge_open_decisions(conn, task.id)  # the reminder path the dispatcher runs; the step is still held
        assert [r['status'] for r in _requests(conn, task.id)] == ['pending']


def test_the_request_carries_the_goal_and_the_exit_the_project_wrote_for_the_step(board, process, hold, monkeypatch):
    process['position'] = dict(CHOOSE, current_goal='Pick the short lane   when the fix fits a typed operation.',
                               current_exit='Record the route and the cut in one entry.')
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        assert _claim(conn, task.id) == (None, ['process_hold'])
        [wait] = _waits(conn, task.id)
        nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS)
        [request] = _requests(conn, task.id)
        assert 'Guia desta etapa no processo do projeto.' in request['question']
        assert 'Meta: Pick the short lane when the fix fits a typed operation.' in request['question']  # espaços repetidos viram um
        assert 'Saída: Record the route and the cut in one entry.' in request['question']
        assert json.loads(request['context'])['process_hold']['exits'] == ['build (Build)', 'drop (Drop)']


def test_the_guide_is_cut_and_absent_without_text(hold):
    long = 'x' * 4000
    text = hold.guide({'current_goal': long, 'current_exit': None})
    assert text.count('x') == 2500 and text.endswith('...') and 'Saída' not in text
    assert hold.guide({}) == '' and hold.guide(None) == '' and hold.guide({'current_goal': '   '}) == ''


def test_leaving_the_step_closes_the_request_as_the_runtime_and_releases_the_card(board, process, hold, monkeypatch):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS)
        [request] = _requests(conn, task.id)

        process['position'] = BUILD
        assert nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS + hold.RECHECK_SECONDS) == [
            (wait['id'], 'satisfied')]
        [closed] = _requests(conn, task.id)
        context = json.loads(closed['context'])
        assert closed['id'] == request['id'] and closed['status'] == 'resolved' and closed['action'] == 'continue'
        assert context['wait_rule']['resolved_by'] == 'runtime' and 'decided_in_session' not in context
        claimed, refusal = _claim(conn, task.id)
        assert claimed is not None and refusal == []


def test_answering_before_registering_the_step_is_refused(board, process, hold, monkeypatch):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS)
        [request] = _requests(conn, task.id)

        for action in ('continue', 'changes'):
            with pytest.raises(delivery.WorkflowError, match='register the next step first'):
                delivery.resolve_decision(conn, request['id'], action=action, answer='go on', author='Principal')
        assert _requests(conn, task.id)[0]['status'] == 'pending'

        process['position'] = BUILD  # the Principal registered the step; his own answer now passes
        delivery.resolve_decision(conn, request['id'], action='continue', answer='built', author='Principal')
        assert _requests(conn, task.id)[0]['status'] == 'resolved'


def test_a_wait_step_never_wakes_the_principal(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:  # BLOQUEIO is a wait step of the Principal's lane
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        assert wait['next_check_at'] - wait['created_at'] == hold.RECHECK_SECONDS
        nfos_waits.sweep(conn, now=wait['created_at'] + hold.RECHECK_SECONDS)
        nfos_waits.sweep(conn, now=wait['created_at'] + 2 * hold.RECHECK_SECONDS)
        assert _requests(conn, task.id) == [] and _asked(conn, task.id) == 0


def test_a_hold_declared_before_the_wake_gets_its_request_on_the_next_sweep(board, process, hold, monkeypatch):
    """The holds already open when this ships: attempts spent, next check long due, no request ever made."""
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        with kb.write_txn(conn):
            conn.execute('UPDATE nfos_waits SET attempts=223,next_check_at=created_at WHERE id=?', (wait['id'],))
        nfos_waits.sweep(conn, now=wait['created_at'] + 3600)
        nfos_waits.sweep(conn, now=wait['created_at'] + 7200)
        assert len(_requests(conn, task.id)) == 1 and _asked(conn, task.id) == 1


def test_an_expired_hold_gets_its_request_on_the_next_claim_and_only_one(board, process, hold, monkeypatch):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        with kb.write_txn(conn):  # as the three real holds: expired with no request ever made
            conn.execute('UPDATE nfos_waits SET next_check_at=deadline_at WHERE id=?', (wait['id'],))
        assert nfos_waits.sweep(conn, now=wait['deadline_at']) == [(wait['id'], 'expired')]
        assert nfos_waits.get(conn, wait['id'])['outcome'] == 'still_held' and _requests(conn, task.id) == []

        assert _claim(conn, task.id) == (None, ['process_hold'])
        assert len(_requests(conn, task.id)) == 1 and _asked(conn, task.id) == 1
        assert _claim(conn, task.id) == (None, ['process_hold'])
        assert len(_requests(conn, task.id)) == 1 and kb._nfos_decision_open(conn, task.id)

        # No open wait is left to close the request: the reminder path closes it once the card leaves the step.
        process['position'] = BUILD
        delivery.nudge_open_decisions(conn, task.id)
        [closed] = _requests(conn, task.id)
        assert closed['status'] == 'resolved' and json.loads(closed['context'])['wait_rule']['resolved_by'] == 'runtime'
        assert _claim(conn, task.id)[0] is not None


@pytest.mark.parametrize('parent_status', ['done', 'archived'])
def test_an_open_parent_defers_the_request_until_it_closes(board, process, hold, monkeypatch, parent_status):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        parent = kb.create_task(conn, title='cause', assignee='default')
        with kb.write_txn(conn):  # a raw link: link_tasks would demote the card to todo, which is not the case under test
            conn.execute('INSERT INTO task_links(parent_id,child_id) VALUES(?,?)', (parent, task.id))
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        nfos_waits.sweep(conn, now=wait['created_at'] + hold.GRACE_SECONDS)
        assert _requests(conn, task.id) == [] and _asked(conn, task.id) == 0

        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (parent_status, parent))
        nfos_waits.sweep(conn, now=wait['created_at'] + 2 * hold.RECHECK_SECONDS)
        assert len(_requests(conn, task.id)) == 1


def test_an_expired_hold_with_an_open_parent_asks_nothing(board, process, hold, monkeypatch):
    process['position'] = CHOOSE
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        parent = kb.create_task(conn, title='cause', assignee='default')
        with kb.write_txn(conn):
            conn.execute('INSERT INTO task_links(parent_id,child_id) VALUES(?,?)', (parent, task.id))
        _claim(conn, task.id)
        [wait] = _waits(conn, task.id)
        with kb.write_txn(conn):
            conn.execute('UPDATE nfos_waits SET next_check_at=deadline_at WHERE id=?', (wait['id'],))
        nfos_waits.sweep(conn, now=wait['deadline_at'])
        _claim(conn, task.id)
        assert _requests(conn, task.id) == []


def test_nothing_is_asked_when_the_process_is_off_or_unreadable(board, process, hold, monkeypatch):
    with kb.connect_closing() as conn:
        task = _answered_card(conn, monkeypatch)
        for position in (BUILD, RuntimeError('store locked'), None, dict(CHOOSE, mode='shadow'), dict(CHOOSE, governed=False)):
            process['position'] = position
            assert hold.claim_hold(conn, kb.get_task(conn, task.id)) is None
            delivery.nudge_open_decisions(conn, task.id)
        assert _requests(conn, task.id) == [] and _waits(conn, task.id) == []
