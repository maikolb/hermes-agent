"""ROUTE_WALL_20261012: a provider route closed by a long usage window.

Read on the production profile on 2026-10-12 (read-only): opencode-go refused 2,046 of 2,046 calls from the 8th to the
11th with `metadata.limitName: weekly` and no reset time in the body; Codex refuses with `usage_limit_reached` and
the reset time of the window. After the executor learned to leave at the wall with exit code 75, a launch onto the
weekly wall still slept through two Retry-After waits capped at 600 s (about twenty minutes holding a slot) and the
respawn guard launched the card again on the same wall, five minutes later and then doubling up to one hour.

What these tests hold, with the real dispatcher tick, a real board, real child processes and the real quiet
single-query path of the worker (only the provider call, the goal judge and the clock are stand-ins):

* the worker leaves on the first refusal of a usage window, with no wait inside the turn;
* the route (the profile the card is launched as, the provider and the model) is closed on every board of the
  host, with the name of the limit, the reset time when the provider gives one and the account it named, and one
  notice goes out for it, from the gateway's notifier to the management topic, whatever chat the card that met
  the wall was born in; the account stays on the wall and out of the notice; while the wall stands and something
  waits for its route the notice is repeated once a day, with how long the route has been closed and how many
  cards and inbox entries wait for it, and a wall nothing waits for any more is not announced again;
* the wall is of that profile's credential: another profile of the host on the same provider and model, with an
  account of its own, goes on being launched and delivers, and gets a wall and a notice of its own when its own
  window ends; the dispatcher and the worker name the same route when the card's profile is not the dispatcher's;
* while it is closed no card of that route is launched: the cards hold no slot, count no failure and stand in the
  wait registry; a card of another route is launched as always, and a card that leaves the route, or that another
  hold takes, stops being said to wait for it;
* a closed route is always probed, one launch at a time, and reopens when the provider answers one: every hour
  and at the reset time when the provider gave one (the reset time only brings the launch forward), at spaced
  intervals when it gave none; a probe the provider refuses does not send a second notice; of the launchers that
  see a route due at the same moment one takes the probe, two launchers of the same card or inbox entry included;
  a route that closes again at once is the same wall going on; the probe of the hour is a card's: an inbox entry
  waits for the reset time the provider gave, and the notices promise no probe on its account;
* a probe the provider refuses spends nothing of the balance of the escalated card that probes: not a goal turn,
  not an iteration, not a second of its runtime;
* a throttle that resets inside the client's own wait is retried as before;
* no other provider or model is used in place of a closed route.
"""
import asyncio
import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from types import SimpleNamespace

import pytest
import yaml

from gateway import kanban_watchers as kw
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, nfos_route_walls as walls, nfos_runtime, nfos_waits
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    EXECUTOR_MODEL, EXECUTOR_PROVIDER, PROFILE_CONFIG, SOURCE, Clock, CodexUsageLimit, GoUsageLimit,
    GoUsageLimitFromSDK, GoUsageLimitNoReset, ProviderRateLimit, ShortThrottle, assert_executor_route, enrol,
    launcher, metered_reply, option, quota_board, run_executor,
)

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

# The profile the suite's cards are launched as (the dispatcher's own), and its route on the executor's provider.
PROFILE = 'default'
ROUTE = (PROFILE, EXECUTOR_PROVIDER, EXECUTOR_MODEL)
HELD = f'route_closed:{EXECUTOR_PROVIDER}/{EXECUTOR_MODEL}@{PROFILE}'
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


def launch_of(launcher, task_id):
    """The argv and environment of this card's latest launch."""
    return next(launch for launch in reversed(launcher.launches) if launch[1]['HERMES_KANBAN_TASK'] == task_id)


def run_card(conn, launcher, monkeypatch, task_id, answer, judge_reply=None):
    """The worker of this card's latest launch, run in this process, in the home and on the board the launch gave
    it, leaving with the code it chose."""
    argv, env = launch_of(launcher, task_id)
    pid = kb.get_task(conn, task_id).worker_pid  # a card its worker hands in no longer names the process
    run = run_executor(monkeypatch, argv, env, answer=answer, judge_reply=judge_reply)
    launcher.exit(pid, run.exit_code)
    return run


def two_cards(conn, launcher):
    """Two NFOS cards of the executor route, both launched while the route was open."""
    first = enrol(conn, launcher)
    second = enrol(conn, launcher, text=SECOND_REQUEST, message_id='124')
    return first.id, second.id


# The project's own topic, where the request came from and the only chat the card follows, and the management topic.
PROJECT_TOPIC = (SOURCE['chat_id'], SOURCE['thread_id'])
MANAGEMENT_TOPIC = (SOURCE['chat_id'], '2')


class Operations:
    """The gateway's notifier with its chat adapter connected and the router's two topics: the project's and the
    management one. Only the adapter's send is a stand-in."""

    def __init__(self):
        self.sent = []
        self.runner = GatewayRunner.__new__(GatewayRunner)
        self.runner._active_profile_name = lambda: 'default'
        self.runner._authorization_adapter = lambda platform, profile: self
        self.runner._kanban_board_display_targets = self.targets

    def targets(self, profiles, management=False):
        chat, topic = MANAGEMENT_TOPIC if management else PROJECT_TOPIC
        return {'management' if management else 'default': [
            {'platform': 'telegram', 'chat_id': chat, 'thread_id': topic, 'notifier_profile': 'default'}]}

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, (metadata or {}).get('thread_id'), text))
        return SendResult(success=True, message_id=str(len(self.sent)))

    def notices(self):
        """One notifier tick, then every message the gateway has sent so far."""
        asyncio.run(self.runner._kanban_route_wall_notices())
        return list(self.sent)


@pytest.fixture
def ops():
    return Operations()


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
        assert wait['predicate'] == {'kind': 'route_open', 'profile': PROFILE, 'provider': EXECUTOR_PROVIDER,
                                     'model': EXECUTOR_MODEL}


def close_route_with_two_cards(conn, launcher, monkeypatch, wall):
    """Both cards running, the first meets `wall`, then the second (launched before the route closed) meets it."""
    first, second = two_cards(conn, launcher)
    run = run_card(conn, launcher, monkeypatch, first, refusing(wall))
    late = run_card(conn, launcher, monkeypatch, second, refusing(wall))
    return first, second, run, late


def test_worker_on_a_weekly_window_leaves_on_the_first_refusal_and_the_route_closes_with_one_notice(
        quota_board, launcher, monkeypatch, ops):
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
        assert (*walls._route(wall), wall['limit'], wall['account']) == (*ROUTE, 'weekly', 'wrk_quota_wall_suite')
        assert wall['reset_at'] == clock.now + DAY and wall['closed_by']['task_id'] == first
        assert wall['probes'] == 0 and wall['probe'] is None
        assert [w['model'] for w in walls.walls()] == [EXECUTOR_MODEL]
        (file,) = (kb.kanban_home() / 'kanban' / 'route_walls').glob('*.json')
        assert file.name.startswith(f'{PROFILE}--{EXECUTOR_PROVIDER}--{EXECUTOR_MODEL}.'), 'found by its name'

        # (5) one notice for the wall, in the management topic
        (*topic, message), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(clock.now + DAY))
        assert f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada para o perfil {PROFILE}' in message
        assert 'pelo limite weekly, com reinício informado' in message
        assert 'wrk_quota_wall_suite' not in message and 'conta ' not in message, 'the account stays on the wall'
        assert when in message and 'o provedor pediu 1 d de espera' in message
        assert 'a cada 60 min' in message and 'esperam sem ocupar vaga' in message

        # (3) the dispatcher reaps both exits and launches neither card again
        tick = kb.dispatch_once(conn)
        assert sorted(tick.rate_limited) == sorted([first, second])
        assert_waiting(conn, tick, [first, second])
        outcomes = [row['outcome'] for row in conn.execute('SELECT outcome FROM task_runs ORDER BY id')]
        assert outcomes == ['rate_limited', 'rate_limited']
        clock.advance(60)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])  # the runtime also closes the two ended runs
        launches, events = len(launcher.launches), event_kinds(conn)
        for _ in range(58):
            clock.advance(60)
            assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        assert clock.now == wall['next_probe_at'] - 60 == wall['closed_at'] + 3540
        assert len(launcher.launches) == launches == 2, 'an hour of ticks launched nothing: the probe is hourly'
        assert event_kinds(conn) == events, 'and wrote no event: the cards wait, they are not re-examined aloud'
        assert len(ops.notices()) == 1 and 'model_fallback' not in events

        # (6) nothing else was used in its place
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)
        for worker in (run, late):
            assert worker.agent._fallback_chain == [] and True not in worker.fallback and worker.reserve == []
            assert (worker.agent.provider, worker.agent.model) == ROUTE[1:]


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
        assert json.loads(env[walls.ROUTE_ENV]) == [PROFILE, 'other-provider', 'other-model']
        assert kb.get_task(conn, other).status == 'running'
        assert kb.get_task(conn, same).status == 'ready' and kb.get_task(conn, review).status == 'review'
        assert [w['status'] for w in quota_waits(conn, same)] == ['open']


