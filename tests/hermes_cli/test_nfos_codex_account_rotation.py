"""POOL_WALL_20261012: a profile with several ChatGPT accounts on its Codex route goes from one to the next when a
usage window ends, and the route closes only when every account has refused.

Owner instruction of 2026-10-12: "o nfos deve alternar entre as 3 contas. Acabou uma, entra outra por fallback".
Read on the production profile that day (read-only): the weekly window of one account ended four times in four
days, the rotation left that account once (0.45 s) and came back to it 187 times in one session.

The three scenarios the coordinating session asked to see, each with three credentials, a real board, the real
dispatcher tick, the real quiet single-query path of the worker with its real route resolution, a real agent
holding the real pool, and the gateway's own notifier (the model, the usage endpoint, the goal judge and the clock
are stand-ins, and every token is a fake):

1. the first account refuses and the second answers: the turn goes on, no route closes, nothing is sent to the
   management topic, and a request whose decision turn it was is not sent back to the inbox;
2. the three refuse: one wall, one notice, and the route reopens at the soonest reset time of the three;
3. the route's probe finds an account back before the time its provider gave: the route reopens.

Scenarios 1 and 2 also run under the two conditions in which the rotation used to hand back the account it had
just marked: the usage endpoint had shown that account's window open just before the refusal, and purchased
credits authorized by ``codex.use_credits`` (on in production). And work that resolves no credential at all,
because the whole pool is inside a usage window on record, leaves by the quota door instead of failing: a card's
launch (the quota exit code) and a request turn of the coordinating session, through the gateway's own route
resolution (the request goes back to the inbox and nothing is said in the topic of who asked).

No notice or log line asserted here names an account: the pool's labels are positions ("login 1").
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from agent.credential_pool import load_pool
from gateway import run as gateway_run
from gateway.wake import current_notify_receipt
from hermes_cli import kanban_db as kb, nfos_delivery as delivery, nfos_route_walls as walls
from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError
from tests.gateway.test_nfos_intake import setup  # noqa: F401
from tests.hermes_cli.nfos_codex_pool import (
    CODEX_BASE_URL, CODEX_MODEL, CODEX_PROVIDER, DAY, Provider, account_of, read_pool, run_pooled_executor,
    three_logins,
)
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    PROFILE_CONFIG, WORKER_ENV, Clock, enrol, launcher, quota_board,
)
from tests.hermes_cli.test_nfos_principal_quota_wall import (  # noqa: F401
    SESSION_KEY, Session, _board, _settle, _tick, intake,
)
from tests.hermes_cli.test_nfos_route_walls import MANAGEMENT_TOPIC, Operations, launch_of, ops  # noqa: F401

posix_only = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

ROUTE = ('default', CODEX_PROVIDER, CODEX_MODEL)
HELD = f'route_closed:{CODEX_PROVIDER}/{CODEX_MODEL}@default'
CONTINUE = json.dumps({'verdict': 'continue', 'reason': 'the total on the screen is not corrected yet'})
GOAL_TURNS = 20
# When the window of each account reopens, counted from the start of a scenario: the second login is the soonest.
WINDOWS = {'A': 5 * DAY, 'B': 2 * DAY, 'C': 4 * DAY}
CONDITIONS = ('plain', 'usage endpoint had shown the window open', 'credits authorized')
# A refusal on record this long ago is older than the usage probe's interval: the usage endpoint is asked about it.
LONG_AGO = 600


def profile_config(use_credits=False):
    """The production profile's shape with its cards on the Codex route, the one its accounts serve."""
    config = json.loads(json.dumps(PROFILE_CONFIG))
    config['kanban']['delivery'].update(worker_model=CODEX_MODEL, worker_provider=CODEX_PROVIDER)
    config['codex'] = {'use_credits': use_credits}
    return config


@pytest.fixture
def codex_board(quota_board, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(quota_board / 'no-codex-cli'))
    (quota_board / 'config.yaml').write_text(yaml.safe_dump(profile_config()), encoding='utf-8')
    return quota_board


