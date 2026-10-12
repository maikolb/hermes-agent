"""ROUTE_WALL_20261012: a provider route closed by a long usage window.

Read on the production profile on 2026-10-12 (read-only): opencode-go refused 2,046 of 2,046 calls from the 8th to the
11th with `metadata.limitName: weekly` and no reset time in the body; Codex refuses with `usage_limit_reached` and
the reset time of the window. After the executor learned to leave at the wall with exit code 75, a launch onto the
weekly wall still slept through two Retry-After waits capped at 600 s (about twenty minutes holding a slot) and the
respawn guard launched the card again on the same wall, five minutes later and then doubling up to one hour.

What these tests hold, with the real dispatcher tick, a real board, real child processes and the real quiet
single-query path of the worker (only the provider call, the goal judge and the clock are stand-ins):

* the worker leaves on the first refusal of a usage window, with no wait inside the turn;
* the route (provider and model) is closed for the whole host, with the name of the limit and the reset time when
  the provider gives one, and one notice goes out for it, through the event of a model switch;
* while it is closed no card of that route is launched: the cards hold no slot, count no failure and stand in the
  wait registry; a card of another route is launched as always;
* the route reopens at the reset time the provider gave, or by one probe at spaced intervals when it gave none,
  and a probe the provider refuses does not send a second notice;
* a throttle that resets inside the client's own wait is retried as before;
* no other provider or model is used in place of a closed route.
"""
import json
import os
import time
from email.utils import formatdate
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, nfos_route_walls as walls, nfos_waits
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    EXECUTOR_MODEL, EXECUTOR_PROVIDER, Clock, CodexUsageLimit, GoUsageLimit, GoUsageLimitFromSDK, GoUsageLimitNoReset,
    ProviderRateLimit, ShortThrottle, assert_executor_route, enrol, launcher, metered_reply, option, quota_board,
    run_executor,
)

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

ROUTE = (EXECUTOR_PROVIDER, EXECUTOR_MODEL)
HELD = f'route_closed:{EXECUTOR_PROVIDER}/{EXECUTOR_MODEL}'
DAY = 86400
CONTINUE = json.dumps({'verdict': 'continue', 'reason': 'the total on the screen is not corrected yet'})
SECOND_REQUEST = '[Japa|1]\nCorrija a ordenação da tela de pedidos.'


def refusing(wall):
    def answer(seen):
        raise wall()
    return answer


def answered_once(seen):
    """The provider answers the first turn; the turn after it meets a plain throttle, so the worker leaves."""
    if seen.turns == 1:
        return metered_reply('The total is read from the wrong column; correcting it next.')
    raise ProviderRateLimit()


def run_card(conn, launcher, monkeypatch, task_id, answer, judge_reply=None):
    """The worker of this card's latest launch, run in this process, leaving with the code it chose."""
    argv, env = next(launch for launch in reversed(launcher.launches) if launch[1]['HERMES_KANBAN_TASK'] == task_id)
    run = run_executor(monkeypatch, argv, env, answer=answer, judge_reply=judge_reply)
    launcher.exit(kb.get_task(conn, task_id).worker_pid, run.exit_code)
    return run


def two_cards(conn, launcher):
    """Two NFOS cards of the executor route, both launched while the route was open."""
    first = enrol(conn, launcher)
    second = enrol(conn, launcher, text=SECOND_REQUEST, message_id='124')
    return first.id, second.id


def notices(conn):
    return [(row['task_id'], json.loads(row['payload'])['message']) for row in conn.execute(
        "SELECT task_id,payload FROM task_events WHERE kind='model_fallback' ORDER BY id")]


def event_kinds(conn):
    return [row['kind'] for row in conn.execute("SELECT kind FROM task_events WHERE kind!='heartbeat' ORDER BY id")]


def quota_waits(conn, task_id):
    return [wait for wait in nfos_waits.card_waits(conn, task_id, closed=20) if wait['reason'] == 'provider_quota']


def spawned(tick):
    return [row[0] for row in tick.spawned]