def test_a_card_that_pins_no_route_takes_the_route_of_its_profile(quota_board, launcher, monkeypatch):
    """Not only NFOS cards: any card is held by the route its launch would use. With neither provider nor model
    pinned that is the profile's own; a card that pins only the model keeps the profile's provider."""
    profile_route = (PROFILE, 'openai-codex', 'gpt-6-astra')
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        plain = kb.create_task(conn, title='No pin', assignee='default')
        model_only = kb.create_task(conn, title='Model pinned, provider from the profile', assignee='default',
                                    model_override='gpt-5.6-luna')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id IN (?,?)", (plain, model_only))
        assert walls.card_route(conn, kb.get_task(conn, plain)) == profile_route
        assert walls.card_route(conn, kb.get_task(conn, model_only)) == (PROFILE, 'openai-codex', 'gpt-5.6-luna')

        walls.close(*profile_route, limit='usage_limit_reached', reset_at=clock.now + 3 * DAY)
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [model_only]
        assert tick.claim_held == [(plain, f'route_closed:openai-codex/gpt-6-astra@{PROFILE}')]
        assert json.loads(launcher.launches[-1][1][walls.ROUTE_ENV]) == [PROFILE, 'openai-codex', 'gpt-5.6-luna']

        clock.advance(3 * DAY)
        assert spawned(kb.dispatch_once(conn)) == [plain], 'the reset time came: this launch tries the route'
        assert json.loads(launcher.launches[-1][1][walls.ROUTE_ENV]) == list(profile_route)
        assert walls.read(*profile_route)['probe']['task_id'] == plain


def test_a_card_with_an_open_decision_stays_with_that_hold(quota_board, launcher, monkeypatch):
    """A question to the Principal or to a person already holds the card and has its own wait and reminders. The
    closed route does not take the card over: it waits for one thing at a time."""
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        # The dispatcher's own predicate for "this card has an open question", answered here for the second card.
        monkeypatch.setattr(kb, '_nfos_decision_open', lambda conn, task_id: task_id == second)
        kb.dispatch_once(conn)
        clock.advance(300)  # past the per-card wait of the quota exit, which comes before the decision's hold
        tick = kb.dispatch_once(conn)
        assert sorted(tick.claim_held) == sorted([(first, HELD), (second, 'open_decision')]) and not tick.spawned, tick
        assert [w['status'] for w in quota_waits(conn, first)] == ['open'] and quota_waits(conn, second) == []


def go_window(seconds):
    """The opencode-go weekly refusal with the reset this many seconds away."""
    return type('GoUsageLimitIn', (GoUsageLimit,), {
        'response': SimpleNamespace(status_code=429, headers={'retry-after': str(seconds)})})


def probe_of(launcher, tick, cards):
    """The one card this tick launched on the closed route, with the other cards of the route still held."""
    (probe,) = spawned(tick)
    assert sorted(tick.claim_held) == sorted((task_id, HELD) for task_id in cards if task_id != probe), tick
    assert walls.read(*ROUTE)['probe']['task_id'] == probe
    assert_executor_route(launch_of(launcher, probe)[0])
    return probe


def test_a_route_with_a_distant_reset_time_is_probed_every_hour_and_reopens_at_the_first_probe_answered(
        quota_board, launcher, monkeypatch, ops):
    """The provider says the window ends in seven days. Obeyed as a time before which nothing is tried, that kept
    the route shut for the week even when the limit was lifted on the first day (quota bought, plan changed):
    someone had to erase the wall's file. The time the provider gave only brings the next launch forward; the
    route is tried every hour, one launch that leaves on its first refused call."""
    week = go_window(7 * DAY)
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        start = clock.now
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, week)
        wall = walls.read(*ROUTE)
        assert (wall['reset_at'], wall['next_probe_at']) == (start + 7 * DAY, start + 3600)
        assert walls.open_at(wall) == start + 3600, 'the next launch is the probe of the hour, not the reset time'
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        assert len(ops.notices()) == 1

        launches = len(launcher.launches)
        for hour in (1, 2):
            clock.advance(3599)
            assert_waiting(conn, kb.dispatch_once(conn), [first, second])
            clock.advance(1)
            probe = probe_of(launcher, kb.dispatch_once(conn), [first, second])
            refused = run_card(conn, launcher, monkeypatch, probe, refusing(week))
            assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75), hour
            wall = walls.read(*ROUTE)
            assert (wall['probes'], wall['probe'], wall['closed_at']) == (hour, None, start), 'the same wall'
            assert (wall['reset_at'], wall['next_probe_at']) == (clock.now + 7 * DAY, clock.now + 3600)
            tick = kb.dispatch_once(conn)
            assert tick.rate_limited == [probe]
            assert_waiting(conn, tick, [first, second])
        assert len(launcher.launches) == launches + 2, 'one launch an hour on the closed route'
        assert len(ops.notices()) == 1, 'a refused probe is not a new wall'

        # The limit is lifted on the first day. The probe of the next hour is answered and the route reopens.
        clock.advance(3600)
        probe = probe_of(launcher, kb.dispatch_once(conn), [first, second])
        run = run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert not run.results[0].get('failed') and run.turns == 2
        assert walls.read(*ROUTE) is None and not walls.any_closed(), 'the provider answered: the route is open'
        assert clock.now == start + 3 * 3600 < wall['reset_at'] - 6 * DAY, 'six days before the time it gave'

        (waiting,) = {first, second} - {probe}
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [waiting] and not [held for held in tick.claim_held if held[1] == HELD], tick
        # The registry closes each card's wait at its next recheck; the launch above did not wait for that.
        clock.advance(walls.WAIT_RECHECK_SECONDS)
        kb.dispatch_once(conn)
        for task_id in (first, second):
            (wait,) = quota_waits(conn, task_id)
            assert (wait['status'], wait['outcome']) == ('satisfied', 'resumed')
        assert len(ops.notices()) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)


def test_a_reset_time_sooner_than_the_probe_of_the_hour_brings_the_launch_forward(
        quota_board, launcher, monkeypatch, ops):
    """Twenty minutes is past what the client waits inside a turn, so it is a window; and it is sooner than the
    probe of the hour, so the launch goes at the time the provider gave."""
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        start = clock.now
        first, second, run, _ = close_route_with_two_cards(conn, launcher, monkeypatch, go_window(1200))
        assert (run.provider_calls, run.waits.waited, run.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['reset_at'], wall['next_probe_at'], walls.open_at(wall)) == (start + 1200, start + 3600,
                                                                                  start + 1200)
        (*_, message), = ops.notices()
        assert 'o provedor pediu 20 min de espera' in message
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        clock.advance(1199)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        # At that time one launch tries the route. The window had not ended: the provider gives a new time, which
        # is again sooner than an hour.
        clock.advance(1)
        probe = probe_of(launcher, kb.dispatch_once(conn), [first, second])
        refused = run_card(conn, launcher, monkeypatch, probe, refusing(go_window(900)))
        assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['probes'], wall['reset_at'], wall['next_probe_at']) == (1, clock.now + 900, clock.now + 3600)
        assert walls.open_at(wall) == clock.now + 900
        tick = kb.dispatch_once(conn)
        assert tick.rate_limited == [probe]
        assert_waiting(conn, tick, [first, second])
        clock.advance(899)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        clock.advance(1)
        probe = probe_of(launcher, kb.dispatch_once(conn), [first, second])
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert walls.read(*ROUTE) is None and clock.now == start + 2100
        (waiting,) = {first, second} - {probe}
        assert spawned(kb.dispatch_once(conn)) == [waiting]
        assert len(ops.notices()) == 1


def test_route_with_no_reset_time_is_probed_at_spaced_intervals_until_the_provider_answers(
        quota_board, launcher, monkeypatch, ops):
    assert [walls._probe_spacing(n) for n in range(6)] == [300, 600, 1200, 2400, 3600, 3600]
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, run, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimitFromSDK)
        assert (run.turns, run.provider_calls, run.waits.waited, run.exit_code) == (1, 1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['limit'], wall['reset_at'], wall['next_probe_at']) == ('weekly', None, clock.now + 300)
        (*_, message), = ops.notices()
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
        assert len(ops.notices()) == 1, 'a probe that meets the same wall is not a new wall'

        clock.advance(2400)
        (probe,) = spawned(kb.dispatch_once(conn))
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert walls.read(*ROUTE) is None
        (waiting,) = {first, second} - {probe}
        assert spawned(kb.dispatch_once(conn)) == [waiting]
        assert len(ops.notices()) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)


def test_a_throttle_that_resets_inside_the_clients_wait_is_retried_and_guarded_as_before(
        quota_board, launcher, monkeypatch, ops):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        task = enrol(conn, launcher)
        run = run_card(conn, launcher, monkeypatch, task.id, refusing(ShortThrottle))
        assert (run.turns, run.provider_calls, run.exit_code) == (1, 3, 75)
        assert 60 <= run.waits.waited < 61, 'two waits of the 30 s the provider asked for'
        assert not walls.any_closed() and ops.notices() == []
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
        # The wall as its latest hourly probe left it: refused this instant, the window still five days long.
        assert walls.take_probe(*ROUTE, task_id='t_probe')
        walls.close(*ROUTE, limit='monthly', reset_at=clock.now + 5 * DAY, task_id='t_probe')
        tick = kb.dispatch_once(conn)
        assert tick.claim_held == [(card, HELD)] and not tick.spawned
        (wait,) = quota_waits(conn, card)
        assert (wait['status'], wait['outcome']) == ('expired', 'still_closed')
        assert kb.get_task(conn, card).status == 'ready' and kb.get_task(conn, card).consecutive_failures == 0
        assert not conn.execute('SELECT 1 FROM nfos_decisions').fetchone(), 'nobody is consulted about a quota window'


