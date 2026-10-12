"""R3 (b): the Principal on its provider's quota wall for hours.

The plan of record supposes: with the Principal answering 429 for hours, a new request still becomes a card, a
decision marked `stalled_at` is charged again when quota returns, and no error leaks into the requester's topic.

What these tests find on main:

* The decision half holds. The executor is not relaunched and nothing closes during the outage, the decision is
  marked `stalled_at` after three reminders, the card parks as `awaiting_principal`, the reminder keeps coming every
  fifteen minutes, and the first one after quota returns is answered: the card goes back to its executor. In the
  requester's topic the notifier posts no reminder and no question: only the bar of the request moves to paused.
* The request half does not. A Telegram request reaches the intake behind the adapter's attribution line, so the
  imperative-prefix rule never admits it: it is persisted as `coordinating` and handed to the Principal. The wake is
  accepted when the Principal's turn starts, the turn dies on the wall, and the intake never offers the request
  again, during the outage or after it. No card, and no receipt for whoever asked.
* The failure of that request turn reaches the topic of who asked: the gateway rewrites it and hands the adapter
  "The model provider is rate-limiting requests. Please wait a moment and try again." The same failure on a decision
  wake is withheld.

Not exercised here: the gateway's own resume of a session interrupted by a provider rate limit
(`mark_resume_pending(..., "provider_rate_limit")`), which needs a reset time in the 429 and stops after three
automatic attempts. Short of a new message from a person, it is the only way the request turn can come back.

Read on the production boards on 2026-10-12 (read-only): all 213 Telegram requests of the last 30 days entered
through the Principal's inbox, none was admitted by code.
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway.wake import current_notify_receipt
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_42039_duplicate_user_message import _bootstrap
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RecordingAdapter
from tests.gateway.test_nfos_intake import setup  # noqa: F401
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    PROFILE_CONFIG, REQUEST, SOURCE, WORKER_ENV, Clock, agent_on_quota_wall, enrol, launcher, quota_board,
)

posix_only = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

TICK = 300
OUTAGE = 4 * 3600
QUESTION = 'Which of the two sources satisfies criterion C1'
SESSION_KEY = 'agent:main:telegram:group:-1000:8'


class Principal(RecordingAdapter):
    """The coordinating session behind the topic, as the notifier reaches it. A wake starts a turn, which accepts
    the wake. On the quota wall the provider then answers 429 and the turn ends having decided nothing."""

    def __init__(self):
        super().__init__()
        self.fail = False
        self.on_wall = True
        self.answered = []

    async def handle_message(self, event):
        await super().handle_message(event)
        if self.on_wall:
            return
        with kb.connect_closing() as conn:
            for row in delivery.pending_decisions(conn):
                delivery.resolve_decision(conn, row['id'], action='continue', answer='Use the first source.',
                                          author='Principal')
                self.answered.append(row['id'])


class Inbox:
    """The same session as the intake reaches it: a wake that carries the preserved request. With quota, the
    Principal runs the intake command the wake names, which is what releases the request to a worker."""

    def __init__(self, root):
        self.root = root
        self.on_wall = True
        self.wakes = []

    async def handle_message(self, event):
        self.wakes.append(event)
        token = current_notify_receipt.set(event.metadata['kanban_wake_delivery'])
        try:
            agent = SimpleNamespace(session_id='principal', _session_db=SimpleNamespace(db_path=self.root / 'state.db'))
            initialize_agent_turn_checkpoint(agent, turn_id=f'turn-{len(self.wakes)}', user_content=event.text,
                                             messages=[])
        finally:
            current_notify_receipt.reset(token)
        if self.on_wall:
            return
        with kb.connect_closing() as conn:
            delivery.receive_request(conn, **event.metadata['nfos_coordinator_intake']['request'])


def _events(conn, task_id, *kinds):
    marks = ','.join('?' for _ in kinds)
    return [dict(kind=row['kind'], at=row['created_at'], payload=json.loads(row['payload'] or '{}'))
            for row in conn.execute('SELECT kind,created_at,payload FROM task_events WHERE task_id=? '
                                    f'AND kind IN ({marks}) ORDER BY id', (task_id, *kinds))]


@pytest.fixture
def client_project(quota_board):
    """The board is a delivery project whose topic is the one the request came from: a client chat."""
    config = json.loads(json.dumps(PROFILE_CONFIG))
    config['kanban']['delivery']['projects'] = {kb.DEFAULT_BOARD: {
        'enabled': True, 'profile': 'default', 'delivery_type': 'report', 'workers': 2,
        'source': {key: SOURCE[key] for key in ('platform', 'chat_id', 'thread_id')}}}
    (quota_board / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    return quota_board


@pytest.fixture
def intake(setup, monkeypatch):
    runner, event, adapter, root = setup
    for key in WORKER_ENV:
        monkeypatch.delenv(key, raising=False)
    runner._adapter_profile_for_source = lambda source: 'default'
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    inbox = Inbox(root)
    adapter.handle_message = inbox.handle_message
    event.text = REQUEST
    return runner, event, adapter, inbox


async def _settle(runner):
    await asyncio.wait_for(asyncio.gather(*list(getattr(runner, '_background_tasks', ()))), 10)


async def _through_the_outage(runner, clock, seconds):
    """The gateway keeps offering the Principal its unaccepted inbox; the dispatcher keeps looking for requests."""
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        await runner._nfos_retry_coordinator_inputs()
        await _settle(runner)
        with kb.connect_closing() as conn:
            reservation = delivery.reserve_request(conn, capacity=2)
            if reservation:
                delivery.bootstrap_card(conn, reservation['id'], reservation['claim_token'], pid=os.getpid())


def _board():
    with kb.connect_closing() as conn:
        request = dict(conn.execute('SELECT * FROM nfos_requests').fetchone())
        return request, json.loads(request['payload']), conn.execute('SELECT count(*) FROM tasks').fetchone()[0]


@posix_only
def test_stalled_decision_is_charged_again_and_the_card_resumes_when_quota_returns(
        client_project, launcher, monkeypatch):
    principal = Principal()
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
        clock = Clock(monkeypatch)
        start = clock.now
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question=QUESTION,
                                          context={'tried': ['Read the existing evidence']})
        launcher.exit(task.worker_pid, 0)

        def tick():
            result = kb.dispatch_once(conn)
            asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(principal)))
            return result

        assert (task.id, 'open_decision') in tick().claim_held, 'the executor left with its question open'
        while clock.now - start < OUTAGE:
            clock.advance(TICK)
            tick()
            assert kb.get_task(conn, task.id).status in ('ready', 'blocked'), 'nothing closes without the Principal'
        card = kb.get_task(conn, task.id)
        context = json.loads(delivery.get_decision(conn, decision)['context'])
        asked = [int(event['at'] - start) for event in _events(conn, task.id, 'nfos_principal_requested')]
        stalled = [int(event['at'] - start) for event in _events(conn, task.id, 'nfos_decision_stalled')]
        assert delivery.get_decision(conn, decision)['status'] == 'pending' and len(launcher.launches) == 1
        assert stalled == [3300] and context['stalled_at'] == start + 3300
        assert (card.status, card.block_kind) == ('blocked', 'awaiting_principal')
        assert asked == [0, 600, 1500, 2400] + list(range(3600, OUTAGE + 1, 900)), 'charged every 15 min after it'
        assert len(principal.handled) >= len(asked), 'every charge reached the Principal as a wake'
        assert all(event.text.startswith('[kanban]') for event in principal.handled)
        assert principal.answered == []

        principal.on_wall = False
        back = clock.now
        while kb.get_task(conn, task.id).status != 'running' and clock.now - back < 3600:
            clock.advance(TICK)
            tick()
        assert principal.answered == [decision]
        assert delivery.get_decision(conn, decision)['status'] == 'resolved'
        assert kb.get_task(conn, task.id).status == 'running' and len(launcher.launches) == 2
        assert clock.now - back <= 900 + TICK, 'one reminder gap, then the next dispatcher tick'
    internal = ('Principal', QUESTION, decision, '429', 'rate', 'API call failed')
    assert [text for text in principal.sent if any(mark in text for mark in internal)] == []
    assert len(principal.sent) <= 1, 'in the topic of who asked, only the bar of the request moved (to paused)'


@pytest.mark.asyncio
async def test_a_bare_imperative_request_becomes_a_card_without_the_principal(intake, monkeypatch):
    """The path the plan counts on exists: a message that starts with the verb is admitted by code."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    event.text = REQUEST.split('\n', 1)[1]
    assert 'registrado' in (await runner._nfos_receive(event)).lower()
    await _through_the_outage(runner, clock, TICK)
    request, payload, cards = _board()
    assert request['status'] == 'attached' and 'coordination' not in payload and cards == 1
    assert inbox.wakes == []