def arrange(home, monkeypatch, clock, closed, condition='plain'):
    """Three logins and the provider behind them. ``closed`` names the accounts out of quota."""
    assert condition in CONDITIONS
    first = {}
    if condition == CONDITIONS[1]:
        # The first account sits in an earlier cooldown and the usage endpoint shows 99%: the pool goes back to
        # it. Its next model call is refused all the same.
        first = {'last_status': 'exhausted', 'last_status_at': clock.now - LONG_AGO, 'last_error_code': 429,
                 'last_error_reason': 'usage_limit_reached', 'last_error_reset_at': clock.now + WINDOWS['A']}
    three_logins(home, **first)
    provider = Provider(monkeypatch, {name: clock.now + WINDOWS[name] for name in closed})
    if condition == CONDITIONS[1]:
        provider.used['A'] = 99.0
    if condition == CONDITIONS[2]:
        (home / 'config.yaml').write_text(yaml.safe_dump(profile_config(use_credits=True)), encoding='utf-8')
        provider.credits = set('ABC')
    return provider


def run_card(conn, launcher, monkeypatch, provider, task_id):
    """The worker of this card's latest launch, run in this process, leaving with the code it chose."""
    argv, env = launch_of(launcher, task_id)
    pid = kb.get_task(conn, task_id).worker_pid
    run = run_pooled_executor(monkeypatch, argv, env, provider, judge_reply=CONTINUE)
    launcher.exit(pid, 1 if run.exit_code is None else run.exit_code)
    return run


def measured(conn, run, ops, task_id, start, home):
    """What one worker run left behind, as numbers: read after the dispatcher reaped it and the notifier ran."""
    wall = walls.read(*ROUTE)
    tick = kb.dispatch_once(conn)
    card = kb.get_task(conn, task_id)
    sent = ops.notices()
    return {
        'exit_code': run.exit_code, 'stopped_at_the_call_ceiling': run.stopped, 'turns': run.turns,
        'calls': ''.join(run.calls), 'failed_turns': sum(1 for result in run.results if result.get('failed')),
        'walls': len(walls.walls()),
        'wall_reset_in': None if wall is None or not wall.get('reset_at') else int(wall['reset_at'] - start),
        'route_opens_in': None if wall is None else int(walls.open_at(wall) - start),
        'wall_limit': wall and wall['limit'], 'wall_account': wall and wall.get('account'),
        'wall_probes': wall and wall['probes'],
        'notices': len(sent), 'notice_topics': [tuple(row[:2]) for row in sent], 'notice_texts': [row[2] for row in sent],
        'reaped_as_quota_exit': task_id in tick.rate_limited, 'reaped_as_crash': task_id in tick.crashed,
        'card_failures': card.consecutive_failures, 'card_status': card.status,
        'held_by_the_route': (task_id, HELD) in tick.claim_held,
        'pool': {ident: row.get('last_status') for ident, row in read_pool(home).items()},
    }


def first_refuses_second_answers(conn, launcher, monkeypatch, ops, home, condition='plain'):
    """Scenario 1, a card's worker."""
    clock = Clock(monkeypatch)
    provider = arrange(home, monkeypatch, clock, closed='A', condition=condition)
    task = enrol(conn, launcher)
    run = run_card(conn, launcher, monkeypatch, provider, task.id)
    return measured(conn, run, ops, task.id, clock.now, home)


def all_three_refuse(conn, launcher, monkeypatch, ops, home, condition='plain'):
    """Scenario 2, a card's worker. Returns the numbers, and what scenario 3 goes on from."""
    clock = Clock(monkeypatch)
    provider = arrange(home, monkeypatch, clock, closed='ABC', condition=condition)
    task = enrol(conn, launcher)
    run = run_card(conn, launcher, monkeypatch, provider, task.id)
    return measured(conn, run, ops, task.id, clock.now, home), SimpleNamespace(
        clock=clock, provider=provider, task=task, start=clock.now)