def test_the_notice_reaches_the_management_topic_when_the_card_follows_only_its_clients_chat(
        quota_board, launcher, monkeypatch, ops):
    """Read on the production profile on 2026-10-12: 11 of its 12 project topics are the source of an enabled
    delivery project, and a card follows the topic its request came from and no other. Nothing passive is
    published there (CLIENT_CHAT_20260913), so a notice that hung on the card's subscription reached nobody."""
    with kb.connect_closing() as conn:
        Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        kb.dispatch_once(conn)
        project = {'enabled': True, 'source': {key: SOURCE[key] for key in ('platform', 'chat_id', 'thread_id')}}
        monkeypatch.setattr(nfos_runtime, 'project_config', lambda board, config=None: dict(project, board=board))
        for task_id in (first, second):
            (sub,) = kb.list_notify_subs(conn, task_id)
            assert (sub['chat_id'], sub['thread_id']) == PROJECT_TOPIC and kw._is_client_chat('default', sub)
        assert 'model_fallback' in kw._CLIENT_SILENT_KINDS
        assert 'model_fallback' not in event_kinds(conn), 'the notice is not an event of the card that met the wall'

        (*topic, message), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC and f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada para' in message
        assert [sent[:2] for sent in ops.notices()] == [MANAGEMENT_TOPIC], 'the next tick sends nothing'
        # A gateway that restarts, or a second gateway of the host, finds the receipt beside the wall.
        assert Operations().notices() == []
        receipt = walls._load(walls._path(*ROUTE, walls.NOTICE))
        assert receipt['sent_at'] and receipt['closed_at'] == walls.read(*ROUTE)['closed_at']
        assert [(sent['chat_id'], sent['thread_id']) for sent in receipt['deliveries']] == [MANAGEMENT_TOPIC]


def test_a_notice_no_topic_could_take_goes_out_once_one_can(quota_board, monkeypatch):
    clock = Clock(monkeypatch)
    walls.close(*ROUTE, limit='weekly', task_id='t_a')
    deaf = Operations()
    deaf.runner._authorization_adapter = lambda platform, profile: None
    assert deaf.notices() == [] and len(walls.unannounced()) == 1, 'no connected topic: the notice is not taken'
    # A notifier that took the notice and died before sending it gives it back when its claim lapses.
    assert walls.claim_notice(walls.read(*ROUTE)) and walls.unannounced() == []
    assert not walls.claim_notice(walls.read(*ROUTE)), 'one notifier at a time'
    ops = Operations()
    assert ops.notices() == []
    clock.advance(walls.PROBE_LEASE_SECONDS)
    assert len(ops.notices()) == 1 and len(ops.notices()) == 1 and walls.unannounced() == []


def test_a_route_that_closes_again_at_once_is_the_same_wall_going_on(quota_board, monkeypatch, ops):
    """A rolling window gives quota back a little at a time: a probe is answered, the cards spend what came back
    and the route closes again. That is not a new wall: no second notice, and the schedule does not start over."""
    clock = Clock(monkeypatch)
    wall, opened = walls.close(*ROUTE, limit='5h', task_id='t_a')
    assert opened and len(ops.notices()) == 1
    clock.advance(300)
    assert walls.reopen(*ROUTE) and walls.read(*ROUTE) is None and not walls.any_closed()
    clock.advance(120)
    again, opened = walls.close(*ROUTE, limit='5h', task_id='t_b')
    assert not opened and again['closed_at'] == wall['closed_at'] and again['closed_by']['task_id'] == 't_a'
    assert (again['probes'], again['next_probe_at']) == (1, clock.now + 600)
    assert len(ops.notices()) == 1, 'the same wall: no second notice'

    clock.advance(600)
    assert walls.closed(*ROUTE) is None and walls.reopen(*ROUTE)
    clock.advance(1199)
    again, opened = walls.close(*ROUTE, limit='5h', task_id='t_a')
    assert not opened and (again['probes'], again['next_probe_at']) == (2, clock.now + 1200)
    assert len(ops.notices()) == 1

    # Open for as long as its next probe would have waited: that window had ended, and this is a new wall.
    assert walls.reopen(*ROUTE)
    clock.advance(2400)
    new, opened = walls.close(*ROUTE, limit='5h', task_id='t_c')
    assert opened and (new['closed_at'], new['probes'], new['closed_by']['task_id']) == (clock.now, 0, 't_c')
    assert len(ops.notices()) == 2
    # Another limit on the same route is another wall, however soon it comes.
    assert walls.reopen(*ROUTE)
    clock.advance(1)
    other, opened = walls.close(*ROUTE, limit='weekly', task_id='t_c')
    assert opened and other['probes'] == 0 and len(ops.notices()) == 3


def test_a_card_moved_to_another_route_is_launched_there_and_stops_waiting_for_the_closed_one(
        quota_board, launcher, monkeypatch):
    """The wall file says whether the route is closed; only the card says whether that is still its route. With
    the executor pointed at another route no card is left to probe the old one, so its wall stands, and a wait
    left open would keep saying the cards wait while they run."""
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        config = copy.deepcopy(PROFILE_CONFIG)
        config['kanban']['delivery'].update(worker_model='other-model', worker_provider='other-provider')
        (quota_board / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
        clock.advance(300)  # past the per-card wait of the quota exit
        tick = kb.dispatch_once(conn)
        assert sorted(spawned(tick)) == sorted([first, second]) and not tick.claim_held, tick
        for argv, env in launcher.launches[-2:]:
            launch = argv[:argv.index('chat')]
            assert (option(launch, '--provider'), option(launch, '-m')) == ('other-provider', 'other-model')
            assert json.loads(env[walls.ROUTE_ENV]) == [PROFILE, 'other-provider', 'other-model']
        assert walls.read(*ROUTE) is not None and walls.read(*ROUTE)['probe'] is None, 'the old wall stands, unprobed'
        for task_id in (first, second):
            (wait,) = quota_waits(conn, task_id)
            assert (wait['status'], wait['outcome']) == ('withdrawn', 'route_changed')
            assert kb.get_task(conn, task_id).status == 'running'


def test_a_card_another_hold_keeps_is_not_said_to_wait_for_the_route(quota_board, launcher, monkeypatch):
    """A request waiting for its approval has its own trail, and whoever watches the board looks for it on a card
    that has no wait in the registry. The closed route still keeps the card from being launched; it does not
    speak for it."""
    from hermes_cli import nfos_delivery as delivery
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        # The claim's own predicate for a request whose approval is pending, answered here for the second card.
        monkeypatch.setattr(delivery, 'task_support_approval_pending', lambda conn, task_id: task_id == second)
        clock.advance(walls.WAIT_RECHECK_SECONDS)
        tick = kb.dispatch_once(conn)
        state = {name: (kb.get_task(conn, task_id).status, dict(tick.claim_held).get(task_id))
                 for name, task_id in (('first', first), ('second', second))}
        assert state == {'first': ('ready', HELD), 'second': ('ready', HELD)} and not tick.spawned, (state, repr(tick))
        assert [wait['status'] for wait in quota_waits(conn, first)] == ['open']
        (gone,) = quota_waits(conn, second)
        assert (gone['status'], gone['outcome']) == ('withdrawn', 'other_hold')
        clock.advance(60)
        assert sorted(kb.dispatch_once(conn).claim_held) == sorted([(first, HELD), (second, HELD)])
        assert len(quota_waits(conn, second)) == 1, 'held by the route, and not declared again while that lasts'

        monkeypatch.setattr(delivery, 'task_support_approval_pending', lambda conn, task_id: False)
        clock.advance(60)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])


# Two profiles of one host on the same provider and model, each with the credential of its own home: the owner's
# case (one account for the factory, another for the rest). The provider counts usage per account.
OTHER = 'pessoal'
OTHER_ROUTE = (OTHER, EXECUTOR_PROVIDER, EXECUTOR_MODEL)
HELD_OTHER = f'route_closed:{EXECUTOR_PROVIDER}/{EXECUTOR_MODEL}@{OTHER}'
ACCOUNTS = {PROFILE: 'wrk_factory', OTHER: 'wrk_personal'}
CLOSEOUT = 'Done: the total now reads the paid column. Evidence: the order screen shows 42,00 for order 7.'