def assert_waiting(conn, tick, task_ids):
    """Held by the closed route: not launched, no slot, no failure, and declared in the wait registry."""
    assert not tick.spawned and not tick.respawn_guarded and not tick.crashed and not tick.auto_blocked, tick
    assert sorted(tick.claim_held) == sorted((task_id, HELD) for task_id in task_ids), tick
    assert kb.count_running_tasks(conn) == 0, 'a waiting card holds no slot'
    for task_id in task_ids:
        card = kb.get_task(conn, task_id)
        assert card.status == 'ready' and card.consecutive_failures == 0 and card.claim_lock is None
        (wait,) = [wait for wait in quota_waits(conn, task_id) if wait['status'] == 'open']
        assert wait['executor'] == {'kind': 'external', 'name': f'{EXECUTOR_PROVIDER}/{EXECUTOR_MODEL}'}
        assert wait['predicate'] == {'kind': 'route_open', 'provider': EXECUTOR_PROVIDER, 'model': EXECUTOR_MODEL}


def close_route_with_two_cards(conn, launcher, monkeypatch, wall):
    """Both cards running, the first meets `wall`, then the second (launched before the route closed) meets it."""
    first, second = two_cards(conn, launcher)
    run = run_card(conn, launcher, monkeypatch, first, refusing(wall))
    late = run_card(conn, launcher, monkeypatch, second, refusing(wall))
    return first, second, run, late


def test_worker_on_a_weekly_window_leaves_on_the_first_refusal_and_the_route_closes_with_one_notice(
        quota_board, launcher, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, run, late = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        argv, env = launcher.launches[0]
        assert json.loads(env[walls.ROUTE_ENV]) == list(ROUTE), 'the launch names its route for the worker'

        # (1) no repetition and no wait inside the turn
        assert [(r['failed'], r['failure_reason']) for r in run.results] == [(True, 'rate_limit')]
        assert (run.turns, run.provider_calls, run.judge_calls, run.waits.waited) == (1, 1, 0, 0)
        assert run.exit_code == late.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75
        assert (late.turns, late.provider_calls, late.waits.waited) == (1, 1, 0)

        # (2) the route is closed with the limit's name and the reset time the provider gave
        wall = walls.read(*ROUTE)
        assert (wall['provider'], wall['model'], wall['limit']) == (*ROUTE, 'weekly')
        assert wall['reset_at'] == clock.now + DAY and wall['closed_by']['task_id'] == first
        assert wall['probes'] == 0 and wall['probe'] is None
        assert [w['model'] for w in walls.walls()] == [EXECUTOR_MODEL]

        # (5) one notice for the wall, on the card that met it first, through the model-switch event
        (noticed_on, message), = notices(conn)
        assert noticed_on == first
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(clock.now + DAY))
        assert f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada' in message and 'limite weekly' in message
        assert when in message and 'esperam sem ocupar vaga' in message

        # (3) the dispatcher reaps both exits and launches neither card again
        tick = kb.dispatch_once(conn)
        assert sorted(tick.rate_limited) == sorted([first, second])
        assert_waiting(conn, tick, [first, second])
        outcomes = [row['outcome'] for row in conn.execute('SELECT outcome FROM task_runs ORDER BY id')]
        assert outcomes == ['rate_limited', 'rate_limited']
        clock.advance(60)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])  # the runtime also closes the two ended runs
        launches, events = len(launcher.launches), event_kinds(conn)
        for _ in range(6):
            clock.advance(3600)
            assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        assert len(launcher.launches) == launches == 2, 'six hours of ticks launched nothing on the closed route'
        assert event_kinds(conn) == events, 'and wrote no event: the cards wait, they are not re-examined aloud'
        assert len(notices(conn)) == 1

        # (6) nothing else was used in its place
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)
        for worker in (run, late):
            assert worker.agent._fallback_chain == [] and True not in worker.fallback and worker.reserve == []
            assert (worker.agent.provider, worker.agent.model) == ROUTE