def an_account_comes_back_before_its_time(conn, launcher, monkeypatch, ops, home):
    """Scenario 3. The three refused and the route closed. When the wall admits its next launch (on main, the
    soonest reset time, the second login's; with an hourly probe, one hour after it closed) the dispatcher launches
    the route's probe. By then the third account is back, days before the time its provider had given, and the
    second is still out whatever its provider had said (a rolling window: one more day). The probe goes past
    every account that refuses and is answered by the third."""
    closed, state = all_three_refuse(conn, launcher, monkeypatch, ops, home)
    clock, provider, task = state.clock, state.provider, state.task
    clock.advance(closed['route_opens_in'] - 1)
    early = kb.dispatch_once(conn)
    clock.advance(1)
    provider.closed_until.update(B=clock.now + DAY)
    del provider.closed_until['C']
    due = kb.dispatch_once(conn)
    probe = (walls.read(*ROUTE) or {}).get('probe') or {}
    out = {'route_closed_with': closed, 'launched_one_second_before': [row[0] for row in early.spawned],
           'launched_when_the_wall_admits_it': [row[0] for row in due.spawned] == [task.id],
           'that_launch_is_the_probe': probe.get('task_id') == task.id}
    if out['launched_when_the_wall_admits_it']:
        run = run_card(conn, launcher, monkeypatch, provider, task.id)
        out.update(measured(conn, run, ops, task.id, state.start, home))
    return out


def three_walls_on_record(clock, provider):
    """Each account refused a call of some other consumer of the profile (an auxiliary call, another session) a
    while ago: the three refusals are in ``auth.json`` with their reset times, counted from now, and no route was
    closed for them."""
    pool = load_pool(CODEX_PROVIDER)
    for name in 'ABC':
        entry = next(e for e in pool.entries() if e.id == f'e-{name}')
        pool.mark_exhausted_and_rotate(
            status_code=429, credential_id=entry.id, api_key_hint=entry.access_token,
            error_context={'reason': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                           'reset_at': clock.now + LONG_AGO + WINDOWS[name]})
    clock.advance(LONG_AGO)
    provider.closed_until.update({name: clock.now + WINDOWS[name] for name in 'ABC'})


def launch_that_resolves_no_credential(conn, launcher, monkeypatch, ops, home):
    """The whole pool is inside a usage window on record and no wall stands yet: the dispatcher launches a card
    on that route."""
    clock = Clock(monkeypatch)
    provider = arrange(home, monkeypatch, clock, closed='ABC')
    three_walls_on_record(clock, provider)
    task = enrol(conn, launcher)
    run = run_card(conn, launcher, monkeypatch, provider, task.id)
    out = measured(conn, run, ops, task.id, clock.now, home)
    out['usage_asked'] = ''.join(sorted(provider.usage_asked))
    # Later the same card is launched as the probe of that wall, before any window ends (the probe is taken here
    # with the function the dispatcher takes it with), and still resolves nothing: that is the probe's verdict.
    wall = walls.read(*ROUTE)
    if wall is not None:
        start = clock.now
        clock.advance(3601)
        walls.take_probe(*ROUTE, task_id=task.id, board=kb.DEFAULT_BOARD)
        argv, env = launch_of(launcher, task.id)
        again = run_pooled_executor(monkeypatch, argv, env, provider, judge_reply=CONTINUE)
        after = walls.read(*ROUTE)
        out['probe'] = {'exit_code': again.exit_code, 'calls': ''.join(again.calls), 'probes': after['probes'],
                        'probe_released': after['probe'] is None, 'walled_again': after['walled_at'] == clock.now,
                        'reset_in': int(after['reset_at'] - start), 'notices': len(ops.notices())}
    return out


# ── Scenario 1: the first account refuses, the second answers ───────────

