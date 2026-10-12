"""R3 (a): an NFOS executor on its provider's quota wall, with no reserve provider.

The plan of record supposes: the executor gets a 429, has no fallback provider, leaves with exit code 75, its card
returns to the queue with a wait that grows, no failure is counted and nothing falls to Codex.

What these tests hold:

* The dispatcher's half. Exit code 75 from a real process requeues the card with no failure, the wait doubles from
  five minutes to one hour, and each relaunch names the executor's own provider.
* The executor's half. Every NFOS card is a goal-mode card. A turn that ends on the wall, the first one or a later
  one, stops the goal loop at once: no judge call, no further turn, no block, so no impediment is filed for the
  Principal (who runs on Codex). The process leaves with 75. A throttle leaves the card to the growing wait; a usage
  window (a named limit, or a reset beyond the client's 600 s wait) closes the route, which is what holds the card.
* A 429 that clears on the client's own retry is not a wall: no turn fails, the goal loop runs its whole budget as
  before and the process does not leave with the quota code. The client's patience is its three attempts: with no
  Retry-After that is 6 to 9 s, and a 429 that outlasts it is a wall like any other (exit 75, five minutes of wait),
  where before the fix the executor stayed in its goal loop.

Before the fix the goal loop ran after the turn that died on the wall and repeated it until the goal budget was
spent, then blocked, which on an NFOS card files an impediment. Read on the production boards on 2026-10-12
(read-only): 88 of the 89 runs that ended `rate_limited` filed the impediment "Goal-mode worker exhausted its turn
budget" in that same run. The two workers launched with no fallback provider on 2026-10-10 met the opencode-go
weekly wall (`GoUsageLimit` below): three attempts 600 s apart per turn, twenty turns in 6 h 46 min, one impediment.

Since ROUTE_WALL_20261012 the executor launched onto the weekly wall leaves on its first refused call (it used to
sleep through two capped Retry-After waits, about twenty minutes holding a slot) and the dispatcher does not launch
on that route again until it reopens. That is held in test_nfos_route_walls; here the walls keep their place in
the table below.
"""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import goals, kanban_db as kb
from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    EXECUTOR_MODEL, EXECUTOR_PROVIDER, WORKER_ENV, Clock, GoUsageLimit, GoUsageLimitNoReset, ProviderRateLimit,
    assert_executor_route, enrol, launcher, quota_board, reply, run_executor,
)

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

FIRST_WAIT = 300
# Seconds the card waits after each consecutive quota exit: the wait doubles up to the one hour ceiling. The guard
# counts the quota exits of the three hours before the moment it looks, so the first exits leave that window during
# the sixth wait and it ends at 3001 s.
WAITS = [300, 600, 1200, 2400, 3600, 3001]
CONTINUE = json.dumps({'verdict': 'continue', 'reason': 'the total on the screen is not corrected yet'})


def _decisions(conn, task_id):
    return [dict(row) for row in conn.execute(
        'SELECT id,kind,status,question,run_id FROM nfos_decisions WHERE task_id=? ORDER BY rowid', (task_id,))]


def _event_kinds(conn, task_id):
    return [row['kind'] for row in conn.execute('SELECT kind FROM task_events WHERE task_id=? ORDER BY id', (task_id,))]