def test_a_card_of_another_route_is_launched_while_the_route_is_closed(quota_board, launcher, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        other = kb.create_task(conn, title='Other route', assignee='default', model_override='other-model',
                               provider_override='other-provider')
        same = kb.create_task(conn, title='Same route, not an NFOS card', assignee='default',
                              model_override=EXECUTOR_MODEL.upper(), provider_override=EXECUTOR_PROVIDER)
        review = kb.create_task(conn, title='Same route, waiting for its reviewer', assignee='default',
                                model_override=EXECUTOR_MODEL, provider_override=EXECUTOR_PROVIDER)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id IN (?,?)", (other, same))
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        clock.advance(60)
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [other], tick
        assert sorted(tick.claim_held) == sorted((task_id, HELD) for task_id in (first, second, same, review))
        argv, env = launcher.launches[-1]
        launch = argv[:argv.index('chat')]
        assert (option(launch, '--provider'), option(launch, '-m')) == ('other-provider', 'other-model')
        assert json.loads(env[walls.ROUTE_ENV]) == ['other-provider', 'other-model']
        assert kb.get_task(conn, other).status == 'running'
        assert kb.get_task(conn, same).status == 'ready' and kb.get_task(conn, review).status == 'review'
        assert [w['status'] for w in quota_waits(conn, same)] == ['open']


def test_route_reopens_at_the_reset_time_the_provider_gave(quota_board, launcher, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        clock.advance(DAY - 1)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        clock.advance(1)
        tick = kb.dispatch_once(conn)
        (probe,) = spawned(tick)
        (waiting,) = {first, second} - {probe}
        assert tick.claim_held == [(waiting, HELD)], 'one launch tries the route; the other card keeps waiting'
        assert walls.read(*ROUTE)['probe']['task_id'] == probe
        assert_executor_route(launcher.launches[-1][0])

        run = run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert not run.results[0].get('failed') and run.turns == 2
        assert walls.read(*ROUTE) is None and not walls.any_closed(), 'the provider answered: the route is open'

        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [waiting] and not [held for held in tick.claim_held if held[1] == HELD], tick
        # The registry closes each card's wait at its next recheck; the launch above did not wait for that.
        clock.advance(walls.WAIT_RECHECK_SECONDS)
        kb.dispatch_once(conn)
        for task_id in (first, second):
            (wait,) = quota_waits(conn, task_id)
            assert (wait['status'], wait['outcome']) == ('satisfied', 'resumed')
        assert len(notices(conn)) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)


def test_route_with_no_reset_time_is_probed_at_spaced_intervals_until_the_provider_answers(
        quota_board, launcher, monkeypatch):
    assert [walls._probe_spacing(n) for n in range(6)] == [300, 600, 1200, 2400, 3600, 3600]
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, run, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimitFromSDK)
        assert (run.turns, run.provider_calls, run.waits.waited, run.exit_code) == (1, 1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['limit'], wall['reset_at'], wall['next_probe_at']) == ('weekly', None, clock.now + 300)
        (_, message), = notices(conn)
        assert 'sem hora de reinício informada' in message and 'em 5 min' in message and 'até 60 min' in message
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        launches = len(launcher.launches)
        for probes, spacing in enumerate((300, 600, 1200), start=1):
            clock.advance(spacing - 1)
            assert_waiting(conn, kb.dispatch_once(conn), [first, second])
            clock.advance(1)
            tick = kb.dispatch_once(conn)
            (probe,) = spawned(tick)
            (waiting,) = {first, second} - {probe}
            assert tick.claim_held == [(waiting, HELD)], (probes, tick)
            refused = run_card(conn, launcher, monkeypatch, probe, refusing(GoUsageLimitFromSDK))
            assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75), probes
            wall = walls.read(*ROUTE)
            assert (wall['probes'], wall['probe']) == (probes, None)
            assert wall['next_probe_at'] == clock.now + walls._probe_spacing(probes)
            tick = kb.dispatch_once(conn)
            assert tick.rate_limited == [probe]
            assert_waiting(conn, tick, [first, second])
        assert len(launcher.launches) == launches + 3, 'three probes in 35 minutes, one launch each'
        assert len(notices(conn)) == 1, 'a probe that meets the same wall is not a new wall'

        clock.advance(2400)
        (probe,) = spawned(kb.dispatch_once(conn))
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert walls.read(*ROUTE) is None
        (waiting,) = {first, second} - {probe}
        assert spawned(kb.dispatch_once(conn)) == [waiting]
        assert len(notices(conn)) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)