@posix_only
@pytest.mark.parametrize('condition', CONDITIONS)
def test_the_first_account_refuses_and_the_turn_goes_on_with_the_second(
        codex_board, launcher, monkeypatch, ops, condition):
    with kb.connect_closing() as conn:
        got = first_refuses_second_answers(conn, launcher, monkeypatch, ops, codex_board, condition)
    assert got['stopped_at_the_call_ceiling'] is False
    calls = got['calls']
    assert calls[0] == 'A' and set(calls[1:]) == {'B'}, 'one refused call on the first account, then the second serves'
    assert len(calls) > GOAL_TURNS, 'the second account served every turn of the goal loop'
    assert (got['exit_code'], got['turns'], got['failed_turns']) == (0, GOAL_TURNS, 0), got
    assert (got['walls'], got['notices']) == (0, 0), 'no route closed and nothing was sent to the management topic'
    assert not got['reaped_as_quota_exit'] and not got['held_by_the_route'] and got['card_failures'] == 0
    assert got['pool']['e-A'] == 'exhausted' and got['pool']['e-B'] != 'exhausted'


# ── Scenario 2: the three refuse ────────────────────────────────────────

@posix_only
@pytest.mark.parametrize('condition', CONDITIONS)
def test_the_route_closes_once_when_the_three_accounts_refuse_and_reopens_at_the_soonest_reset(
        codex_board, launcher, monkeypatch, ops, condition):
    with kb.connect_closing() as conn:
        got, state = all_three_refuse(conn, launcher, monkeypatch, ops, codex_board, condition)
        assert got['stopped_at_the_call_ceiling'] is False
        assert (got['calls'], got['turns'], got['exit_code']) == ('ABC', 1, kb.KANBAN_RATE_LIMIT_EXIT_CODE), \
            'each account refused once, in the order of the logins, and the worker left by the quota door'
        assert (got['walls'], got['wall_limit'], got['wall_account']) == (1, 'usage_limit_reached', None)
        # The wall's reset time is the soonest of the three, and the route admits its next launch no later
        # than that (at that time on main; sooner where a closed route is probed every hour).
        assert got['wall_reset_in'] == WINDOWS['B'] and 0 < got['route_opens_in'] <= WINDOWS['B']
        assert got['pool'] == {'e-A': 'exhausted', 'e-B': 'exhausted', 'e-C': 'exhausted'}
        assert got['reaped_as_quota_exit'] and not got['reaped_as_crash'] and got['card_failures'] == 0
        assert got['card_status'] == 'ready' and got['held_by_the_route']

        # One notice, in the management topic, with the route and the time; it names no account.
        (topic,), (notice,) = got['notice_topics'], got['notice_texts']
        assert topic == MANAGEMENT_TOPIC
        assert f'Rota {CODEX_PROVIDER}/{CODEX_MODEL} fechada para o perfil default' in notice
        assert 'limite usage_limit_reached, com reinício informado para' in notice and '(conta' not in notice
        assert 'o provedor pediu 2 d de espera' in notice
        assert not [word for word in ('user-', 'acct-', '@', 'login') if word in notice]

        # Until the wall admits its next launch nothing is launched on the route and no second notice goes out.
        state.clock.advance(got['route_opens_in'] - 1)
        tick = kb.dispatch_once(conn)
        assert not tick.spawned and tick.claim_held == [(state.task.id, HELD)], tick
        assert len(ops.notices()) == 1 and state.provider.calls == list('ABC')


# ── Scenario 3: an account is back before the time its provider gave ────

@posix_only
def test_the_probe_reopens_the_route_with_an_account_that_came_back_before_its_time(
        codex_board, launcher, monkeypatch, ops):
    with kb.connect_closing() as conn:
        got = an_account_comes_back_before_its_time(conn, launcher, monkeypatch, ops, codex_board)
    assert got['route_closed_with']['walls'] == 1 and got['launched_one_second_before'] == []
    assert got['launched_when_the_wall_admits_it'] and got['that_launch_is_the_probe']
    calls = got['calls']
    assert 'A' not in calls and calls.count('B') <= 1, 'at most one refused call: the second, when its time had come'
    assert set(calls.lstrip('B')) == {'C'} and calls.count('C') > GOAL_TURNS, 'the third, back early, serves the card'
    assert (got['exit_code'], got['failed_turns'], got['walls']) == (0, 0, 0), 'the provider answered: route open'
    assert got['notices'] == 1, 'the notice of the wall, and none after it'
    assert got['pool']['e-B'] == 'exhausted'