def quota_wall(launcher, monkeypatch, answer=None, judge_reply=None):
    """One executor run, then the dispatcher: the tick that reaps it and the tick after the wait."""
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
        argv, env = launcher.launches[-1]
        clock = Clock(monkeypatch)
        run = run_executor(monkeypatch, argv, env, answer=answer, judge_reply=judge_reply)
        run.task_id = task.id
        run.decisions = _decisions(conn, task.id)
        launcher.exit(task.worker_pid, run.exit_code)
        run.reaped = kb.dispatch_once(conn)
        run.card = kb.get_task(conn, task.id)
        run.outcomes = [row['outcome'] for row in conn.execute(
            'SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
        run.events = _event_kinds(conn, task.id)
        clock.advance(FIRST_WAIT)
        run.guard_after_wait = kb.check_respawn_guard(conn, task.id)
        run.after_wait = kb.dispatch_once(conn)
        run.relaunched = [row[0] for row in run.after_wait.spawned]
    return run


ROUTE_CLOSED = f'route_closed:{EXECUTOR_PROVIDER}/{EXECUTOR_MODEL}'


def assert_left_on_the_wall(run, launcher, route_closed_until_reset=False):
    """The executor left with the quota code. After the first wait the card is launched again, on the executor's
    own route: by the growing wait of a throttle, or as the one probe of a window that gave no reset time. A window
    that gave its reset time keeps the route closed until then."""
    assert run.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75
    assert run.decisions == [], 'no consult is filed for the Principal'
    assert not {'nfos_principal_requested', 'nfos_goal_yield', 'blocked'} & set(run.events), run.events
    assert run.reaped.rate_limited == [run.task_id] and not run.reaped.crashed and not run.reaped.auto_blocked
    assert run.outcomes == ['rate_limited'], 'the run is closed as a quota exit, not as a crash'
    assert run.card.status == 'ready' and run.card.consecutive_failures == 0
    assert run.guard_after_wait is None, 'the wait itself is over'
    if route_closed_until_reset:
        assert run.relaunched == [] and run.after_wait.claim_held == [(run.task_id, ROUTE_CLOSED)], run.after_wait
    else:
        assert run.relaunched == [run.task_id] and not run.after_wait.claim_held, run.after_wait
    assert_executor_route(launcher.launches[-1][0])
    assert run.agent._fallback_chain == [] and True not in run.fallback and run.reserve == []


def test_launch_names_the_executor_route_and_the_profile_has_no_reserve(quota_board, launcher):
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
    argv, env = launcher.launches[-1]
    assert_executor_route(argv)
    assert '-Q' in argv and env['HERMES_KANBAN_GOAL_MODE'] == '1', 'an NFOS card always runs the goal loop'
    assert env['HERMES_KANBAN_TASK'] == task.id and env['HERMES_KANBAN_RUN_ID'] == str(task.current_run_id)
    assert get_fallback_chain(load_config()) == []


def test_quota_exit_requeues_with_no_failure_and_the_wait_doubles_up_to_one_hour(quota_board, launcher, monkeypatch):
    """The dispatcher's half, from the real exit status of a real child."""
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
        clock = Clock(monkeypatch)
        for attempt, wait in enumerate(WAITS, start=1):
            launcher.exit(kb.get_task(conn, task.id).worker_pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            tick = kb.dispatch_once(conn)
            card = kb.get_task(conn, task.id)
            assert tick.rate_limited == [task.id] and not tick.crashed and not tick.auto_blocked, (attempt, tick)
            assert card.status == 'ready' and card.consecutive_failures == 0, attempt
            assert tick.respawn_guarded == [(task.id, 'rate_limit_cooldown')] and not tick.spawned, attempt
            clock.advance(wait - 1)
            assert kb.check_respawn_guard(conn, task.id) == 'rate_limit_cooldown', (attempt, wait)
            clock.advance(1)
            tick = kb.dispatch_once(conn)
            assert [row[0] for row in tick.spawned] == [task.id], (attempt, wait, tick)
            assert_executor_route(launcher.launches[-1][0])
        outcomes = [row['outcome'] for row in conn.execute(
            'SELECT outcome FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id', (task.id,))]
        assert outcomes == ['rate_limited'] * len(WAITS)
        assert kb.get_task(conn, task.id).status == 'running'
    assert len(launcher.launches) == 1 + len(WAITS)


def test_quota_wall_turn_is_a_rate_limit_failure_and_the_executor_never_changes_provider(
        quota_board, launcher, monkeypatch):
    run = quota_wall(launcher, monkeypatch)
    first = run.results[0]
    assert first['failed'] is True and first['failure_reason'] == 'rate_limit'
    assert run.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75
    assert run.agent._fallback_chain == [] and True not in run.fallback and not run.agent._fallback_activated
    assert (run.agent.provider, run.agent.model, str(run.agent.base_url)) == run.route
    assert run.route[:2] == (EXECUTOR_PROVIDER, EXECUTOR_MODEL)
    assert run.reserve == [], 'no other provider client was asked for'
    assert run.reaped.rate_limited == [run.task_id] and not run.reaped.crashed and not run.reaped.auto_blocked
    assert run.outcomes == ['rate_limited'], 'the run is closed as a quota exit, not as a crash'
    assert run.card.status == 'ready' and run.card.consecutive_failures == 0


# The wall, what the turn result calls it, provider calls in the one turn, seconds the client waited inside it,
# and whether the route stays closed past the first wait.
# A 429 with no Retry-After is a wall once it outlasts the client's two jittered waits from a 2 s base, 6 to 9 s.
# The weekly window is left on the first refusal: its reset is a day away and no wait of the client reaches it.
WALLS = [
    pytest.param(ProviderRateLimit, 'rate_limit', 3, 6, False, id='plain-429'),
    pytest.param(GoUsageLimit, 'rate_limit', 1, 0, True, id='opencode-go-weekly'),
    pytest.param(GoUsageLimitNoReset, 'billing', 1, 0, False, id='usage-limit-with-no-reset'),
]


@pytest.mark.parametrize('wall, reason, calls, waited, closed_until_reset', WALLS)
def test_executor_leaves_at_the_first_wall_and_the_card_waits_with_no_failure(
        quota_board, launcher, monkeypatch, wall, reason, calls, waited, closed_until_reset):
    def answer(seen):
        raise wall()

    run = quota_wall(launcher, monkeypatch, answer=answer)
    assert [(result['failed'], result['failure_reason']) for result in run.results] == [(True, reason)]
    assert (run.turns, run.provider_calls, run.judge_calls) == (1, calls, 0), 'one turn, the goal loop did not run'
    assert waited <= run.waits.waited < waited + 10, 'the short backoff of a throttle, no wait at all on a window'
    assert_left_on_the_wall(run, launcher, route_closed_until_reset=closed_until_reset)


def test_wall_met_on_a_later_goal_turn_leaves_the_loop_the_same_way(quota_board, launcher, monkeypatch):
    """The first turn worked and did not finish; the goal loop asked for another and that one met the wall."""
    def answer(seen):
        if seen.turns >= 2:
            raise GoUsageLimit()
        return reply('The total is read from the wrong column; correcting it next.')

    run = quota_wall(launcher, monkeypatch, answer=answer, judge_reply=CONTINUE)
    assert [result.get('failure_reason') for result in run.results] == [None, 'rate_limit']
    assert not run.results[0].get('failed')
    assert (run.turns, run.judge_calls) == (2, 1), 'judged once, after the turn that answered'
    assert run.refused == 1 and run.waits.waited == 0, 'the weekly window is left on its first refusal'
    assert_left_on_the_wall(run, launcher, route_closed_until_reset=True)


def test_a_429_that_clears_on_the_clients_own_retry_is_not_a_quota_exit(quota_board, launcher, monkeypatch):
    """Every turn meets one 429 and the client's retry is answered. No turn fails, so the goal loop is the one it
    was: the executor works its whole turn budget and does not leave with the quota code."""
    met = set()

    def answer(seen):
        if seen.turns not in met:
            met.add(seen.turns)
            raise ProviderRateLimit()
        return reply('Still correcting the total.')

    run = quota_wall(launcher, monkeypatch, answer=answer, judge_reply=CONTINUE)
    assert not [result for result in run.results if result.get('failed') or result.get('failure_reason')]
    assert run.turns == run.judge_calls == goals.DEFAULT_MAX_TURNS == 20, 'the goal loop ran its whole budget'
    assert run.refused == run.turns, 'one 429 in each turn, each cleared by the retry'
    assert run.exit_code == 0
    assert not run.reaped.rate_limited and 'rate_limited' not in run.outcomes and 'rate_limited' not in run.events
    assert run.card.consecutive_failures == 0


def test_goal_loop_wrapper_hands_back_the_turn_that_met_the_wall(quota_board, launcher, monkeypatch):
    """`_run_kanban_goal_loop_q` returns the walled turn's result, which is what `cli.main` derives the exit code
    from, and nothing for a loop that ended for any other cause."""
    import cli as cli_mod

    walled = {'failed': True, 'failure_reason': 'rate_limit', 'final_response': 'API call failed after 3 retries'}
    for reason, expected in (('rate_limit', True), ('billing', True), ('upstream_rate_limit', False), (None, False)):
        assert cli_mod._kanban_quota_wall({'failed': True, 'failure_reason': reason}) is expected, reason
    assert cli_mod._kanban_quota_wall({'failed': False, 'failure_reason': 'rate_limit'}) is False
    assert cli_mod._kanban_quota_wall(None) is False and cli_mod._kanban_quota_wall('text') is False

    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
    _, env = launcher.launches[-1]
    for key in WORKER_ENV:
        if key in env:
            monkeypatch.setenv(key, env[key])
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: pytest.fail('a walled turn is not judged'))
    agent = SimpleNamespace(session_id='executor', run_conversation=lambda **kwargs: pytest.fail('no further turn'))
    worker = SimpleNamespace(agent=agent, conversation_history=[], session_id='executor')
    assert cli_mod._run_kanban_goal_loop_q(worker, walled['final_response'], walled) is walled
    with kb.connect_closing() as conn:
        assert _decisions(conn, task.id) == [] and kb.get_task(conn, task.id).status == 'running'

    turns = iter([{'final_response': 'working'}, dict(walled, failure_reason='billing')])
    agent.run_conversation = lambda **kwargs: next(turns)
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: ('continue', 'not done', False, None, False))
    later = cli_mod._run_kanban_goal_loop_q(worker, 'working', {'final_response': 'working'})
    assert later['failure_reason'] == 'billing' and next(turns, None) is None
    with kb.connect_closing() as conn:
        assert _decisions(conn, task.id) == [] and kb.get_task(conn, task.id).status == 'running'
