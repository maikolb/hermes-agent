"""WORKER_ROUTES_20261016: the workers take the first open route of an ordered list.

On 2026-10-16 the workers ran on one route, OpenRouter, at about US$ 2.6 an hour, after the opencode-go account
ran out of quota. A second OpenCode Go account (Go Plus) is much cheaper per token and has its own limits
(20% of the month in five hours, 50% in a week). ``kanban.delivery.worker_routes`` lists the routes in order of
preference; a route walled by its provider's limit (``nfos_route_walls``) hands its cards to the next route
instead of making them wait. A wall that is the last route's still makes the cards wait.

What these tests hold:

* the choice of route: the first one whose wall does not hold; a route due for its probe is open (the launch that
  goes there is the probe) and, while that probe is in flight, the cards take the reserve; with every route closed
  the first route keeps the cards; a card that pins its own model, a profile with no list and a list with
  unusable entries are untouched;
* with the real dispatcher tick and a real worker: the worker that meets the limit of the first route closes it,
  the next launch (a new card or the same card again) goes to the reserve with the reserve in its argv and its
  route environment, holds a slot and waits for nothing;
* the configuration of the Go Plus route: its own endpoint, its own key from the environment, the client's own
  user agent, and the stable ``x-opencode-session`` of the conversation.
"""
import json
import os
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_principal_review as review, nfos_route_walls as walls
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    PROFILE_CONFIG, Clock, GoUsageLimit, enrol, launcher, option, quota_board,
)
from tests.hermes_cli.test_nfos_route_walls import PROFILE, DAY, refusing, run_card, spawned

PRIMARY = ('opencode-go-plus', 'deepseek-v4.1-flash')
RESERVE = ('openrouter', 'deepseek/deepseek-v4.1-flash')
ROUTES = [{'provider': p, 'model': m} for p, m in (PRIMARY, RESERVE)]


def _task(**fields):
    return SimpleNamespace(assignee=PROFILE, model_override=None, provider_override=None, reasoning_effort=None,
                           id='t_1', **fields)


def _policy(routes=ROUTES):
    return {'worker_model': RESERVE[1], 'worker_provider': RESERVE[0], 'worker_routes': routes}


def _close(route, *, reset_at=None, now=None):
    return walls.close(PROFILE, *route, limit='weekly', reset_at=reset_at, task_id='t_x', now=now)


@pytest.fixture
def board(quota_board):
    return quota_board


def test_the_first_open_route_is_chosen_and_a_closed_one_hands_over_to_the_reserve(board, monkeypatch):
    clock = Clock(monkeypatch)
    assert review.worker_routes(_task(), _policy()) == PRIMARY
    _close(PRIMARY, reset_at=clock.now + 3 * DAY, now=clock.now)
    assert review.worker_routes(_task(), _policy()) == RESERVE, 'the wall of the first route moves the card on'
    _close(RESERVE, now=clock.now)
    assert review.worker_routes(_task(), _policy()) == PRIMARY, 'every route closed: the card waits on the first'


def test_a_route_due_for_its_probe_is_open_and_while_the_probe_flies_the_reserve_takes_the_cards(board, monkeypatch):
    clock = Clock(monkeypatch)
    _close(PRIMARY, reset_at=clock.now + 3 * DAY, now=clock.now)
    clock.advance(3600)  # the hour of the probe
    assert review.worker_routes(_task(), _policy()) == PRIMARY
    assert walls.take_probe(PROFILE, *PRIMARY, task_id='t_probe')
    assert review.worker_routes(_task(), _policy()) == RESERVE, 'one launch probes; the others take the reserve'


def test_a_wall_of_another_profile_does_not_move_the_card(board, monkeypatch):
    Clock(monkeypatch)
    walls.close('another-profile', *PRIMARY, limit='weekly', task_id='t_x')
    assert review.worker_routes(_task(), _policy()) == PRIMARY


@pytest.mark.parametrize('routes', [None, [], [{'provider': 'x'}, 'junk', {'model': 'y'}, {'provider': '', 'model': ''}]])
def test_a_policy_with_no_usable_list_is_left_to_the_single_route_keys(board, routes):
    assert review.worker_routes(_task(), _policy(routes)) is None