# ── A launch that resolves no credential leaves by the quota door ───────

@posix_only
def test_a_launch_with_the_whole_pool_inside_a_usage_window_leaves_by_the_quota_door(
        codex_board, launcher, monkeypatch, ops):
    """Before the fix this launch left with the generic exit code 1: a failure counted against the card, and,
    when it was the probe of a closed route, a probe that gave no verdict."""
    with kb.connect_closing() as conn:
        got = launch_that_resolves_no_credential(conn, launcher, monkeypatch, ops, codex_board)
    assert (got['exit_code'], got['calls'], got['turns']) == (kb.KANBAN_RATE_LIMIT_EXIT_CODE, '', 0)
    assert set(got['usage_asked']) == set('ABC'), 'each account was asked for its window, and none was back'
    assert (got['walls'], got['wall_limit'], got['wall_account']) == (1, 'usage_limit_reached', None)
    assert got['wall_reset_in'] == WINDOWS['B'] and 0 < got['route_opens_in'] <= WINDOWS['B']
    assert got['reaped_as_quota_exit'] and not got['reaped_as_crash'] and got['card_failures'] == 0
    assert got['held_by_the_route'] and got['notices'] == 1
    assert got['probe'] == {'exit_code': kb.KANBAN_RATE_LIMIT_EXIT_CODE, 'calls': '', 'probes': 1,
                            'probe_released': True, 'walled_again': True, 'reset_in': WINDOWS['B'], 'notices': 1}


def test_only_a_card_worker_refused_by_quota_leaves_by_the_quota_door(monkeypatch):
    import cli as cli_mod

    quota = AuthError('Codex provider quota exhausted (429).', provider=CODEX_PROVIDER, code=CODEX_RATE_LIMITED_CODE)
    missing = AuthError('No Codex credentials stored.', provider=CODEX_PROVIDER, code='codex_auth_missing',
                        relogin_required=True)
    for key in WORKER_ENV:
        monkeypatch.delenv(key, raising=False)
    worker = SimpleNamespace(_runtime_credentials_error=quota, requested_provider=CODEX_PROVIDER, model=CODEX_MODEL)
    assert cli_mod._kanban_route_unresolved_on_quota(worker) is False, 'not a card: the plain 0/1 contract'
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_card')
    for other in (missing, RuntimeError('no route'), None):
        worker._runtime_credentials_error = other
        assert cli_mod._kanban_route_unresolved_on_quota(worker) is False
    assert not walls.any_closed()


# ── The coordinating session: a request is not sent back while an account answers ──

class PooledSession(Session):
    """The coordinating session with the profile's real credential pool behind its agent. Each model call is
    answered for the account whose token the agent holds; with quota, the Principal releases the request of the
    wake as new work."""

    def __init__(self, monkeypatch, tmp_path, root, provider):
        self.accounts = provider
        super().__init__(monkeypatch, tmp_path, root)
        pool = load_pool(CODEX_PROVIDER)
        first = pool.select()
        self.agent._credential_pool, self.agent._credential_pool_entry_id = pool, first.id
        self.agent.api_key, self.agent.base_url = first.access_token, CODEX_BASE_URL
        self.agent._replace_primary_openai_client = lambda **kwargs: True

    def _provider(self, seen):
        answer = self.accounts.answer(account_of(self.agent.api_key), 'Registered as new work.')
        if self.request:
            with kb.connect_closing() as conn:
                delivery.receive_request(conn, **self.request)
        return answer