def second_profile(home):
    """Another profile of the host, with its own home (config, sessions, credentials) under the same root. Its
    own model is the executor's provider and model, which is not what the dispatcher's profile runs."""
    profile = home / 'profiles' / OTHER
    profile.mkdir(parents=True)
    config = dict(copy.deepcopy(PROFILE_CONFIG), model={'default': EXECUTOR_MODEL, 'provider': EXECUTOR_PROVIDER})
    (profile / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    from hermes_state import SessionDB
    SessionDB(db_path=profile / 'state.db').close()
    return profile


def ready_card(conn, title, *, assignee, pinned=True):
    """A card outside the NFOS, ready for the dispatcher, on the executor's provider and model: pinned on the card,
    or left to the profile it is assigned to."""
    pins = dict(model_override=EXECUTOR_MODEL, provider_override=EXECUTOR_PROVIDER) if pinned else {}
    task_id = kb.create_task(conn, title=title, body='Correct the total of the order screen.', assignee=assignee,
                             goal_mode=True, **pins)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    return task_id


class Accounts:
    """The provider. A worker calls it with the credential of the home it runs in, so the account is the one of
    its profile. An account past its weekly window is refused by name, as opencode-go does. An account with quota
    is answered: the model asks for the worker's own `kanban_complete`, which the agent runs, and then says so."""

    def __init__(self):
        self.walled, self.calls = set(), []

    def __call__(self, seen):
        from hermes_cli.profiles import get_active_profile_name
        account = ACCOUNTS[get_active_profile_name()]
        self.calls.append(account)
        if account in self.walled:
            body = copy.deepcopy(GoUsageLimit.body)
            body['metadata']['workspace'] = account
            raise type('GoUsageLimitOfAccount', (GoUsageLimit,), {'body': body})()
        if seen.provider_calls > 1:
            return metered_reply('The card is handed in.')
        # The suite builds its agent with no tool at all; a card's worker always has the board's own.
        seen.agent.valid_tool_names = {'kanban_complete'}
        call = SimpleNamespace(id='call_hand_in', type='function', function=SimpleNamespace(
            name='kanban_complete', arguments=json.dumps({'summary': CLOSEOUT})))
        handing_in = metered_reply(None)
        handing_in.choices[0].message.tool_calls, handing_in.choices[0].finish_reason = [call], 'tool_calls'
        return handing_in


def test_a_wall_holds_the_cards_of_its_profile_and_no_other_profile_of_the_host(
        quota_board, launcher, monkeypatch, ops):
    """The quota that ran out is of one credential. A wall kept by provider and model alone held, for days, the
    cards of a profile whose own account had quota left."""
    provider, profile_home = Accounts(), second_profile(quota_board)
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second = two_cards(conn, launcher)
        provider.walled.add(ACCOUNTS[PROFILE])
        for task_id in (first, second):
            refused = run_card(conn, launcher, monkeypatch, task_id, provider)
            assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['profile'], wall['account'], wall['closed_by']['task_id']) == (PROFILE, 'wrk_factory', first)
        assert walls.read(*OTHER_ROUTE) is None and [walls._route(w) for w in walls.walls()] == [ROUTE]
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])

        # The other profile's cards, on the same provider and model, are launched as that profile and delivered.
        mine = [ready_card(conn, title, assignee=OTHER) for title in ('Order total', 'Order sorting')]
        clock.advance(60)
        tick = kb.dispatch_once(conn)
        assert sorted(spawned(tick)) == sorted(mine), tick
        assert sorted(tick.claim_held) == sorted([(first, HELD), (second, HELD)]), 'only the walled profile is held'
        for task_id in mine:
            argv, env = launch_of(launcher, task_id)
            launch = argv[:argv.index('chat')]
            assert (option(launch, '-p'), env['HERMES_HOME']) == (OTHER, str(profile_home))
            assert (option(launch, '--provider'), option(launch, '-m')) == ROUTE[1:], 'the same provider and model'
            assert json.loads(env[walls.ROUTE_ENV]) == list(OTHER_ROUTE)
            run = run_card(conn, launcher, monkeypatch, task_id, provider)
            assert (run.turns, run.provider_calls, run.refused, run.exit_code) == (1, 2, 0, 0), run.results
            delivered = kb.get_task(conn, task_id)
            assert (delivered.status, delivered.result) == ('done', CLOSEOUT) and quota_waits(conn, task_id) == []
        assert provider.calls == ['wrk_factory'] * 2 + ['wrk_personal'] * 4, 'each worker on the account of its home'
        assert walls.read(*ROUTE) == wall, 'an answer to another account does not reopen this one'
        tick = kb.dispatch_once(conn)
        assert not tick.crashed and not tick.auto_blocked and not tick.spawned, tick
        assert sorted(tick.claim_held) == sorted([(first, HELD), (second, HELD)])

        # One notice, of the profile whose route closed, with the account the provider named.
        (*topic, message), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC
        assert f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada para o perfil {PROFILE}' in message
        assert 'limite weekly,' in message and OTHER not in message
        assert 'wrk_factory' not in message, 'whose account it is stays on the wall'

        # Later the other account's own window ends: its own wall, its own notice, its own cards held.
        provider.walled.add(ACCOUNTS[OTHER])
        third = ready_card(conn, 'Order filter', assignee=OTHER)
        clock.advance(600)
        assert spawned(kb.dispatch_once(conn)) == [third]
        refused = run_card(conn, launcher, monkeypatch, third, provider)
        assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        other_wall = walls.read(*OTHER_ROUTE)
        assert (other_wall['profile'], other_wall['account']) == (OTHER, 'wrk_personal')
        assert other_wall['closed_by']['task_id'] == third and other_wall['reset_at'] == clock.now + DAY
        assert walls.read(*ROUTE) == wall, 'the first wall is as it was'
        names = sorted(file.name for file in (kb.kanban_home() / 'kanban' / 'route_walls').glob('*.json'))
        assert [name.split('--')[0] for name in names] == [PROFILE, OTHER]
        first_notice, second_notice = (sent[2] for sent in ops.notices())
        assert first_notice == message and f'fechada para o perfil {OTHER}' in second_notice
        assert 'wrk_personal' not in second_notice and 'wrk_factory' not in second_notice
        tick = kb.dispatch_once(conn)
        assert tick.rate_limited == [third] and not tick.spawned
        assert sorted(tick.claim_held) == sorted([(first, HELD), (second, HELD), (third, HELD_OTHER)])
        (wait,) = quota_waits(conn, third)
        assert wait['predicate'] == {'kind': 'route_open', 'profile': OTHER, 'provider': EXECUTOR_PROVIDER,
                                     'model': EXECUTOR_MODEL}

        # Each route reopens on its own: the first profile's hourly probe comes while the other's is still ahead.
        provider.walled.discard(ACCOUNTS[PROFILE])
        clock.advance(wall['next_probe_at'] - clock.now)
        assert clock.now < other_wall['next_probe_at'] < wall['reset_at']
        tick = kb.dispatch_once(conn)
        (probe,) = spawned(tick)
        assert probe in (first, second) and (third, HELD_OTHER) in tick.claim_held
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert walls.read(*ROUTE) is None and walls.read(*OTHER_ROUTE) == other_wall
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [({first, second} - {probe}).pop()]
        assert [held for held in tick.claim_held if held[1].startswith('route_closed')] == [(third, HELD_OTHER)]

        # The other profile's probe comes due: this dispatcher launches its card as that route's probe, and the
        # answer its own account gives reopens its route.
        provider.walled.discard(ACCOUNTS[OTHER])
        clock.advance(other_wall['next_probe_at'] - clock.now)
        assert third in spawned(kb.dispatch_once(conn))
        assert walls.read(*OTHER_ROUTE)['probe']['task_id'] == third
        run = run_card(conn, launcher, monkeypatch, third, provider)
        assert (run.provider_calls, run.exit_code, kb.get_task(conn, third).status) == (2, 0, 'done')
        assert not walls.any_closed() and len(ops.notices()) == 2


def test_two_boards_of_one_profile_share_the_wall_of_their_route(quota_board, launcher, monkeypatch, ops):
    """The wall is under the kanban root, which the boards of the host share: the same profile on the same provider
    and model uses one credential on every board, so one wall, one notice and one probe serve them all."""
    monkeypatch.delenv('HERMES_KANBAN_DB')  # each board at its own path under the shared root
    kb.create_board('second')
    with kb.connect_closing() as conn, kb.connect_closing(board='second') as other:
        clock = Clock(monkeypatch)
        task = enrol(conn, launcher)
        run_card(conn, launcher, monkeypatch, task.id, refusing(GoUsageLimit))
        wall = walls.read(*ROUTE)
        assert wall['closed_by'] == {'task_id': task.id, 'board': 'default'}

        card = ready_card(other, 'Same profile, another board', assignee=PROFILE)
        tick = kb.dispatch_once(other, board='second')
        assert tick.claim_held == [(card, HELD)] and not tick.spawned, tick
        assert [wait['status'] for wait in quota_waits(other, card)] == ['open']
        assert_waiting(conn, kb.dispatch_once(conn), [task.id])
        assert walls.walls() == [wall] and len(ops.notices()) == 1

        # At the hour the first board to tick launches the route's one probe; the other board waits for it.
        clock.advance(3600)
        assert spawned(kb.dispatch_once(other, board='second')) == [card]
        assert walls.read(*ROUTE)['probe'] == {'task_id': card, 'board': 'second', 'at': clock.now}
        tick = kb.dispatch_once(conn)
        assert tick.claim_held == [(task.id, HELD)] and not tick.spawned, tick
        assert len(walls.walls()) == 1 and len(ops.notices()) == 1


def test_dispatcher_and_worker_name_the_same_route_when_the_card_is_of_another_profile(
        quota_board, launcher, monkeypatch):
    """The dispatcher runs as one profile and launches the card as another. The route is the card's: the profile
    it is assigned to, in the spelling the launch gives ``-p``. The worker gets that spelling from the launch, and
    reaches the same one from its own home when the launch does not say."""
    profile_home = second_profile(quota_board)
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        pinned = ready_card(conn, 'Pinned on the card', assignee='Pessoal')
        unpinned = ready_card(conn, 'Left to its profile', assignee=OTHER, pinned=False)
        own = ready_card(conn, "Left to the dispatcher's profile", assignee=PROFILE, pinned=False)
        assert walls.card_route(conn, kb.get_task(conn, own)) == (PROFILE, 'openai-codex', 'gpt-6-astra')
        assert sorted(spawned(kb.dispatch_once(conn))) == sorted([pinned, unpinned, own])
        for task_id in (pinned, unpinned):
            route = walls.card_route(conn, kb.get_task(conn, task_id))
            argv, env = launch_of(launcher, task_id)
            assert route == OTHER_ROUTE and json.loads(env[walls.ROUTE_ENV]) == list(route)
            assert (option(argv[:argv.index('chat')], '-p'), env['HERMES_PROFILE']) == (OTHER, OTHER)
            assert env['HERMES_HOME'] == str(profile_home)

            # The worker's side, in the home the launch gave it.
            worker = _agent()
            monkeypatch.setenv('HERMES_HOME', env['HERMES_HOME'])
            monkeypatch.setenv('HERMES_KANBAN_TASK', task_id)
            monkeypatch.setenv(walls.ROUTE_ENV, env[walls.ROUTE_ENV])
            assert walls.worker_route(worker) == route
            monkeypatch.delenv(walls.ROUTE_ENV)
            assert walls.worker_route(worker) == route, 'from its own home, with no word from the launch'
            switched = _agent()
            switched.provider, switched.model, switched._fallback_activated = 'other-provider', 'other-model', True
            assert walls.worker_route(switched) == (OTHER, 'other-provider', 'other-model')
            assert walls.on_quota_error(worker, GoUsageLimit()) is True

            # The dispatcher's side, in its own home: the wall the worker wrote is the one it asks for.
            monkeypatch.setenv('HERMES_HOME', str(quota_board))
            monkeypatch.delenv('HERMES_KANBAN_TASK')
            assert walls.closed(*route)['closed_by']['task_id'] == task_id
            assert walls.read(PROFILE, *route[1:]) is None, "not a wall of the dispatcher's own profile"
            assert kb._route_wall_hold(conn, task_id, dry_run=True) == HELD_OTHER
            assert kb._route_wall_hold(conn, own, dry_run=True) is None
            clock.advance(3600)  # the hour of the route's probe
            assert kb._route_wall_hold(conn, task_id, dry_run=True) is None
            assert kb._route_wall_probe(conn, kb.get_task(conn, task_id)) is None, 'this launch is the probe'
            assert walls.read(*route)['probe']['task_id'] == task_id, 'the probe is marked on the wall the worker wrote'
            assert walls.reopen(*route)
            clock.advance(DAY)  # long open: the next card's refusal is a new wall, closed by that card