def test_the_args_follow_the_chosen_route_and_a_pinned_card_keeps_its_own(board, monkeypatch):
    clock = Clock(monkeypatch)
    config = dict(PROFILE_CONFIG, kanban={'delivery': dict(_policy(), worker_reasoning_effort='max')})
    (board / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    assert review.worker_model_args(_task()) == ['-m', PRIMARY[1], '--provider', PRIMARY[0], '--reasoning', 'max']
    _close(PRIMARY, reset_at=clock.now + DAY, now=clock.now)
    assert review.worker_model_args(_task()) == ['-m', RESERVE[1], '--provider', RESERVE[0], '--reasoning', 'max']
    pinned = _task()
    pinned.model_override, pinned.provider_override = 'pinned-model', 'pinned-provider'
    assert review.worker_model_args(pinned) == ['-m', 'pinned-model', '--provider', 'pinned-provider']


@pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')
def test_the_worker_that_meets_the_limit_closes_the_route_and_the_cards_go_on_on_the_reserve(
        board, launcher, monkeypatch):
    config = dict(PROFILE_CONFIG, kanban={'delivery': dict(PROFILE_CONFIG['kanban']['delivery'], **_policy())})
    (board / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    with kb.connect_closing() as conn:
        clock = Clock(monkeypatch)
        first = enrol(conn, launcher)
        argv, env = launcher.launches[0]
        assert (option(argv, '--provider'), option(argv, '-m')) == PRIMARY
        assert json.loads(env[walls.ROUTE_ENV]) == [PROFILE, *PRIMARY]

        run = run_card(conn, launcher, monkeypatch, first.id, refusing(GoUsageLimit))
        assert run.exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
        assert walls.read(PROFILE, *PRIMARY)['limit'] == 'weekly' and walls.read(PROFILE, *RESERVE) is None

        # the card that met the wall is launched again on the reserve once its own cooldown ends; it is not held
        kb.dispatch_once(conn)  # reaps the exit
        clock.advance(300)
        tick = kb.dispatch_once(conn)
        assert spawned(tick) == [first.id] and not tick.claim_held, tick
        argv, env = launcher.launches[-1]
        assert (option(argv, '--provider'), option(argv, '-m')) == RESERVE
        assert json.loads(env[walls.ROUTE_ENV]) == [PROFILE, *RESERVE]
        card = kb.get_task(conn, first.id)
        assert card.status == 'running' and card.consecutive_failures == 0
        assert kb.count_running_tasks(conn) == 1, 'it holds a slot: nothing waits for the closed route'

        # a new card is born on the reserve as well
        second = enrol(conn, launcher, text='[Japa|1]\nCorrija a ordenação da tela de pedidos.', message_id='124')
        argv, env = launcher.launches[-1]
        assert (option(argv, '--provider'), option(argv, '-m')) == RESERVE
        assert second.id != first.id


GO_PLUS_CONFIG = """
providers:
  opencode-go-plus:
    base_url: https://opencode.ai/inference/go/openai/v1
    key_env: OPENCODE_GO_PLUS_API_KEY
    transport: chat_completions
    extra_headers:
      User-Agent: nexa-factory-os-hermes/1.0
"""


def test_the_go_plus_route_has_its_own_endpoint_key_user_agent_and_a_stable_session_header(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(GO_PLUS_CONFIG, encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENCODE_GO_PLUS_API_KEY', 'test-go-plus-key')
    monkeypatch.setenv('OPENCODE_GO_API_KEY', 'test-old-go-key')
    from agent.opencode_affinity import opencode_session_headers
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested='opencode-go-plus', target_model='deepseek-v4.1-flash')
    assert runtime['base_url'] == 'https://opencode.ai/inference/go/openai/v1'
    assert runtime['api_key'] == 'test-go-plus-key', 'the key of the new account, never the old one'
    assert runtime['api_mode'] == 'chat_completions'
    assert runtime['extra_headers'] == {'User-Agent': 'nexa-factory-os-hermes/1.0'}
    first = opencode_session_headers('opencode-go-plus', runtime['base_url'], 'session-1')
    again = opencode_session_headers('opencode-go-plus', runtime['base_url'], 'session-1')
    assert first == again and first.get('x-opencode-session'), 'stable per conversation'
    assert opencode_session_headers('openrouter', 'https://openrouter.ai/api/v1', 'session-1') == {}