@pytest.fixture
def principal(intake, monkeypatch, tmp_path):
    runner, event, adapter, inbox = intake
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-codex-cli'))
    clock = Clock(monkeypatch)
    provider = Provider(monkeypatch)
    three_logins(inbox.root)
    session = PooledSession(monkeypatch, tmp_path, inbox.root, provider)
    adapter.handle_message = session.handle_message
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True, message_id='900'))
    return runner, event, session, provider, clock


async def request_turn(runner, event, session, provider, clock, ops, closed):
    """A Telegram request reaches the Principal, whose accounts in ``closed`` are out of quota."""
    start = clock.now
    provider.closed_until.update({name: start + WINDOWS[name] for name in closed})
    await runner._nfos_receive(event)
    await _settle(runner)
    await _tick(runner, clock, ops=ops)
    request, payload, cards = _board()
    wall = walls.read(*ROUTE)
    return {
        'calls': ''.join(provider.calls), 'turns': len(session.results),
        'turn_failed': [bool(result.get('failed')) for result in session.results],
        'request_status': request['status'], 'cards': cards,
        'back_in_the_inbox': (payload.get('coordination') or {}).get('wake_accepted') is False,
        'walls': len(walls.walls()),
        'wall_reset_in': None if wall is None else int(wall['reset_at'] - start),
        'wall_closed_by_the_request': bool(wall and (wall['closed_by'] or {}).get('request_id') == request['id']),
        'notices': len(ops.sent), 'notice_topics': [tuple(row[:2]) for row in ops.sent],
        'notice_texts': [row[2] for row in ops.sent], 'said_in_the_topic_of_who_asked': session.said_in_the_topic(),
    }


@pytest.mark.asyncio
async def test_a_request_is_decided_on_the_second_account_and_never_goes_back_to_the_inbox(principal, ops):
    runner, event, session, provider, clock = principal
    got = await request_turn(runner, event, session, provider, clock, ops, closed='A')
    assert (got['calls'], got['turns'], got['turn_failed']) == ('AB', 1, [False])
    assert (got['request_status'], got['cards'], got['back_in_the_inbox']) == ('attached', 1, False)
    assert (got['walls'], got['notices']) == (0, 0)
    assert not [text for text in got['said_in_the_topic_of_who_asked'] if '429' in text or 'rate' in text.lower()]


@pytest.mark.asyncio
async def test_a_request_goes_back_to_the_inbox_only_when_the_three_accounts_refused(principal, ops):
    runner, event, session, provider, clock = principal
    got = await request_turn(runner, event, session, provider, clock, ops, closed='ABC')
    assert (got['calls'], got['turns'], got['turn_failed']) == ('ABC', 1, [True])
    assert (got['request_status'], got['cards'], got['back_in_the_inbox']) == ('coordinating', 0, True)
    assert (got['walls'], got['wall_closed_by_the_request'], got['wall_reset_in']) == (1, True, WINDOWS['B'])
    assert got['notice_topics'] == [MANAGEMENT_TOPIC] and '(conta' not in got['notice_texts'][0]
    assert got['said_in_the_topic_of_who_asked'] == []


# ── The coordinating session: a turn that resolves no credential ends on quota ──