ACCOUNT_NAMED = [
    pytest.param(GoUsageLimit(), 'wrk_quota_wall_suite', id='opencode-go-body'),
    pytest.param(GoUsageLimitFromSDK(), 'wrk_quota_wall_suite', id='only-in-the-http-answer'),
    pytest.param(CodexUsageLimit(), None, id='codex-names-no-account'),
    pytest.param(ProviderRateLimit(), None, id='plain-429'),
]


@pytest.mark.parametrize('error, account', ACCOUNT_NAMED)
def test_the_account_the_provider_names_is_kept_and_decides_nothing(quota_board, monkeypatch, error, account):
    assert walls.quota_account(error) == account
    # The same route whatever account is named: the account is no part of it.
    Clock(monkeypatch)
    wall, opened = walls.close(*ROUTE, limit='weekly', account=account, task_id='t_a')
    assert opened and wall['account'] == account and walls.read(*ROUTE) == wall
    again, opened = walls.close(*ROUTE, limit='weekly', account='wrk_another_account', task_id='t_b')
    assert not opened and again == wall, 'a second card on the wall changes nothing, whatever account it was refused on'


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
    pytest.param(_with_body(resets_at='2026-10-15T12:54:00'),
                 {'limit': 'usage_limit_reached', 'reset_at': 1_792_068_840.0}, id='iso-with-no-zone-is-utc'),
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
    return SimpleNamespace(provider=EXECUTOR_PROVIDER, model=EXECUTOR_MODEL, _fallback_activated=False,
                           _has_pending_fallback=lambda: pending_fallback)


def test_only_a_cards_own_worker_with_no_fallback_left_closes_a_route(quota_board, monkeypatch):
    # Any session leaves the turn on a usage window; a session that is not a card's worker closes nothing.
    assert walls.on_quota_error(_agent(), GoUsageLimit()) is True
    assert walls.on_quota_error(_agent(), ShortThrottle()) is False
    assert not walls.any_closed()

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    # A fallback provider the operator configured still has the turn: the route is not closed under it.
    assert walls.on_quota_error(_agent(pending_fallback=True), GoUsageLimit()) is True and not walls.any_closed()

    # The dispatcher's spelling of the route wins over the agent's while the worker runs its primary model.
    monkeypatch.setenv(walls.ROUTE_ENV, json.dumps(['Default', 'OpenCode-Go', 'DeepSeek-V4.1-Flash']))
    worker = _agent()
    worker.provider, worker.model = 'opencode', 'deepseek/deepseek-v4.1-flash'
    assert walls.on_quota_error(worker, GoUsageLimit()) is True
    wall = walls.read(*ROUTE)
    assert wall['closed_by']['task_id'] == 't_worker' and walls.unannounced() == [wall]
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_other')
    assert walls.on_quota_error(_agent(), GoUsageLimit()) is True
    assert walls.read(*ROUTE) == wall and walls.unannounced() == [wall], 'a second card on the wall changes nothing'


def test_only_a_call_begun_after_the_wall_and_answered_reopens_the_route(quota_board, monkeypatch):
    clock = Clock(monkeypatch)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    # A call in flight when the wall was met: its late answer is not proof that the window ended.
    early = _agent()
    assert walls.answered(early) is False
    clock.advance(5)
    walls.close(*ROUTE, limit='weekly')
    clock.advance(1)
    early.session_api_calls = 1
    assert walls.answered(early) is False and walls.read(*ROUTE) is not None
    # The same worker's next call began after the wall. With no answer counted it proves nothing; answered, it
    # is the proof, whatever time the provider had said the window would reset at.
    clock.advance(20)
    assert walls.answered(early) is False and walls.read(*ROUTE) is not None
    clock.advance(20)
    early.session_api_calls = 2
    assert walls.answered(early) is True and walls.read(*ROUTE) is None

    walls.close(*ROUTE, limit='monthly')
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


# ── One launch probes a route ───────────────────────────────────────────
#
# The probe used to be marked with an unconditional rewrite of the wall, after the card was claimed: two launchers
# that both read the route as due both launched, and the last one to write was the probe. On the production host
# two things launch a card: the gateway's dispatcher (one tick per board, each under its board's own lock) and the
# support desk's start of an approved ticket (`start_approved` of deploy/nfos-reviewer-control.py in the NFOS
# repository, read at 08ece04), which runs in a process of its own.

def start_approved(conn, task_id, board=None):
    """The support desk starting an approved ticket: the runtime calls `start_approved` makes, in its order, under
    the board's dispatch lock as it takes it. It asks nothing of the route: it claims the card and launches it."""
    with kb._dispatch_tick_lock(kb.kanban_db_path(board=board)) as held:
        assert held
        task = kb.get_task(conn, task_id)
        assert task.status == 'ready' and not kb._nfos_decision_open(conn, task_id)
        assert not kb.check_respawn_guard(conn, task_id)
        workspace = kb.resolve_workspace(task, board=board)
        lease, _ = kb._try_acquire_workspace_lease(workspace, task_id=task_id)
        claimed = kb.claim_task(conn, task_id)
        assert lease is not None and claimed is not None
        kb.set_workspace_path(conn, task_id, str(workspace))
        pid = kb._default_spawn(claimed, str(workspace), board=board)
        kb._set_worker_pid(conn, task_id, int(pid))
        kb._promote_workspace_lease(lease, int(pid))
        return pid


def test_the_probe_of_a_due_route_is_taken_by_one_of_the_launchers_that_ask_at_once(quota_board, monkeypatch):
    clock = Clock(monkeypatch)
    walls.close(*ROUTE, limit='weekly', task_id='t_a')
    clock.advance(300)
    assert walls.closed(*ROUTE) is None, 'due: every launcher that looks now sees a route to probe'
    together = threading.Barrier(8)

    def ask(number):
        together.wait(timeout=30)
        return walls.take_probe(*ROUTE, task_id=f't_{number}', board=f'board-{number}')

    with ThreadPoolExecutor(max_workers=8) as launchers:
        answers = list(launchers.map(ask, range(8)))
    (taken,) = [answer for answer in answers if answer]
    assert answers.count(False) == 7, 'the others are held by the wall after all'
    assert walls.read(*ROUTE)['probe'] == taken['probe'] and walls.closed(*ROUTE) is not None
    winner = taken['probe']['task_id']
    # The probe is of the launch that took it, not of its id: a second launcher of the same card is held as well.
    assert walls.take_probe(*ROUTE, task_id=winner, board=taken['probe']['board']) is False
    assert walls.read(*ROUTE)['probe'] == taken['probe']
    # The intake asks for the same route a moment later: its entry is not offered.
    entry = {'coordination': {'quota': {'route': list(ROUTE)}}}
    assert walls.entry_turn(entry, PROFILE, 'req_1') is False and walls.read(*ROUTE)['probe'] == taken['probe']

    # A launch that did not take the probe changes nothing when it meets the wall; the probe's refusal is the verdict.
    loser = next(f't_{number}' for number in range(8) if f't_{number}' != winner)
    before = walls.read(*ROUTE)
    assert walls.close(*ROUTE, limit='weekly', task_id=loser) == (before, False)
    refused, opened = walls.close(*ROUTE, limit='weekly', task_id=winner)
    assert not opened and (refused['probes'], refused['probe'], refused['next_probe_at']) == (1, None, clock.now + 600)
    # That verdict moved the schedule: a launcher that had read the route as due before it is held too.
    assert walls.take_probe(*ROUTE, task_id=loser) is False and walls.read(*ROUTE) == refused
    clock.advance(600)
    assert walls.entry_turn(entry, PROFILE, 'req_1') == ROUTE, 'due again: the intake takes this one'
    assert walls.take_probe(*ROUTE, task_id=loser) is False
    # A route with no wall holds nobody, and asking for its probe does not close it.
    assert walls.reopen(*ROUTE) and walls.take_probe(*ROUTE, task_id=loser) is None and not walls.any_closed()