@pytest.mark.asyncio
async def test_today_a_telegram_request_waits_in_the_principal_inbox_through_the_outage_and_after(
        intake, monkeypatch):
    """What main does today. Delete with the xfail mark below when a request no longer depends on the Principal."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    assert await runner._nfos_receive(event) == '', 'no receipt by code: the message went to the Principal'
    await _settle(runner)
    assert len(inbox.wakes) == 1 and inbox.wakes[0].text.startswith('[NFOS: mensagem original preservada')
    await _through_the_outage(runner, clock, OUTAGE)
    request, payload, cards = _board()
    assert request['status'] == 'coordinating' and payload['coordination']['wake_accepted'] and cards == 0
    assert len(inbox.wakes) == 1, 'accepted once when the turn started; the turn died on the wall'

    inbox.on_wall = False
    await _through_the_outage(runner, clock, 3600)
    request, payload, cards = _board()
    assert len(inbox.wakes) == 1, 'quota is back and the intake still does not offer the request again'
    assert request['status'] == 'coordinating' and cards == 0
    adapter._send_with_retry.assert_not_called()


@pytest.mark.xfail(strict=True, reason=(
    'R3 (b): a Telegram request arrives behind the adapter line "[Sender|id]", so _classify_parallel_intake_message '
    'never returns new_task and _nfos_receive persists it as coordinating, for the Principal. The wake is accepted '
    'at the turn checkpoint, the turn dies on the 429, and _nfos_receipt_candidates skips wake_accepted for good: '
    'no card during the outage and no new offer after it. Fix: an accepted coordinator input whose turn ended '
    'failed is offered again (clear wake_accepted, honour retry_not_before), and one still coordinating past a '
    'deadline becomes pending so the worker creates the card and the Principal classifies it on the card.'))
@pytest.mark.asyncio
async def test_new_request_becomes_a_card_while_the_principal_is_on_the_wall(intake, monkeypatch):
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    await runner._nfos_receive(event)
    await _settle(runner)
    await _through_the_outage(runner, clock, OUTAGE)
    request, payload, cards = _board()
    assert cards == 1, f'{OUTAGE // 3600} h later the request is {request["status"]} and the board has {cards} cards'


def _handed_to_the_topic(monkeypatch, tmp_path, event):
    """What the gateway hands to the adapter for this wake when the Principal's turn dies on the quota wall."""
    agent, seen = agent_on_quota_wall(monkeypatch, provider=None, model='gpt-6-astra')
    result = agent.run_conversation(event.text)
    assert result['failed'] is True and result['failure_reason'] == 'rate_limit'
    runner = _bootstrap(monkeypatch, tmp_path)
    # The keys GatewayRunner._run_agent returns for a turn that ended with text.
    runner._run_agent = AsyncMock(return_value={
        'final_response': result['final_response'], 'messages': [], 'api_calls': result['api_calls'],
        'failed': True, 'failure_reason': result['failure_reason'], 'completed': result['completed'],
        'error': result['error'], 'tools': [], 'history_offset': 0, 'last_prompt_tokens': 0})
    handed = asyncio.run(runner._handle_message_with_agent(event, event.source, SESSION_KEY, 1))
    return handed, result