class GatewayResolvedSession(Session):
    """The coordinating session as the gateway runs its turn: the turn's result is what ``TurnRunner.run_sync``
    returns, and ``run_sync`` resolves the route of the session itself, over the profile's real ``auth.json``.
    With the whole pool inside a usage window on record that resolution finds no credential, and no agent is
    built: the result is the one the gateway hands on for a turn that never started."""

    def __init__(self, monkeypatch, tmp_path, root):
        resolve = gateway_run._resolve_runtime_agent_kwargs  # the gateway's own, before the bootstrap's stand-in
        assert resolve.__name__ == '_resolve_runtime_agent_kwargs'
        super().__init__(monkeypatch, tmp_path, root)
        monkeypatch.setattr(gateway_run, '_resolve_runtime_agent_kwargs', resolve)
        self.turn.user_config = yaml.safe_load((root / 'config.yaml').read_text(encoding='utf-8'))

    async def handle_message(self, event):
        self.wakes.append(event)
        self.turn.source, self.turn._loop_for_step = event.source, asyncio.get_running_loop()
        token = current_notify_receipt.set(event.metadata['kanban_wake_delivery'])
        try:
            result = gateway_run.TurnRunner(self.gateway, self.turn).run_sync()
            self.results.append(result)
            self.gateway._run_agent = AsyncMock(return_value=result)
            handed = await self.gateway._handle_message_with_agent(event, event.source, SESSION_KEY, 1)
        finally:
            current_notify_receipt.reset(token)
            self.gateway._release_turn_lease(SESSION_KEY, 1)
        for _ in range(3):
            await asyncio.sleep(0)
        self.handed.append(handed)


@pytest.fixture
def unresolved_principal(intake, monkeypatch, tmp_path):
    runner, event, adapter, inbox = intake
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-codex-cli'))
    clock = Clock(monkeypatch)
    provider = Provider(monkeypatch, {name: clock.now + WINDOWS[name] for name in 'ABC'})
    three_logins(inbox.root)
    three_walls_on_record(clock, provider)
    session = GatewayResolvedSession(monkeypatch, tmp_path, inbox.root)
    adapter.handle_message = session.handle_message
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True, message_id='900'))
    return runner, event, session, provider, clock


async def unresolved_request_turn(runner, event, session, provider, clock, ops):
    """A Telegram request reaches the Principal while its three accounts are inside a usage window on record."""
    got = await request_turn(runner, event, session, provider, clock, ops, closed='ABC')
    got['usage_asked'] = ''.join(sorted(set(provider.usage_asked)))
    got['turn_result'] = [{key: result.get(key) for key in ('failed', 'failure_reason', 'api_calls')}
                          for result in session.results]
    got['handed_to_the_topic'] = [text for text in session.handed if text]
    return got


@pytest.mark.asyncio
async def test_a_request_turn_that_resolves_no_credential_goes_back_to_the_inbox_and_closes_the_route(
        unresolved_principal, ops):
    """Before the fix the gateway handed "Provider authentication failed: Codex provider quota exhausted (429)"
    to the topic of who asked as the answer of the turn, closed no route and left the request where it was."""
    runner, event, session, provider, clock = unresolved_principal
    got = await unresolved_request_turn(runner, event, session, provider, clock, ops)
    assert (got['calls'], got['turns'], got['usage_asked']) == ('', 1, 'ABC'), 'no call: each window was asked about'
    assert got['turn_result'] == [{'failed': True, 'failure_reason': 'rate_limit', 'api_calls': 0}]
    assert (got['request_status'], got['cards'], got['back_in_the_inbox']) == ('coordinating', 0, True)
    assert (got['walls'], got['wall_closed_by_the_request'], got['wall_reset_in']) == (1, True, WINDOWS['B'])
    assert got['notice_topics'] == [MANAGEMENT_TOPIC] and '(conta' not in got['notice_texts'][0]
    assert not [word for word in ('user-', 'acct-', '@', 'login') if word in got['notice_texts'][0]]
    assert got['said_in_the_topic_of_who_asked'] == []


def test_a_route_that_fails_to_resolve_for_another_reason_is_no_quota_failure():
    """A login that is missing or revoked, a broken setup: the gateway's result for that turn is what it was."""
    missing = AuthError('No Codex credentials stored.', provider=CODEX_PROVIDER, code='codex_auth_missing',
                        relogin_required=True)
    wrapped = RuntimeError('No Codex credentials stored. Run `hermes model` to re-authenticate.')
    wrapped.__cause__ = missing
    for error in (missing, wrapped, RuntimeError('no route'), None):
        assert gateway_run._route_unresolved_on_quota(error, {'model': {'default': CODEX_MODEL}}) == {}
    assert not walls.any_closed()