def test_of_two_gateways_offering_the_same_inbox_entry_one_is_the_probe_and_keeps_it(quota_board, monkeypatch):
    """Two gateways serving one profile both read the route as due for the same entry. A probe known by the id
    of what it launches made both of them its owner: the one that then lost the claim of the entry gave the probe
    back, and it was the other's, with its turn running."""
    clock = Clock(monkeypatch)
    walls.close(*ROUTE, limit='usage_limit_reached', request_id='req_1')
    entry = {'coordination': {'quota': {'route': list(ROUTE)}}}
    clock.advance(300)
    read_the_wall, theirs = walls.entry_held, []

    def read_then_the_other_gateway_offers(*route, **kwargs):
        held = read_the_wall(*route, **kwargs)
        if not theirs:
            assert held is None, 'this gateway saw the route due'
            theirs.append(None)
            theirs[0] = walls.entry_turn(entry, PROFILE, 'req_1')
        return held

    monkeypatch.setattr(walls, 'entry_held', read_then_the_other_gateway_offers)
    ours = walls.entry_turn(entry, PROFILE, 'req_1')
    monkeypatch.setattr(walls, 'entry_held', read_the_wall)
    assert theirs == [ROUTE], 'the gateway that asked first offers the entry: its turn is the probe'
    assert ours is False, 'the other is held by the wall: it offers nothing, so it has no claim to lose'
    probe = walls.read(*ROUTE)['probe']
    assert probe == {'task_id': 'req_1', 'board': None, 'at': clock.now}
    # A card of the route is held beside that probe, and the probe's refusal is the one verdict.
    assert walls.take_probe(*ROUTE, task_id='t_card') is False and walls.read(*ROUTE)['probe'] == probe
    refused, opened = walls.close(*ROUTE, limit='usage_limit_reached', request_id='req_1')
    assert not opened and (refused['probes'], refused['probe'], refused['next_probe_at']) == (1, None, clock.now + 600)


def test_an_inbox_entry_waits_for_the_reset_time_and_the_probe_of_the_hour_is_left_to_the_cards(
        quota_board, monkeypatch):
    """The probe of an inbox entry is the whole turn of its request, and each refused one leaves the wake of the
    request once more in the session of the topic. With a reset time ahead the entry waits for it; a card of the
    same route is still launched every hour, and the notices say of each what happens to it."""
    clock = Clock(monkeypatch)
    start, reset = clock.now, clock.now + 6 * DAY
    entry = {'coordination': {'quota': {'route': list(ROUTE)}}}
    wall, _ = walls.close(*ROUTE, limit='usage_limit_reached', reset_at=reset, request_id='req_1')
    assert walls.open_at(wall) == start + 3600 and walls.entry_held(*ROUTE) == wall

    for hour in (1, 2, 3):
        clock.advance(3600)
        assert walls.closed(*ROUTE) is None, 'due: the launch of a card of this route is its probe'
        assert walls.entry_turn(entry, PROFILE, 'req_1') is False, 'the entry is held all the same'
        assert walls.read(*ROUTE)['probe'] is None, 'and took no probe'
        assert walls.take_probe(*ROUTE, task_id='t_card')['probe']['task_id'] == 't_card'
        refused, opened = walls.close(*ROUTE, limit='usage_limit_reached', reset_at=reset, task_id='t_card')
        assert (refused['probes'], refused['probe'], refused['next_probe_at']) == (hour, None, clock.now + 3600)

    # The first notice of this wall promises no probe: no entry is one, and it cannot tell whether a card waits.
    opening = walls.notice(wall)
    assert walls.ENTRIES_RESUMED in opening and 'sonda' not in opening and 'a cada 60 min' not in opening
    of_a_card = walls.notice(dict(wall, closed_by={'task_id': 't_a', 'board': None}))
    assert 'Um lançamento sonda a rota a cada 60 min e nessa hora' in of_a_card
    assert walls.ENTRIES_RESUMED not in of_a_card

    def daily(cards, entries, standing=wall):
        return walls.still_closed(standing, {'cards': cards, 'entries': entries, 'unread': 0})

    # The day's notice knows what waits: of the entries it says when they are resumed, and a probe is a card's.
    assert walls.ENTRIES_RESUMED in daily(0, 2) and 'sonda' not in daily(0, 2)
    both = daily(1, 2)
    assert walls.ENTRIES_RESUMED in both and 'O lançamento de um card sonda a rota a intervalos de até 60 min' in both
    assert 'Um lançamento sonda a rota a intervalos de até 60 min' in daily(1, 0)
    assert walls.ENTRIES_RESUMED not in daily(1, 0)
    no_reset = dict(wall, reset_at=None)
    assert 'Um lançamento sonda a rota a intervalos de até 60 min' in daily(0, 2, no_reset)
    assert walls.ENTRIES_RESUMED not in daily(0, 2, no_reset)

    # One second before the reset time the entry is still held. At that time its offer is the probe of the route.
    clock.advance(reset - 1 - clock.now)
    assert walls.closed(*ROUTE) is None and walls.entry_turn(entry, PROFILE, 'req_1') is False
    clock.advance(1)
    assert walls.entry_turn(entry, PROFILE, 'req_1') == ROUTE and walls.read(*ROUTE)['probe']['task_id'] == 'req_1'
    assert 'Um lançamento sonda a rota a intervalos de até 60 min' in daily(0, 2)
    assert walls.ENTRIES_RESUMED not in daily(0, 2)
    # Refused there with no reset time: the wall is now the only clock the entry has, and it probes at its spacing.
    refused, _ = walls.close(*ROUTE, limit='usage_limit_reached', request_id='req_1')
    assert (refused['reset_at'], refused['probe'], refused['next_probe_at']) == (None, None, clock.now + 3600)
    clock.advance(3600)
    assert walls.entry_turn(entry, PROFILE, 'req_1') == ROUTE


def test_a_writer_of_a_wall_does_not_wait_for_ever_for_its_lock(quota_board, monkeypatch, caplog):
    """The intake asks for the lock on the gateway's event loop. A holder that stopped with the lock in hand (a
    frozen process) is waited for a bounded time, and the writer then goes on without it and says so."""
    import fcntl
    clock = Clock(monkeypatch)
    walls.close(*ROUTE, limit='weekly', task_id='t_a')
    entry = {'coordination': {'quota': {'route': list(ROUTE)}}}
    clock.advance(300)
    monkeypatch.setattr(walls, 'LOCK_WAIT_SECONDS', 0.2)

    def not_given():
        return [record for record in caplog.records if 'was not given' in record.getMessage()]

    with caplog.at_level('WARNING', logger=walls.logger.name):
        with open(walls._path(*ROUTE, walls.LOCK), 'a+b') as stopped:
            fcntl.flock(stopped.fileno(), fcntl.LOCK_EX)
            began = time.monotonic()
            assert walls.entry_turn(entry, PROFILE, 'req_1') == ROUTE
            assert time.monotonic() - began >= 0.2 and len(not_given()) == 1, 'waited for the lock, and not for ever'
        assert walls.read(*ROUTE)['probe']['task_id'] == 'req_1'
        walls.release_probe(*ROUTE, task_id='req_1')
        assert walls.read(*ROUTE)['probe'] is None and len(not_given()) == 1, 'a free lock is taken at once'


def test_of_two_dispatchers_that_see_the_route_due_one_launches_the_probe(quota_board, launcher, monkeypatch, ops):
    """Two boards of one profile, each ticked under its own lock. The first board's dispatcher reads the route as
    due for its card, and before it claims the card the whole tick of the second board runs."""
    monkeypatch.delenv('HERMES_KANBAN_DB')  # each board at its own path under the shared root
    kb.create_board('second')
    with kb.connect_closing() as conn, kb.connect_closing(board='second') as other:
        clock = Clock(monkeypatch)
        task = enrol(conn, launcher)
        run_card(conn, launcher, monkeypatch, task.id, refusing(GoUsageLimit))
        card = ready_card(other, 'Same profile, another board', assignee=PROFILE)
        assert kb.dispatch_once(other, board='second').claim_held == [(card, HELD)]
        assert_waiting(conn, kb.dispatch_once(conn), [task.id])
        assert len(ops.notices()) == 1

        clock.advance(3600)
        read_the_wall, raced = kb._route_wall_hold, []

        def read_then_the_other_board_ticks(conn_, task_id, **kwargs):
            held = read_the_wall(conn_, task_id, **kwargs)
            if task_id == task.id and not raced:
                assert held is None, 'this dispatcher saw the route due'
                raced.append(kb.dispatch_once(other, board='second'))
            return held

        monkeypatch.setattr(kb, '_route_wall_hold', read_then_the_other_board_ticks)
        launches = len(launcher.launches)
        tick = kb.dispatch_once(conn)
        monkeypatch.setattr(kb, '_route_wall_hold', read_the_wall)
        (theirs,) = raced
        assert spawned(theirs) == [card], 'the second board launched the probe'
        assert not tick.spawned and tick.claim_held == [(task.id, HELD)], tick
        assert len(launcher.launches) == launches + 1, 'one launch on the closed route, not two'
        assert walls.read(*ROUTE)['probe'] == {'task_id': card, 'board': 'second', 'at': clock.now}
        held = kb.get_task(conn, task.id)
        assert (held.status, held.claim_lock, held.consecutive_failures) == ('ready', None, 0)
        assert kb.count_running_tasks(conn) == 0, 'the card that lost the probe holds no slot'

        # The probe is refused: one verdict, the next probe an hour on, no second notice. Then it is answered, and
        # the card the first board held is launched: nothing of the lost probe stayed on it.
        refused = run_card(other, launcher, monkeypatch, card, refusing(GoUsageLimit))
        assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['probes'], wall['probe'], wall['next_probe_at']) == (1, None, clock.now + 3600)
        assert kb.dispatch_once(other, board='second').rate_limited == [card]
        assert_waiting(conn, kb.dispatch_once(conn), [task.id])
        clock.advance(3600)
        assert spawned(kb.dispatch_once(conn)) == [task.id]
        run_card(conn, launcher, monkeypatch, task.id, answered_once, judge_reply=CONTINUE)
        assert not walls.any_closed() and len(ops.notices()) == 1
        assert spawned(kb.dispatch_once(other, board='second')) == [card]