@posix_only
def test_quota_error_of_a_decision_turn_is_withheld_from_the_topic(client_project, launcher, monkeypatch, tmp_path):
    principal = Principal()
    with kb.connect_closing() as conn:
        task = enrol(conn, launcher)
        delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question=QUESTION,
                               context={'tried': ['Read the existing evidence']})
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(principal)))
    wake = principal.handled[0]
    handed, result = _handed_to_the_topic(monkeypatch, tmp_path, wake)
    assert result['final_response'].startswith('API call failed after 3 retries: HTTP 429')
    assert not handed, handed


@pytest.mark.xfail(strict=True, reason=(
    'R3 (b): _wake_narration_to_suppress only withholds the reply of a wake whose text starts with "[kanban]". The '
    'intake wake starts with "[NFOS: mensagem original preservada", so the failed turn is rewritten by '
    '_sanitize_gateway_final_response and "The model provider is rate-limiting requests. Please wait a moment and '
    'try again." is handed to the adapter, for the topic of who asked. Fix: a turn that ended failed on a provider '
    'quota wall (failure_reason rate_limit or billing) delivers nothing on an internal wake.'))
def test_quota_error_of_a_request_turn_is_withheld_from_the_topic(intake, monkeypatch, tmp_path):
    runner, event, adapter, inbox = intake

    async def receive():
        await runner._nfos_receive(event)
        await _settle(runner)

    asyncio.run(receive())
    handed, result = _handed_to_the_topic(monkeypatch, tmp_path, inbox.wakes[0])
    assert not handed, f'handed to the adapter for the topic of who asked: {handed}'
