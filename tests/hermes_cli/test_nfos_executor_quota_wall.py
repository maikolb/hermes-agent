"""R3 (a): an NFOS executor on its provider's quota wall, with no reserve provider.

The plan of record supposes: the executor gets a 429, has no fallback provider, leaves with exit code 75, its card
returns to the queue with a wait that grows, no failure is counted and nothing falls to Codex.

What these tests find on main:

* The dispatcher's half holds. Exit code 75 from a real process requeues the card with no failure, the wait doubles
  from five minutes to one hour, and each relaunch names the executor's own provider.
* The executor's half does not. Every NFOS card is a goal-mode card, and the goal loop runs after the turn that died
  on the wall: it repeats the turn until the goal budget is spent and then blocks, which on an NFOS card files an
  impediment for the Principal. The card is then held by that open decision, not by the growing wait. The executor
  never changes provider, but each wall becomes a consult to the Principal, who runs on Codex.

Read on the production boards on 2026-10-12 (read-only): 88 of the 89 runs that ended `rate_limited` filed the
impediment "Goal-mode worker exhausted its turn budget" in that same run.
"""
import os
import signal

import pytest

from hermes_cli import goals, kanban_db as kb
from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    EXECUTOR_MODEL, EXECUTOR_PROVIDER, WORKER_ENV, Clock, agent_on_quota_wall, assert_executor_route, enrol,
    launcher, option, quota_board,
)

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

FIRST_WAIT = 300
# Seconds the card waits after each consecutive quota exit: the wait doubles up to the one hour ceiling. The guard
# counts the quota exits of the three hours before the moment it looks, so the first exits leave that window during
# the sixth wait and it ends at 3001 s.
WAITS = [300, 600, 1200, 2400, 3600, 3001]


class RateLimitError(Exception):
    """The goal judge shares the executor's provider. Same class name as on the boards in September."""


def run_executor(monkeypatch, argv, env):
    """The executor child, run in this process: the real quiet single-query path of `cli.main`, driving a real
    AIAgent whose provider answers every call with 429. Returns what the process did and the code it left with."""
    import cli as cli_mod
    from agent import auxiliary_client

    for key in WORKER_ENV:
        if key in env:
            monkeypatch.setenv(key, env[key])
    agent, seen = agent_on_quota_wall(monkeypatch, provider=option(argv, '--provider'), model=option(argv, '-m'))
    seen.agent, seen.judge_calls = agent, 0

    def judge(*args, **kwargs):
        seen.judge_calls += 1
        raise RateLimitError('Error code: 429 - Rate limit exceeded.')

    monkeypatch.setattr(auxiliary_client, 'call_llm', judge)

    class WorkerCLI:
        """HermesCLI reduced to what the quiet single-query path reads. The agent behind it is real."""

        def __init__(self, **options):
            self.provider = self.requested_provider = agent.provider
            self.model = agent.model
            self.session_id = agent.session_id
            self.conversation_history = []
            self._active_agent_route_signature = 'executor'
            self.agent = agent

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, query):
            return {'signature': 'executor', 'model': None, 'runtime': None, 'request_overrides': None}

        def _init_agent(self, **kwargs):
            return True

    monkeypatch.setattr(cli_mod, 'HermesCLI', WorkerCLI)
    monkeypatch.setattr(cli_mod.atexit, 'register', lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_mod, '_finalize_single_query', lambda cli: None)
    monkeypatch.setattr(cli_mod, '_single_query_finalize_attempted_session_ids', set())
    monkeypatch.setattr(cli_mod, '_cleanup_done', False)
    handlers = {number: signal.getsignal(number) for number in
                (signal.SIGINT, signal.SIGTERM, getattr(signal, 'SIGHUP', None)) if number is not None}
    try:
        with pytest.raises(SystemExit) as left:
            cli_mod.main(query=option(argv, '-q'), quiet='-Q' in argv, toolsets=option(argv, '--toolsets'),
                         model=option(argv, '-m'), provider=option(argv, '--provider'),
                         reasoning=option(argv, '--reasoning'))
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        for key in WORKER_ENV:
            monkeypatch.delenv(key, raising=False)
    seen.exit_code = left.value.code
    return seen


def _decisions(conn, task_id):
    return [dict(row) for row in conn.execute(
        'SELECT id,kind,status,question,run_id FROM nfos_decisions WHERE task_id=? ORDER BY rowid', (task_id,))]


def quota_wall(launcher, monkeypatch):
    """One executor run on the wall, then the dispatcher: the tick that reaps it and the tick after the wait."""
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
        argv, env = launcher.launches[-1]
        run = run_executor(monkeypatch, argv, env)
        run.task_id = task.id
        run.decisions = _decisions(conn, task.id)
        launcher.exit(task.worker_pid, run.exit_code)
        clock = Clock(monkeypatch)
        run.reaped = kb.dispatch_once(conn)
        run.card = kb.get_task(conn, task.id)
        run.outcomes = [row['outcome'] for row in conn.execute(
            'SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id', (task.id,))]
        clock.advance(FIRST_WAIT)
        run.guard_after_wait = kb.check_respawn_guard(conn, task.id)
        run.after_wait = kb.dispatch_once(conn)
        run.relaunched = [row[0] for row in run.after_wait.spawned]
    return run


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


def test_today_the_goal_loop_spends_its_budget_on_the_wall_and_asks_the_principal(quota_board, launcher, monkeypatch):
    """What main does today. Delete with the xfail mark below when the executor leaves at the first wall."""
    run = quota_wall(launcher, monkeypatch)
    assert run.turns == goals.DEFAULT_MAX_TURNS == 20, 'one turn died on the wall, the goal loop ran nineteen more'
    assert run.provider_calls == run.turns * run.agent._api_max_retries == 60
    assert run.judge_calls == run.turns
    assert all(result['failure_reason'] == 'rate_limit' for result in run.results)
    assert [(row['kind'], row['status']) for row in run.decisions] == [('impediment', 'pending')]
    assert run.decisions[0]['question'].startswith(
        'Goal-mode worker exhausted its turn budget (20/20) without completing the task. '
        'Last judge verdict: judge error: RateLimitError')
    assert run.guard_after_wait is None, 'the wait itself is over'
    assert run.relaunched == [] and (run.task_id, 'open_decision') in run.after_wait.claim_held


@pytest.mark.xfail(strict=True, reason=(
    'R3 (a): the goal loop runs after the turn that died on the quota wall. cli.main calls _run_kanban_goal_loop_q '
    'without looking at result["failed"], so the executor repeats the turn until the goal budget is spent (20 turns, '
    '60 provider calls here) and block_task then files an impediment for the Principal. The card is held by that '
    'open decision instead of the growing wait. Fix: when the first turn or a goal turn ends failed with '
    'failure_reason rate_limit or billing, skip or leave the goal loop and exit 75 at once.'))
def test_executor_leaves_at_the_first_wall_and_only_the_growing_wait_holds_the_card(
        quota_board, launcher, monkeypatch):
    run = quota_wall(launcher, monkeypatch)
    assert run.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    found = []
    if run.turns != 1:
        found.append(f'{run.turns} turns and {run.provider_calls} provider calls on the wall instead of 1 turn')
    if run.decisions:
        found.append('consult filed for the Principal: ' + run.decisions[0]['question'][:120])
    if run.relaunched != [run.task_id]:
        found.append(f'after the {FIRST_WAIT} s wait the card is not relaunched: held by {run.after_wait.claim_held}')
    assert not found, '; '.join(found)