def test_a_throttle_that_resets_inside_the_clients_wait_is_retried_and_guarded_as_before(
        quota_board, launcher, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        task = enrol(conn, launcher)
        run = run_card(conn, launcher, monkeypatch, task.id, refusing(ShortThrottle))
        assert (run.turns, run.provider_calls, run.exit_code) == (1, 3, 75)
        assert 60 <= run.waits.waited < 61, 'two waits of the 30 s the provider asked for'
        assert not walls.any_closed() and notices(conn) == []
        tick = kb.dispatch_once(conn)
        assert tick.rate_limited == [task.id] and tick.respawn_guarded == [(task.id, 'rate_limit_cooldown')]
        assert not tick.claim_held and quota_waits(conn, task.id) == []
        clock.advance(300)
        assert spawned(kb.dispatch_once(conn)) == [task.id]


def test_a_wait_that_outlives_its_deadline_is_closed_as_still_closed_and_the_card_stays_held(
        quota_board, launcher, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        walls.close(*ROUTE, limit='monthly', reset_at=clock.now + 40 * DAY)
        card = kb.create_task(conn, title='Same route', assignee='default', model_override=EXECUTOR_MODEL,
                              provider_override=EXECUTOR_PROVIDER)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (card,))
        assert kb.dispatch_once(conn).claim_held == [(card, HELD)]
        clock.advance(walls.WAIT_DEADLINE_SECONDS + 1)
        tick = kb.dispatch_once(conn)
        assert tick.claim_held == [(card, HELD)] and not tick.spawned
        (wait,) = quota_waits(conn, card)
        assert (wait['status'], wait['outcome']) == ('expired', 'still_closed')
        assert kb.get_task(conn, card).status == 'ready' and kb.get_task(conn, card).consecutive_failures == 0
        assert not conn.execute('SELECT 1 FROM nfos_decisions').fetchone(), 'nobody is consulted about a quota window'


# What the provider's answer says, read from its structured fields only.
class _Named(ProviderRateLimit):
    body = {'error': {'type': 'rate_limit_error', 'message': 'Too many requests'}, 'metadata': {'limitName': 'rpm'}}
    response = SimpleNamespace(status_code=429, headers={'retry-after': '30'})


def _with_retry_after(value):
    return type('RetryAfter', (ProviderRateLimit,), {
        'response': SimpleNamespace(status_code=429, headers={'Retry-After': str(value)})})()


def _with_body(**fields):
    error = ProviderRateLimit()
    error.body = {'error': dict({'type': 'usage_limit_reached', 'message': 'The usage limit has been reached'},
                                **fields)}
    return error


NOW = 1_791_800_000.0
WINDOWS = [
    pytest.param(GoUsageLimit(), {'limit': 'weekly', 'reset_at': NOW + DAY}, id='opencode-go-weekly'),
    pytest.param(GoUsageLimitNoReset(), {'limit': 'weekly', 'reset_at': None}, id='named-limit-no-reset'),
    pytest.param(GoUsageLimitFromSDK(), {'limit': 'weekly', 'reset_at': None}, id='name-only-in-the-http-answer'),
    pytest.param(_with_body(resets_at=NOW + 4 * DAY), {'limit': 'usage_limit_reached', 'reset_at': NOW + 4 * DAY},
                 id='codex-epoch'),
    pytest.param(_with_body(resets_at='2026-10-15T12:54:00Z'),
                 {'limit': 'usage_limit_reached', 'reset_at': 1_792_068_840.0}, id='codex-iso'),
    pytest.param(_with_body(resets_in_seconds=5 * 3600), {'limit': 'usage_limit_reached', 'reset_at': NOW + 18000},
                 id='five-hour-window'),
    pytest.param(_with_retry_after(601), {'limit': 'retry-after', 'reset_at': NOW + 601}, id='retry-after-past-cap'),
    pytest.param(_with_retry_after(formatdate(NOW + 7200, usegmt=True)),
                 {'limit': 'retry-after', 'reset_at': NOW + 7200}, id='retry-after-http-date'),
    pytest.param(_with_retry_after(600), None, id='retry-after-at-the-cap'),
    pytest.param(ShortThrottle(), None, id='short-throttle'),
    pytest.param(_Named(), None, id='named-limit-that-resets-in-seconds'),
    pytest.param(_with_body(resets_in_seconds=45), None, id='usage-limit-that-resets-in-seconds'),
    pytest.param(ProviderRateLimit(), None, id='plain-429'),
    pytest.param(ValueError('usage limit reached, resets in 5 hours'), None, id='only-text-says-so'),
]


@pytest.mark.parametrize('error, expected', WINDOWS)
def test_long_window_is_read_from_the_structured_fields_of_the_answer(error, expected):
    assert walls.quota_window(error, now=NOW) == expected


def _agent(pending_fallback=False):
    sent = []
    return SimpleNamespace(provider=EXECUTOR_PROVIDER, model=EXECUTOR_MODEL, _fallback_activated=False,
                           _has_pending_fallback=lambda: pending_fallback, sent=sent,
                           _record_worker_fallback_notice=sent.append)


def test_only_a_cards_own_worker_with_no_fallback_left_closes_a_route(quota_board, monkeypatch):
    # Any session leaves the turn on a usage window; a session that is not a card's worker closes nothing.
    assert walls.on_quota_error(_agent(), GoUsageLimit()) is True
    assert walls.on_quota_error(_agent(), ShortThrottle()) is False
    assert not walls.any_closed()

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    # A fallback provider the operator configured still has the turn: the route is not closed under it.
    reserve = _agent(pending_fallback=True)
    assert walls.on_quota_error(reserve, GoUsageLimit()) is True and not walls.any_closed() and reserve.sent == []

    # The dispatcher's spelling of the route wins over the agent's while the worker runs its primary model.
    monkeypatch.setenv(walls.ROUTE_ENV, json.dumps(['OpenCode-Go', 'DeepSeek-V4.1-Flash']))
    worker = _agent()
    worker.provider, worker.model = 'opencode', 'deepseek/deepseek-v4.1-flash'
    assert walls.on_quota_error(worker, GoUsageLimit()) is True
    assert walls.read(*ROUTE)['closed_by']['task_id'] == 't_worker' and len(worker.sent) == 1
    again = _agent()
    assert walls.on_quota_error(again, GoUsageLimit()) is True and again.sent == [], 'one notice for one wall'


def test_only_a_worker_that_began_after_the_wall_reopens_the_route(quota_board, monkeypatch):
    clock = Clock(monkeypatch)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    # In flight before the wall: its late answer is not proof that the window ended.
    early = _agent()
    assert walls.answered(early) is False
    clock.advance(5)
    walls.close(*ROUTE, limit='weekly')
    early.session_api_calls = 1
    assert walls.answered(early) is False and walls.read(*ROUTE) is not None

    clock.advance(300)
    late = _agent()
    assert walls.answered(late) is False, 'first iteration: nothing answered yet'
    assert walls.answered(late) is False and walls.read(*ROUTE) is not None, 'no answered call counted yet'
    late.session_api_calls = 1
    assert walls.answered(late) is True and walls.read(*ROUTE) is None


def test_probe_slot_is_single_and_a_silent_probe_gives_it_back(quota_board, monkeypatch):
    clock = Clock(monkeypatch)
    wall, opened = walls.close(*ROUTE, limit='weekly', task_id='t_a')
    assert opened and walls.closed(*ROUTE) is not None
    assert walls.close(*ROUTE, limit='weekly', task_id='t_b') == (wall, False), 'a second card changes nothing'
    clock.advance(300)
    assert walls.closed(*ROUTE) is None, 'due: the next launch is the probe'
    walls.take_probe(*ROUTE, task_id='t_b')
    assert walls.closed(*ROUTE) is not None, 'one probe at a time'
    clock.advance(walls.PROBE_LEASE_SECONDS - 1)
    assert walls.closed(*ROUTE) is not None
    clock.advance(1)
    assert walls.closed(*ROUTE) is None, 'the probe said nothing: another card may try'
    # A card that is not the probe meets the wall: the schedule is the route's, it does not move.
    assert walls.close(*ROUTE, limit='weekly', task_id='t_a')[0]['probes'] == 0
    # The probe's refusal schedules the next one, twice as far.
    refused, opened = walls.close(*ROUTE, limit='weekly', task_id='t_b')
    assert not opened and (refused['probes'], refused['probe']) == (1, None)
    assert refused['next_probe_at'] == clock.now + 600
