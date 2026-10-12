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
  the provider gives one, and one notice goes out for it, from the gateway's notifier to the management topic,
  whatever chat the card that met the wall was born in;
* while it is closed no card of that route is launched: the cards hold no slot, count no failure and stand in the
  wait registry; a card of another route is launched as always, and a card that leaves the route, or that another
  hold takes, stops being said to wait for it;
* the route reopens at the reset time the provider gave, or by one probe at spaced intervals when it gave none,
  and a probe the provider refuses does not send a second notice; a route that closes again at once is the same
  wall going on;
* a throttle that resets inside the client's own wait is retried as before;
* no other provider or model is used in place of a closed route.
"""
import asyncio
import copy
import json
import os
import time
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
        assert wait['predicate'] == {'kind': 'route_open', 'provider': EXECUTOR_PROVIDER, 'model': EXECUTOR_MODEL}


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
        assert (wall['provider'], wall['model'], wall['limit']) == (*ROUTE, 'weekly')
        assert wall['reset_at'] == clock.now + DAY and wall['closed_by']['task_id'] == first
        assert wall['probes'] == 0 and wall['probe'] is None
        assert [w['model'] for w in walls.walls()] == [EXECUTOR_MODEL]
        (file,) = (kb.kanban_home() / 'kanban' / 'route_walls').glob('*.json')
        assert file.name.startswith(f'{EXECUTOR_PROVIDER}--{EXECUTOR_MODEL}.'), 'a person finds the route by its name'

        # (5) one notice for the wall, in the management topic
        (*topic, message), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(clock.now + DAY))
        assert f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada' in message and 'limite weekly' in message
        assert when in message and 'o provedor pediu 1 d de espera' in message
        assert 'esperam sem ocupar vaga' in message

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
        assert len(ops.notices()) == 1 and 'model_fallback' not in events

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


def test_a_card_that_pins_no_route_takes_the_route_of_its_profile(quota_board, launcher, monkeypatch):
    """Not only NFOS cards: any card is held by the route its launch would use. With neither provider nor model
    pinned that is the profile's own; a card that pins only the model keeps the profile's provider."""
    profile_route = ('openai-codex', 'gpt-6-astra')
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        plain = kb.create_task(conn, title='No pin', assignee='default')
        model_only = kb.create_task(conn, title='Model pinned, provider from the profile', assignee='default',
                                    model_override='gpt-5.6-luna')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id IN (?,?)", (plain, model_only))
        assert walls.card_route(conn, kb.get_task(conn, plain)) == profile_route
        assert walls.card_route(conn, kb.get_task(conn, model_only)) == ('openai-codex', 'gpt-5.6-luna')

        walls.close(*profile_route, limit='usage_limit_reached', reset_at=clock.now + 3 * DAY)
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [model_only] and tick.claim_held == [(plain, 'route_closed:openai-codex/gpt-6-astra')]
        assert json.loads(launcher.launches[-1][1][walls.ROUTE_ENV]) == ['openai-codex', 'gpt-5.6-luna']

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


def test_route_reopens_at_the_reset_time_the_provider_gave(quota_board, launcher, monkeypatch, ops):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first, second, _, _ = close_route_with_two_cards(conn, launcher, monkeypatch, GoUsageLimit)
        assert_waiting(conn, kb.dispatch_once(conn), [first, second])
        assert len(ops.notices()) == 1
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
        assert len(ops.notices()) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)


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
        tick = kb.dispatch_once(conn)
        assert tick.claim_held == [(card, HELD)] and not tick.spawned
        (wait,) = quota_waits(conn, card)
        assert (wait['status'], wait['outcome']) == ('expired', 'still_closed')
        assert kb.get_task(conn, card).status == 'ready' and kb.get_task(conn, card).consecutive_failures == 0
        assert not conn.execute('SELECT 1 FROM nfos_decisions').fetchone(), 'nobody is consulted about a quota window'


def test_the_notice_reaches_the_management_topic_when_the_card_follows_only_its_clients_chat(
        quota_board, launcher, monkeypatch, ops):
    """Read on the production profile on 2026-10-12: the source of every project is the project's own topic, and a
    card follows that topic and no other. Nothing passive is published there (CLIENT_CHAT_20260913), so a notice
    that hung on the card's subscription reached nobody."""
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
        assert tuple(topic) == MANAGEMENT_TOPIC and f'Rota {EXECUTOR_PROVIDER}/{EXECUTOR_MODEL} fechada' in message
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
            assert json.loads(env[walls.ROUTE_ENV]) == ['other-provider', 'other-model']
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
    monkeypatch.setenv(walls.ROUTE_ENV, json.dumps(['OpenCode-Go', 'DeepSeek-V4.1-Flash']))
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