def test_a_launch_made_outside_the_dispatcher_beside_the_probe_leaves_the_wall_to_the_probe(
        quota_board, launcher, monkeypatch, ops):
    """The desk starts an approved ticket in the same moment the dispatcher's tick probes the route. The desk's
    launch asks nothing of the wall, so two workers go out. That is harmless: each leaves on its first refused
    call, only the probe's refusal is the verdict of the route, and an answer to either reopens it."""
    wall_of = GoUsageLimitFromSDK  # a named limit and no reset time: the first probe is due in 300 s
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, wall_of)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        assert len(ops.notices()) == 1

        # The desk first, the dispatcher right after it.
        ticket = ready_card(conn, 'Approved ticket', assignee=PROFILE)
        clock.advance(300)
        start_approved(conn, ticket)
        assert walls.read(*ROUTE)['probe'] is None, 'that launch took nothing from the wall'
        probe = probe_of(launcher, kb.dispatch_once(conn), [first, second])
        marked = walls.read(*ROUTE)
        outside = run_card(conn, launcher, monkeypatch, ticket, refusing(wall_of))
        assert (outside.provider_calls, outside.waits.waited, outside.exit_code) == (1, 0, 75)
        assert walls.read(*ROUTE) == marked, 'the refusal of the launch that is not the probe changes nothing'
        refused = run_card(conn, launcher, monkeypatch, probe, refusing(wall_of))
        assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['probes'], wall['probe'], wall['next_probe_at']) == (1, None, clock.now + 600)
        tick = kb.dispatch_once(conn)
        assert sorted(tick.rate_limited) == sorted([ticket, probe])
        assert_waiting(conn, tick, [first, second, ticket])
        assert len(ops.notices()) == 1, 'two refusals on the same wall: no second notice'

        # The dispatcher first, the desk right after it, with the probe in flight. The quota is back: the desk's
        # worker is answered and the route reopens on that answer, whatever the probe does later.
        clock.advance(600)
        probe = probe_of(launcher, kb.dispatch_once(conn), [first, second, ticket])
        another = ready_card(conn, 'Another approved ticket', assignee=PROFILE)
        start_approved(conn, another)
        assert walls.read(*ROUTE)['probe']['task_id'] == probe, 'the probe is still the launch of the dispatcher'
        run = run_card(conn, launcher, monkeypatch, another, answered_once, judge_reply=CONTINUE)
        assert not run.results[0].get('failed') and walls.read(*ROUTE) is None and not walls.any_closed()
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert not walls.any_closed() and len(ops.notices()) == 1
        waiting = sorted({first, second, ticket} - {probe})
        assert sorted(spawned(kb.dispatch_once(conn))) == waiting, 'the route is open: the held cards go'
        for task_id in (first, second, ticket, another):
            assert kb.get_task(conn, task_id).consecutive_failures == 0


# ── The day's notice of a wall that stands ──────────────────────────────

def test_a_wall_that_stands_is_announced_once_a_day_with_how_long_and_what_waits(
        quota_board, launcher, monkeypatch, ops):
    """One notice on the first day and silence for a week is a board standing still in silence. While the wall
    stands the management topic hears of it once a day: for how long the route has been closed and how many cards
    wait for it. The account the provider named is in neither text; it is on the wall."""
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        start = clock.now
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimitFromSDK)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        (*topic, opening), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC
        assert 'Enquanto ela seguir fechada com card ou pedido esperando por ela, sai aqui um aviso por dia.' in opening
        receipt = walls._load(walls._path(*ROUTE, walls.NOTICE))
        assert (receipt['notices'], receipt['sent_at']) == (1, start)

        # The first day: the notifier ticks every hour and says nothing more.
        for _ in range(23):
            clock.advance(3600)
            assert len(ops.notices()) == 1
        clock.advance(3599)
        assert len(ops.notices()) == 1 and walls.unannounced() == []

        clock.advance(1)
        assert [walls._route(wall) for wall in walls.unannounced()] == [ROUTE]
        (_, (*topic, daily)) = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC, 'the same topic as the notice of the wall, and no other'
        assert f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} do perfil {PROFILE} segue fechada há 1 d, ' in daily
        assert 'pelo limite weekly.' in daily and 'O provedor não informou hora de reinício.' in daily
        assert 'Cards esperando por ela: 2.' in daily
        assert 'Pedidos na caixa do Principal, ainda sem card: 0.' in daily
        assert 'contagem é parcial' not in daily
        # Neither text names the account. The wall does.
        assert walls.read(*ROUTE)['account'] == 'wrk_quota_wall_suite'
        for text in (opening, daily):
            assert 'wrk_quota_wall_suite' not in text and 'conta ' not in text
        receipt = walls._load(walls._path(*ROUTE, walls.NOTICE))
        assert (receipt['notices'], receipt['sent_at'], receipt['closed_at']) == (2, clock.now, start)
        assert [(sent['chat_id'], sent['thread_id']) for sent in receipt['deliveries']] == [MANAGEMENT_TOPIC]

        # Not twice on the same day: not on the next ticks, not from a gateway that restarts, not from a second
        # gateway of the host. The receipt is beside the wall.
        clock.advance(600)
        assert len(ops.notices()) == 2 and Operations().notices() == [] and walls.unannounced() == []
        assert not walls.claim_notice(walls.read(*ROUTE))
        clock.advance(DAY - 601)
        assert len(ops.notices()) == 2 and Operations().notices() == []

        # The second day, from the gateway that restarted. A third card waits by now.
        third = ready_card(conn, 'Order filter', assignee=PROFILE)
        # The wall as its latest probe left it, refused this instant; then the hold the dispatcher asks for.
        assert walls.take_probe(*ROUTE, task_id=first) and walls.close(*ROUTE, limit='weekly', task_id=first)
        assert kb._route_wall_hold(conn, third) == HELD
        clock.advance(1)
        restarted = Operations()
        (*topic, second_day), = restarted.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC and 'segue fechada há 2 d, ' in second_day
        assert 'Cards esperando por ela: 3.' in second_day
        assert ops.notices() == [(*MANAGEMENT_TOPIC, opening), (*MANAGEMENT_TOPIC, daily)], 'one gateway sent it'

        # The route reopens: nothing more is said, that day or the days after.
        assert walls.reopen(*ROUTE)
        for _ in range(4):
            clock.advance(DAY)
            assert len(ops.notices()) == 2 and len(restarted.notices()) == 1 and walls.unannounced() == []


def test_the_days_notice_that_no_topic_took_is_tried_again_and_a_new_wall_starts_its_own_days(
        quota_board, monkeypatch):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        wall, _ = walls.close(*ROUTE, limit='weekly', account='wrk_quota_wall_suite', reset_at=clock.now + 7 * DAY,
                              task_id='t_a')
        card = ready_card(conn, 'Held by the route', assignee=PROFILE)
        assert kb._route_wall_hold(conn, card) == HELD, 'a card waits for the route'
        ops = Operations()
        (*_, opening), = ops.notices()
        assert 'wrk_quota_wall_suite' not in opening
        clock.advance(DAY)
        # A notifier takes the day's notice and dies before sending it: the claim lapses and the notice goes out.
        claim = walls.claim_notice(walls.read(*ROUTE))
        assert claim['notices'] == 1 and ops.notices() == [(*MANAGEMENT_TOPIC, opening)]
        clock.advance(walls.PROBE_LEASE_SECONDS)
        (_, (*_, daily)) = ops.notices()
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(wall['reset_at']))
        assert 'segue fechada há 1 d 15 min' in daily and f'O reinício informado pelo provedor é {when}.' in daily
        assert 'Cards esperando por ela: 1.' in daily and 'wrk_quota_wall_suite' not in daily
        # The route reopens and stays open for long: its next wall is a new one, with its own notice and its own
        # days.
        assert walls.reopen(*ROUTE)
        clock.advance(3 * DAY)
        assert len(ops.notices()) == 2
        walls.close(*ROUTE, limit='weekly', task_id='t_b')
        assert kb._route_wall_hold(conn, card) == HELD
        assert len(ops.notices()) == 3 and 'fechada para o perfil' in ops.sent[-1][2]
        clock.advance(DAY - 1)
        assert len(ops.notices()) == 3
        clock.advance(1)
        assert len(ops.notices()) == 4 and 'segue fechada há 1 d, ' in ops.sent[-1][2]


