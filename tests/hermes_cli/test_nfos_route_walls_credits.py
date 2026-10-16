"""CREDIT_WALL_20261016: an account out of paid credit closes the route like a usage window does.

Read on the old VPS (read-only) for the night of 2026-10-15 to 16: the OpenRouter key ran out of credit and every
worker answered the same HTTP 402. The answer carries no limit name and no reset time
(``metadata.limit_source: openrouter_credits``), so ``quota_window`` found no window, no route was closed, and the
dispatcher launched 12 cards in four waves of three, one wave per tick, each into the same refusal. Only the card
that had just been refused waited (the respawn guard is per card).

What these tests hold, with the real dispatcher tick, a real board, real child processes and the real quiet
single-query path of the worker (only the provider call, the goal judge and the clock are stand-ins):

* the 402 of an empty balance is a window with no end known; a 402 that only asks to try again in minutes, a
  billing body that may be a content-filter rejection, a plain throttle and a server error are not;
* the worker leaves on the first refusal, with no wait, and the route (profile, provider, model) is closed with the
  limit ``credits`` and no reset time, and one notice goes out;
* after the first wave the dispatcher launches no card of that route: the cards wait without a slot and without a
  failure, and one launch an hour at most (5 min, doubling) probes the route;
* the probe the provider refuses again does not open a second wall or a second notice, and the first answer reopens
  the route;
* no other provider or model is used in place of the closed route.

The harness names the executor's route with the opencode-go provider; the verdict is the same for any provider, it
reads the HTTP status and the structured answer, not the provider's name.
"""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, nfos_route_walls as walls
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    EXECUTOR_MODEL, EXECUTOR_PROVIDER, Clock, ProviderRateLimit, assert_executor_route, launcher, metered_reply,
    quota_board,
)
from tests.hermes_cli.test_nfos_route_walls import (  # noqa: F401
    CONTINUE, HELD, PROFILE, ROUTE, MANAGEMENT_TOPIC, answered_once, assert_waiting, event_kinds, ops, quota_waits,
    ready_card, refusing, run_card, spawned,
)


class OpenRouterCredits(ProviderRateLimit):
    """What OpenRouter answered at 00:48:55Z on 2026-10-16 (request dump of the worker of t_b6caffc3): the OpenAI
    SDK keeps the inner ``error`` object in ``.body``. No ``limitName``, no reset field, no ``Retry-After``."""
    status_code = 402
    body = {'message': 'Insufficient credits. Add more using https://openrouter.ai/settings/credits', 'code': 402,
            'metadata': {'limit_source': 'openrouter_credits',
                         'remedy_hint': 'Add credits at https://openrouter.ai/settings/credits, or lower '
                                        'max_tokens / prompt size to fit your remaining balance.'}}
    response = SimpleNamespace(status_code=402, headers={}, json=lambda: {'error': OpenRouterCredits.body})

    def __str__(self):
        return f"Error code: 402 - {{'error': {OpenRouterCredits.body}}}"


class TryAgainLater(ProviderRateLimit):
    """A 402 that is a periodic quota in disguise: the runtime reads it as a throttle."""
    status_code = 402
    body = {'message': 'Usage limit reached, try again in 5 minutes', 'code': 402}
    response = SimpleNamespace(status_code=402, headers={})

    def __str__(self):
        return f"Error code: 402 - {{'error': {TryAgainLater.body}}}"


class MaybeContentFilter(ProviderRateLimit):
    """A billing body the classifier itself holds unverified (it may be the provider rejecting this message)."""
    status_code = 400
    body = {'error': {'type': 'invalid_request_error', 'message': 'You are out of extra usage'}}
    response = SimpleNamespace(status_code=400, headers={})

    def __str__(self):
        return f"Error code: 400 - {MaybeContentFilter.body}"


class ServerError(ProviderRateLimit):
    status_code = 500
    body = {'error': {'message': 'Internal server error'}}
    response = SimpleNamespace(status_code=500, headers={})

    def __str__(self):
        return f"Error code: 500 - {ServerError.body}"


def _agent(pending_fallback=False):
    return SimpleNamespace(provider=EXECUTOR_PROVIDER, model=EXECUTOR_MODEL, _fallback_activated=False,
                           _has_pending_fallback=lambda: pending_fallback)


CREDITS = {'limit': 'credits', 'reset_at': None}
VERDICTS = [
    pytest.param(OpenRouterCredits(), CREDITS, id='openrouter-insufficient-credits'),
    pytest.param(TryAgainLater(), None, id='402-that-resets-in-minutes'),
    pytest.param(MaybeContentFilter(), None, id='billing-body-that-may-be-a-content-filter'),
    pytest.param(ProviderRateLimit(), None, id='plain-429'),
    pytest.param(ServerError(), None, id='server-error'),
    pytest.param(ValueError('Insufficient credits'), None, id='only-text-says-so'),
]


@pytest.mark.parametrize('error, expected', VERDICTS)
def test_an_empty_balance_is_a_window_whose_end_is_unknown(error, expected):
    assert walls.credit_window(_agent(), error) == expected


def test_the_402_of_an_empty_balance_names_no_window_to_the_usage_window_reader():
    # The reason the wall did not close on the night of 15 to 16 October: the answer has nothing quota_window reads.
    assert walls.quota_window(OpenRouterCredits(), now=1_791_800_000.0) is None


def test_only_a_cards_own_worker_with_no_fallback_left_closes_the_route_on_an_empty_balance(quota_board, monkeypatch):
    assert walls.on_quota_error(_agent(), OpenRouterCredits()) is True, 'any session leaves the turn'
    assert not walls.any_closed(), 'and only a card worker closes a route'
    assert walls.on_quota_error(_agent(), TryAgainLater()) is False

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    assert walls.on_quota_error(_agent(pending_fallback=True), OpenRouterCredits()) is True
    assert not walls.any_closed(), 'a fallback provider the operator configured still has the turn'
    assert walls.on_quota_error(_agent(), TryAgainLater()) is False and not walls.any_closed()

    assert walls.on_quota_error(_agent(), OpenRouterCredits()) is True
    wall = walls.read(*ROUTE)
    assert (wall['limit'], wall['reset_at'], wall['closed_by']['task_id']) == ('credits', None, 't_worker')
    assert wall['next_probe_at'] == wall['closed_at'] + 300


posix_only = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')


@posix_only
def test_a_burst_of_cards_meets_the_empty_balance_once_and_the_rest_wait(quota_board, launcher, monkeypatch, ops):
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        cards = [ready_card(conn, f'Order screen {n}', assignee=PROFILE) for n in range(8)]

        # The first wave: the slots the profile has (two in production), launched while the route is open.
        tick = kb.dispatch_once(conn, max_spawn=2)
        wave = spawned(tick)
        assert len(wave) == 2 and set(wave) <= set(cards)
        for task_id in wave:
            run = run_card(conn, launcher, monkeypatch, task_id, refusing(OpenRouterCredits))
            assert (run.turns, run.provider_calls, run.judge_calls, run.waits.waited) == (1, 1, 0, 0), task_id
            assert [(r['failed'], r['failure_reason']) for r in run.results] == [(True, 'billing')]
            assert run.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75

        wall = walls.read(*ROUTE)
        assert (wall['limit'], wall['reset_at'], wall['closed_by']['task_id']) == ('credits', None, wave[0])
        assert wall['probes'] == 0 and wall['next_probe_at'] == clock.now + 300
        (*topic, message), = ops.notices()
        assert tuple(topic) == MANAGEMENT_TOPIC
        assert 'pelo limite credits, sem hora de reinício informada' in message
        assert 'esperam sem ocupar vaga e sem contar falha' in message

        # The tick that reaps the wave launches nobody: before this rule it launched the next two of the eight,
        # and the tick after that the next two.
        tick = kb.dispatch_once(conn, max_spawn=2)
        assert sorted(tick.rate_limited) == sorted(wave)
        assert_waiting(conn, tick, cards)
        launches, events = len(launcher.launches), event_kinds(conn)
        assert launches == 2
        for _ in range(4):
            clock.advance(60)
            assert_waiting(conn, kb.dispatch_once(conn, max_spawn=2), cards)
        assert len(launcher.launches) == launches and event_kinds(conn) == events
        assert [row['consecutive_failures'] for row in
                conn.execute('SELECT consecutive_failures FROM tasks')] == [0] * len(cards)

        # The route is probed after 5 minutes by one launch; the provider refuses it again: same wall, no notice,
        # and the next probe is further away.
        clock.advance(60)
        tick = kb.dispatch_once(conn, max_spawn=2)
        (probe,) = spawned(tick)
        assert sorted(tick.claim_held) == sorted((card, HELD) for card in cards if card != probe)
        refused = run_card(conn, launcher, monkeypatch, probe, refusing(OpenRouterCredits))
        assert (refused.provider_calls, refused.waits.waited, refused.exit_code) == (1, 0, 75)
        wall = walls.read(*ROUTE)
        assert (wall['probes'], wall['probe'], wall['next_probe_at']) == (1, None, clock.now + 600)
        assert len(ops.notices()) == 1
        assert_waiting(conn, kb.dispatch_once(conn, max_spawn=2), cards)

        # Credit is back: the probe's call is answered, the route reopens and the cards go on.
        clock.advance(600)
        (probe,) = spawned(kb.dispatch_once(conn, max_spawn=2))
        run_card(conn, launcher, monkeypatch, probe, answered_once, judge_reply=CONTINUE)
        assert walls.read(*ROUTE) is None
        assert len(spawned(kb.dispatch_once(conn, max_spawn=2))) == 2
        assert len(ops.notices()) == 1
        for launch_argv, _ in launcher.launches:
            assert_executor_route(launch_argv)
        assert json.loads(launcher.launches[0][1][walls.ROUTE_ENV]) == list(ROUTE)