def test_a_wall_nothing_waits_for_any_more_is_not_announced_again_until_something_waits(
        quota_board, launcher, monkeypatch, ops):
    """The cards of a route on its weekly wall are moved to another model to get away from it, and are delivered
    there. The old wall stands with nothing behind it: no launch probes it and it holds nobody. A notice a day
    about it would go on for as long as the file is there, saying that a launch probes a route no launch probes."""
    week, provider = go_window(7 * DAY), Accounts()
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second = (ready_card(conn, title, assignee=PROFILE) for title in ('Order total', 'Order sorting'))
        assert sorted(spawned(kb.dispatch_once(conn))) == sorted([first, second])
        for task_id in (first, second):
            run_card(conn, launcher, monkeypatch, task_id, refusing(week))
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        (*_, opening), = ops.notices()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET model_override='other-model' WHERE id IN (?,?)", (first, second))
        clock.advance(300)  # past the per-card wait of the quota exit
        assert sorted(spawned(kb.dispatch_once(conn))) == sorted([first, second])
        for task_id in (first, second):
            (wait,) = quota_waits(conn, task_id)
            assert (wait['status'], wait['outcome']) == ('withdrawn', 'route_changed')
            delivered = run_card(conn, launcher, monkeypatch, task_id, provider)
            assert delivered.exit_code == 0 and kb.get_task(conn, task_id).status == 'done'
        assert not kb.dispatch_once(conn).spawned
        standing = walls.read(*ROUTE)
        assert standing is not None and standing['probe'] is None, 'the old wall stands, unprobed'

        # Five days: the wall is due for its day's notice at every tick of the notifier, and nothing waits for it.
        for day in range(1, 6):
            clock.advance(DAY)
            assert [walls._route(wall) for wall in walls.unannounced()] == [ROUTE]
            assert ops.notices() == [(*MANAGEMENT_TOPIC, opening)], day
            assert Operations().notices() == [], 'nor from a gateway that restarts'
        assert walls.read(*ROUTE) == standing, 'nothing wrote on the wall'
        count = walls.waiting(standing, owner_of=lambda payload: PROFILE)
        assert count == {'cards': 0, 'entries': 0, 'unread': 0} and walls.still_closed(standing, count) is None
        # A board that could not be read leaves the count unknown: that is said, and nothing is said of a probe.
        unknown = walls.still_closed(standing, dict(count, unread=1))
        assert 'contagem é parcial' in unknown and 'sonda a rota' not in unknown

        # A card pinned to the old route arrives. The route is long due: its launch is the probe, the provider
        # refuses it, and from then on a card waits behind the wall. The day's notice is owed and goes out.
        back = ready_card(conn, 'Pinned to the closed route', assignee=PROFILE)
        assert spawned(kb.dispatch_once(conn)) == [back]
        assert walls.read(*ROUTE)['probe']['task_id'] == back
        refused = run_card(conn, launcher, monkeypatch, back, refusing(week))
        assert (refused.provider_calls, refused.exit_code) == (1, 75)
        tick = kb.dispatch_once(conn)
        assert tick.rate_limited == [back] and tick.claim_held == [(back, HELD)], tick
        clock.advance(walls.PROBE_LEASE_SECONDS)  # the notifier's last look at the wall has lapsed
        (_, (*topic, daily)) = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC and 'segue fechada há 5 d ' in daily
        assert 'Cards esperando por ela: 1.' in daily and 'Um lançamento sonda a rota a intervalos de até 60 min' in daily
        assert len(ops.notices()) == 2, 'and not again that day'

        # A card that stands in no lane the dispatcher launches from does not probe the route: it is not counted
        # as waiting for it, whatever the registry still holds of it.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (back,))
        assert [wait['status'] for wait in quota_waits(conn, back)] == ['open']
        assert walls.waiting(walls.read(*ROUTE), owner_of=lambda payload: PROFILE)['cards'] == 0
        clock.advance(DAY)
        assert len(ops.notices()) == 2, 'and with it blocked nothing waits: no notice the next day'


def test_a_notice_listed_for_a_wall_is_not_taken_once_the_route_has_reopened(quota_board, monkeypatch):
    """The notifier lists the walls with a notice due and then takes each. A probe answered in between reopens the
    route: the notice of a closed route must not go out for it, and no notice of a reopening exists to unsay it."""
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        walls.close(*ROUTE, limit='weekly', task_id='t_a')
        card = ready_card(conn, 'Held by the route', assignee=PROFILE)
        assert kb._route_wall_hold(conn, card) == HELD
        ops = Operations()
        assert len(ops.notices()) == 1
        clock.advance(DAY)
        (listed,) = walls.unannounced()
        assert walls.reopen(*ROUTE)
        assert walls.claim_notice(listed) is False and len(ops.notices()) == 1
        # Nor is it taken for a new wall of the route: that one has a notice of its own.
        clock.advance(DAY)
        new, opened = walls.close(*ROUTE, limit='weekly', task_id='t_b')
        assert opened and walls.claim_notice(listed) is False
        assert walls.claim_notice(new)['notices'] == 0, "the new wall's own notice is the one due"


# ── What a probe spends of the card that probes ─────────────────────────
#
# A card in rework the Principal escalated carries a balance for its whole lineage of runs: goal turns, iterations
# and seconds of runtime, summed over the runs since the escalation (`nfos_principal_review.escalation_usage`). The
# turn and the iteration are counted before the provider is asked, and a run's seconds whatever it did. On a closed
# route such a card is launched once an hour as the probe, and each launch the provider refuses used to count one
# turn, one iteration and the seconds of the launch: on its twentieth probe the claim found the balance of goal
# turns spent, blocked the card and asked the Principal about a balance nothing had been done with.

def run_owned_card(conn, launcher, monkeypatch, task_id, answer, judge_reply=None):
    """As `run_card`, with the run handed to this process for as long as the worker runs in it. The suite's
    worker process is a stand-in child, and what a run spends of its lineage is written only by the process that
    owns the run (`nfos_principal_review._owned_worker_run`)."""
    child = tuple(conn.execute('SELECT worker_pid,worker_started_at,current_run_id FROM tasks WHERE id=?',
                               (task_id,)).fetchone())

    def hand_to(pid, started_at):
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?', (pid, started_at, task_id))
            conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?', (pid, child[2]))

    hand_to(os.getpid(), kb._process_start_time(os.getpid()))
    try:
        run = run_executor(monkeypatch, *launch_of(launcher, task_id), answer=answer, judge_reply=judge_reply)
    finally:
        hand_to(*child[:2])
    launcher.exit(child[0], run.exit_code)
    return run


def test_a_probe_the_provider_refuses_spends_nothing_of_the_balance_of_the_escalated_card_that_probes(
        quota_board, launcher, monkeypatch):
    from hermes_cli import goals, nfos_delivery as delivery
    from hermes_cli.nfos_workspace_repair import execution_budget
    assert kb._process_start_time(os.getpid()) is not None, 'the owner of a run is told by its start time'
    config = dict(copy.deepcopy(PROFILE_CONFIG), agent={'max_turns': 90})
    (quota_board / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    week, loop_turns, run_the_loop = go_window(7 * DAY), [], goals.run_kanban_goal_loop

    def goal_loop(*args, **kwargs):
        loop_turns.append(kwargs['max_turns'])
        return run_the_loop(*args, **kwargs)

    monkeypatch.setattr(goals, 'run_kanban_goal_loop', goal_loop)
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        task = enrol(conn, launcher)
        # The lineage as the one live card that has one on the production boards carries it (read on 2026-10-12):
        # level 1, applied, 9,900 s of runtime. The card's goal loop has its 20 turns.
        with kb.write_txn(conn):
            state = json.loads(delivery.get_workflow(conn, task.id)['state_json'])
            state['worker_escalation'] = dict(
                level=1, model='gpt-6-luna', reasoning_effort='max', reason='Rework the Principal escalated',
                source_run_id=task.current_run_id, first_run_id=task.current_run_id, run_id=task.current_run_id,
                status='applied')
            conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (json.dumps(state), task.id))
            conn.execute('UPDATE tasks SET goal_max_turns=20,max_runtime_seconds=9900 WHERE id=?', (task.id,))

        def balance():
            left = execution_budget(conn, kb.get_task(conn, task.id))
            return left['remaining_goal_turns'], left['remaining_iterations'], left['remaining_runtime_seconds']

        def probe_refused():
            clock.advance(40)  # from the launch to the provider's refusal of its first call
            refused = run_owned_card(conn, launcher, monkeypatch, task.id, refusing(week))
            assert (refused.turns, refused.provider_calls, refused.exit_code) == (1, 1, 75)
            assert kb.dispatch_once(conn).rate_limited == [task.id]
            return json.loads(conn.execute('SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                           (task.id,)).fetchone()[0])['escalation_usage']

        assert balance() == (20, 90, 9900)
        usage = probe_refused()
        assert walls.read(*ROUTE)['closed_by']['task_id'] == task.id
        assert balance() == (20, 90, 9900), 'the launch that met the wall spent nothing of the balance'
        # The run's record says what was counted before the provider was asked, and that the turn was at its first
        # call when the worker left on the wall.
        assert (usage['turns'], usage['iterations'], usage['turn_calls']) == (1, 1, 1)

        # A day on the wall: the card is the probe of every hour.
        for hour in range(1, 25):
            clock.advance(walls.read(*ROUTE)['next_probe_at'] - clock.now)
            tick = kb.dispatch_once(conn)
            assert spawned(tick) == [task.id] and not tick.claim_held, (hour, tick)
            usage = probe_refused()
            assert balance() == (20, 90, 9900), hour
            assert (usage['turns'], usage['iterations'], usage['turn_calls']) == (1, 1, 1), hour
        assert loop_turns == [20] * 25, 'every launch found the whole allowance of the goal loop'
        outcomes = [row['outcome'] for row in conn.execute('SELECT outcome FROM task_runs ORDER BY id')]
        assert outcomes == ['rate_limited'] * 25
        card = kb.get_task(conn, task.id)
        assert (card.status, card.block_kind, card.consecutive_failures) == ('ready', None, 0)
        assert not conn.execute('SELECT 1 FROM nfos_decisions').fetchone(), 'nobody was asked about its balance'
        assert 'nfos_execution_budget_exhausted' not in event_kinds(conn)

        # The quota is back: the probe of the next hour is answered and works with the allowance it had before the
        # wall. That run did work, and what it did counts: the turn the provider answered and the run's seconds.
        # The turn it then left on (a throttle on that turn's only call) does not.
        clock.advance(walls.read(*ROUTE)['next_probe_at'] - clock.now)
        assert spawned(kb.dispatch_once(conn)) == [task.id]
        clock.advance(40)
        run = run_owned_card(conn, launcher, monkeypatch, task.id, answered_once, judge_reply=CONTINUE)
        assert not run.results[0].get('failed') and (run.turns, run.exit_code) == (2, 75)
        assert walls.read(*ROUTE) is None and loop_turns[-1] == 20
        assert kb.dispatch_once(conn).rate_limited == [task.id]
        usage = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                        (task.id,)).fetchone()[0])['escalation_usage']
        assert (usage['turns'], usage['turn_calls']) == (2, 1)
        turns, _, seconds = balance()
        assert (turns, seconds) == (19, 9900 - 40)
