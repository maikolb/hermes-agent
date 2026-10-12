"""R3 (b): the Principal on its provider's quota wall for hours or days (PRINCIPAL_WALL_20261012).

A Telegram request reaches the intake behind the adapter's attribution line, is persisted as `coordinating` and is
handed to the Principal, which decides by intent whether it is new work. All 213 Telegram requests of the 30 days
read on the production boards on 2026-10-12 entered that way. That decision is a turn on the Principal's own route.

What main did when that turn died on the wall: the wake had been marked accepted when the turn started, so the
intake never offered the request again, during the outage or after it (no card, no receipt), and the provider's
refusal reached the topic of who asked twice, in English: as the turn's lifecycle status while it ran, and as its
reply ("The model provider is rate-limiting requests. Please wait a moment and try again.").

What these tests hold:

* the request stays persisted and its decision stays owed: the inbox entry goes back to the inbox when the turn ends
  failed on quota (`rate_limit` or `billing`), with the route the session ran on;
* that route is closed like an executor's (`nfos_route_walls`), with one notice in the management topic; while it
  is closed no request is offered to the Principal, the one that met the wall or one that arrives later, so nothing
  is spent on a refused call;
* the same request is offered again as the probe of its route, the oldest request first and one at a time: every
  hour and at the reset time when the provider gave one (whichever comes first), at spaced intervals when it gave
  none; the probe the provider answers reopens the route, the request becomes one card, and a request the
  Principal had released before its turn died is not offered again;
* a turn that ends on quota with no usage window named (a throttle that outlasts the client's retries) closes the
  route too, with no reset time: the request waits on the wall, never on a clock of its own with nobody told;
* a billing verdict the classifier holds unverified is not a quota wall; a delegated child inside the turn is not
  the coordinating session; with several credentials the route reopens at the soonest reset the pool knows;
* the two commands the intake hands the Principal are idempotent for the same request (`receive`, and `urgent`
  by the request id), and a request that comes back says what its dead turn did;
* nothing reaches the topic of who asked while the wall is up: not the reply of the dead turn, not its status;
* a request that enters through the portal becomes a card as before, with the Principal on the wall;
* the decision half is as it was: a stalled decision is charged again and the card resumes when quota returns.

The Principal here is a real agent on the profile's route with the gateway's own handling of the turn it ran
(`GatewayRunner._handle_message_with_agent`, the turn checkpoint, the status bridge). The provider, the clock and
the Telegram adapter are stand-ins. With quota, the stand-in provider does what the Principal does for independent
work: it runs the intake with the exact request of the wake.
"""
import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway import run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.turn_context import TurnContext
from gateway.wake import current_notify_receipt
from hermes_cli import kanban_db as kb, nfos_delivery as delivery, nfos_route_walls as walls
from hermes_cli.nfos_runtime import resumed_intake_note
from tests.gateway.test_42039_duplicate_user_message import _bootstrap
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RecordingAdapter
from tests.gateway.test_nfos_intake import setup  # noqa: F401
from tests.hermes_cli.nfos_quota_wall import (  # noqa: F401
    PROFILE_CONFIG, REQUEST, SOURCE, WORKER_ENV, Clock, CodexUsageLimit, GoUsageLimitFromSDK, ProviderRateLimit,
    agent_on_quota_wall, enrol, launcher, metered_reply, quota_board,
)
from tests.hermes_cli.test_nfos_route_walls import MANAGEMENT_TOPIC, Operations

posix_only = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

TICK = 300
OUTAGE = 4 * 3600
QUESTION = 'Which of the two sources satisfies criterion C1'
SESSION_KEY = 'agent:main:telegram:group:-1000:8'
# The Principal's provider and model on the production profile (read on 2026-10-12; there the profile is
# `hermes-project-factory`, for the gateway process and for the intake alike, and here it is the suite's `default`),
# and the distance of the reset in the refusal its log has on 2026-10-09: `usage_limit_reached`,
# `limit_window_minutes: 10080`, `resets_in_seconds: 522167`.
PRINCIPAL = ('default', PROFILE_CONFIG['model']['provider'], PROFILE_CONFIG['model']['default'])
WEEKLY_RESET = 522167
SECOND_REQUEST = '[Japa|1]\nCorrija a ordenação da tela de pedidos.'
PORTAL = {'platform': 'portal', 'chat_id': 'dov', 'thread_id': 'suporte', 'message_id': 'DV-0031',
          'chat_type': 'portal', 'user_id': 'portal:dov:lucas'}


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
    """The same session as the intake reaches it, reduced to the start of its turn: the wake's content is
    checkpointed, which accepts the wake, and nothing else happens."""

    def __init__(self, root):
        self.root = root
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


class Session:
    """The coordinating session behind the topic, whole: a wake starts a turn of a real agent on the Principal's
    route, the turn writes its checkpoint (which is what accepts the wake), its status goes through the gateway's
    status bridge and its result through the gateway's own handler. What that handler returns is what the adapter
    sends to the topic the wake was delivered in, which is the topic of who asked.

    `refusal` builds what the provider answers while the Principal is on the wall; `None` means it has quota, and
    the Principal then releases the request of the wake as new work."""

    def __init__(self, monkeypatch, tmp_path, root):
        from hermes_state import SessionDB
        self.refusal = None
        self.releases_then_dies = False
        self.request = None
        self.wakes, self.calls, self.results, self.handed, self.status_sent = [], [], [], [], []
        self.agent, self.seen = agent_on_quota_wall(monkeypatch, provider=PRINCIPAL[1], model=PRINCIPAL[2],
                                                    answer=self._provider)
        # The stand-in provider answers in the chat-completions shape, whatever transport the route would use.
        self.agent.api_mode = 'chat_completions'
        self.agent._session_db = SessionDB(db_path=root / 'state.db')
        self.gateway = _bootstrap(monkeypatch, tmp_path)
        self.turn = TurnContext(_run_still_current=lambda: True)
        self.turn._status_adapter, self.turn._status_chat_id = self, SOURCE['chat_id']
        self.turn._status_thread_metadata = {'thread_id': SOURCE['thread_id']}
        self.turn.agent_holder[0] = self.agent
        self.agent.status_callback = gateway_run.TurnRunner(self.gateway, self.turn)._status_callback_sync

    def _provider(self, seen):
        self.calls.append(time.time())
        if self.request and (self.refusal is None or self.releases_then_dies):
            with kb.connect_closing() as conn:
                delivery.receive_request(conn, **self.request)
        if self.refusal is not None:
            raise self.refusal()
        return metered_reply('Registered as new work.')

    def other_turn(self, text):
        """A turn of the same session that is not the turn of a request (a card's question, say). Only its effect
        on the session's checkpoint matters here, so its own status goes nowhere."""
        self.request, bridge, self.agent.status_callback = None, self.agent.status_callback, None
        try:
            return self.agent.run_conversation(text)
        finally:
            self.agent.status_callback = bridge

    async def send(self, chat_id, content, metadata=None):
        """The topic's adapter, for the status bridge."""
        self.status_sent.append(content)
        return SendResult(success=True, message_id=str(len(self.status_sent)))

    async def handle_message(self, event):
        self.wakes.append(event)
        self.request = event.metadata['nfos_coordinator_intake']['request']
        self.turn.source, self.turn._loop_for_step = event.source, asyncio.get_running_loop()
        receipt = event.metadata['kanban_wake_delivery']
        token = current_notify_receipt.set(receipt)
        try:
            result = self.agent.run_conversation(event.text)
            self.results.append(result)
            # The keys GatewayRunner._run_agent returns for a turn that ended with text.
            self.gateway._run_agent = AsyncMock(return_value={
                'final_response': result['final_response'], 'messages': [], 'api_calls': result['api_calls'],
                'failed': bool(result.get('failed')), 'failure_reason': result.get('failure_reason'),
                'completed': result['completed'], 'error': result.get('error'), 'tools': [], 'history_offset': 0,
                'last_prompt_tokens': 0, 'model': self.agent.model})
            handed = await self.gateway._handle_message_with_agent(event, event.source, SESSION_KEY, 1)
        finally:
            current_notify_receipt.reset(token)
            self.gateway._release_turn_lease(SESSION_KEY, 1)  # as the handler's caller does when the turn is over
        for _ in range(3):  # let the status bridge run what it scheduled on this loop
            await asyncio.sleep(0)
        self.handed.append(handed)

    def said_in_the_topic(self):
        """Everything the gateway handed to the topic of who asked: turn replies and status messages."""
        return [text for text in self.handed if text] + list(self.status_sent)

    def offered(self):
        """The request each wake carried, in the order the wakes came."""
        return [event.metadata['nfos_coordinator_intake']['request_id'] for event in self.wakes]


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
    # The profile's own config: it names the route the Principal runs on.
    (root / 'config.yaml').write_text(yaml.safe_dump(PROFILE_CONFIG), encoding='utf-8')
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    inbox = Inbox(root)
    adapter.handle_message = inbox.handle_message
    event.text = REQUEST
    return runner, event, adapter, inbox


@pytest.fixture
def desk(intake, monkeypatch, tmp_path):
    """The intake with the whole coordinating session behind the topic, and a Telegram that confirms what it sends."""
    runner, event, adapter, inbox = intake
    session = Session(monkeypatch, tmp_path, inbox.root)
    adapter.handle_message = session.handle_message
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True, message_id='900'))
    return runner, event, adapter, session


@pytest.fixture
def ops():
    return Operations()


async def _settle(runner):
    await asyncio.wait_for(asyncio.gather(*list(getattr(runner, '_background_tasks', ()))), 10)


async def _tick(runner, clock, seconds=TICK, ops=None):
    """One round of the gateway: the Principal is offered its unaccepted inbox, receipts owed are sent, the wall's
    notice goes out, and the dispatcher turns a released request into its card."""
    clock.advance(seconds)
    await runner._nfos_retry_coordinator_inputs()
    await _settle(runner)
    await runner._nfos_retry_receipts()
    if ops is not None:
        await ops.runner._kanban_route_wall_notices()
    with kb.connect_closing() as conn:
        reservation = delivery.reserve_request(conn, capacity=5)
        if reservation:
            delivery.bootstrap_card(conn, reservation['id'], reservation['claim_token'], pid=os.getpid())


async def _through(runner, clock, seconds, ops=None, step=TICK):
    for _ in range(seconds // step):
        await _tick(runner, clock, step, ops)


def _requests():
    """Every request of the board in arrival order, each with its payload."""
    with kb.connect_closing() as conn:
        return [dict(row, payload=json.loads(row['payload']))
                for row in map(dict, conn.execute('SELECT * FROM nfos_requests ORDER BY created_at,id'))]


def _cards():
    with kb.connect_closing() as conn:
        return [dict(row) for row in conn.execute('SELECT id,title,body FROM tasks ORDER BY created_at,id')]


def _board():
    with kb.connect_closing() as conn:
        request = dict(conn.execute('SELECT * FROM nfos_requests').fetchone())
        return request, json.loads(request['payload']), conn.execute('SELECT count(*) FROM tasks').fetchone()[0]


def _second(event):
    """Another message of the same person in the same topic."""
    return MessageEvent(text=SECOND_REQUEST, message_id='124', message_type=MessageType.TEXT, source=event.source)


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
    """A message that starts with the verb is admitted by code."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    event.text = REQUEST.split('\n', 1)[1]
    assert 'registrado' in (await runner._nfos_receive(event)).lower()
    await _tick(runner, clock)
    request, payload, cards = _board()
    assert request['status'] == 'attached' and 'coordination' not in payload and cards == 1
    assert inbox.wakes == []


@pytest.mark.asyncio
async def test_the_same_request_is_resumed_once_by_the_first_hourly_probe_the_provider_answers(
        desk, monkeypatch, ops):
    """The provider gave a reset time six days away. The route is tried every hour all the same, by the oldest
    request: the limit may be lifted before that time (quota bought, plan changed), and nobody should have to
    erase a file for the Principal to find out."""
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    session.refusal = lambda: CodexUsageLimit(resets_in=int(start + WEEKLY_RESET - clock.now))

    assert await runner._nfos_receive(event) == '', 'no receipt by code: the message went to the Principal'
    await _settle(runner)
    request, payload, cards = _board()
    request_id, entry = request['id'], payload['coordination']

    # The turn met the usage window on its first call and ended there, having decided nothing.
    assert [(r['failed'], r['failure_reason']) for r in session.results] == [(True, 'rate_limit')]
    assert (session.seen.provider_calls, session.seen.waits.waited) == (1, 0)
    # The request is where it was, its decision owed: back in the inbox, with the route it waits for.
    assert (request['status'], request['task_id'], cards) == ('coordinating', None, 0)
    assert payload['text'] == REQUEST and payload['source']['message_id'] == '123'
    assert entry['wake_accepted'] is False and 'claim_token' not in entry
    assert entry['quota']['route'] == list(PRINCIPAL) and entry['quota']['reset_at'] == start + WEEKLY_RESET
    assert (entry['quota']['reason'], entry['quota']['turns']) == ('rate_limit', 1)
    # The Principal's route is closed by that request. The wall keeps the reset time the provider gave, and the
    # route's next launch is the probe of the hour, which comes first.
    wall = walls.read(*PRINCIPAL)
    assert (wall['limit'], wall['reset_at']) == ('usage_limit_reached', start + WEEKLY_RESET)
    assert wall['closed_by']['request_id'] == request_id and walls.open_at(wall) == start + 3600
    assert session.said_in_the_topic() == [], 'neither the reply of the dead turn nor its status'

    # Four hours on the wall. Between two probes nothing is offered and no call is spent. Another turn of the same
    # session starts meanwhile (a card's question): it replaces the checkpoint of the dead turn, which flushes
    # that turn's acceptance. The request stays owed.
    await _through(runner, clock, 3600 - TICK, ops)
    assert session.offered() == [request_id] and session.seen.provider_calls == 1, 'nothing before the hour'
    await _through(runner, clock, OUTAGE // 2 - (3600 - TICK), ops)
    assert session.other_turn('[kanban] a card asks')['failure_reason'] == 'rate_limit'
    await _through(runner, clock, OUTAGE // 2, ops)
    request, payload, cards = _board()
    # One probe an hour, by the same request: one refused call each, which is the probe's verdict.
    assert session.offered() == [request_id] * 5, 'the turn that met the wall, then one probe an hour'
    assert [int(at - start) for at in session.calls] == [0, 3600, 7200, 7200, 10800, 14400]
    assert session.seen.provider_calls == 6 and session.seen.waits.waited == 0, 'the fourth call is the other turn'
    assert (request['status'], cards, payload['coordination']['wake_accepted']) == ('coordinating', 0, False)
    wall = walls.read(*PRINCIPAL)
    assert (wall['probes'], wall['probe'], wall['reset_at']) == (4, None, start + WEEKLY_RESET)
    assert walls.open_at(wall) == clock.now + 3600 and wall['closed_at'] == start, 'the same wall, tried again in 1 h'
    (chat, topic, notice), = ops.sent
    assert (chat, topic) == MANAGEMENT_TOPIC, 'the one notice of the wall, in the management topic'
    assert f'Rota {PRINCIPAL[1]}/{PRINCIPAL[2]} fechada para o perfil {PRINCIPAL[0]}' in notice
    assert time.strftime('%d/%m %H:%M UTC', time.gmtime(start + WEEKLY_RESET)) in notice
    assert 'a cada 60 min' in notice and 'mesmo antes da hora informada' in notice
    assert 'sessão coordenadora (Principal)' in notice and 'retomada, na ordem de chegada' in notice
    adapter._send_with_retry.assert_not_called()
    assert session.said_in_the_topic() == [], 'a refused probe says nothing in the topic of who asked either'

    # The limit is lifted days before the time the provider gave. One tick before the next probe: nothing. At the
    # probe: the same request, and the provider answers it.
    session.refusal = None
    await _through(runner, clock, 3600 - TICK, ops)
    assert session.offered() == [request_id] * 5 and _board()[2] == 0
    await _tick(runner, clock, TICK, ops)
    request, payload, cards = _board()
    assert session.offered() == [request_id] * 6, 'the same request, by its id'
    assert clock.now - start == 5 * 3600 and clock.now < start + WEEKLY_RESET - 5 * 86400, 'days before that time'
    first, again = (wake.metadata['nfos_coordinator_intake']['request'] for wake in session.wakes[:2])
    assert first == again and again['source']['message_id'] == '123' and again['text'] == REQUEST
    assert all(wake.metadata['nfos_coordinator_intake']['request'] == first for wake in session.wakes)
    # The second offer is the first one plus what became of it: the provider answered no call of that turn.
    told = resumed_intake_note(0)
    assert 'nothing was decided or done for it' in told and told not in session.wakes[0].text
    assert session.wakes[1].text.replace(told, '') == session.wakes[0].text
    assert f'--task CARD_ID --input urgency.json --request {request_id}' in session.wakes[0].text
    assert (request['id'], request['status'], cards) == (request_id, 'attached', 1), 'it became its card'
    (card,) = _cards()
    assert request['task_id'] == card['id'] and REQUEST.split('\n', 1)[1] in card['body']
    assert walls.read(*PRINCIPAL) is None, 'the provider answered: the route is open'
    # Whoever asked gets the intake's own receipt for the message they sent, now and only now.
    (receipt,) = adapter._send_with_retry.await_args_list
    assert receipt.kwargs['reply_to'] == '123' and receipt.kwargs['content'].startswith(f'Pedido registrado: {request_id}')

    await _through(runner, clock, 3600, ops)
    assert session.offered() == [request_id] * 6 and len(_cards()) == 1, 'once: no further offer, no second card'
    assert adapter._send_with_retry.await_count == 1 and len(ops.sent) == 1
    (said,) = session.said_in_the_topic()
    assert said.startswith('Registered as new work.'), 'only what the Principal said with quota'


@pytest.mark.asyncio
async def test_with_no_reset_time_the_oldest_request_probes_the_route_at_spaced_intervals(desk, monkeypatch, ops):
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    session.refusal = GoUsageLimitFromSDK  # a named limit and no reset time: the end of the window is unknown

    await runner._nfos_receive(event)
    await _settle(runner)
    request_id = _board()[0]['id']
    wall = walls.read(*PRINCIPAL)
    assert (wall['limit'], wall['reset_at'], wall['account']) == ('weekly', None, 'wrk_quota_wall_suite')
    assert session.results[0]['failure_reason'] == 'billing' and session.seen.provider_calls == 1

    # Probes at the respawn guard's spacing, kept for the route: 300 s, then 600 s after it, then 1200 s.
    await _through(runner, clock, 2040, ops, step=60)
    assert [int(at - start) for at in session.calls] == [0, 300, 900], 'one refused call per probe, nothing between'
    request, payload, cards = _board()
    assert (request['status'], cards, payload['coordination']['quota']['turns']) == ('coordinating', 0, 3)
    wall = walls.read(*PRINCIPAL)
    assert (wall['probes'], wall['probe'], wall['next_probe_at']) == (2, None, start + 2100)
    assert len(ops.sent) == 1 and 'sem hora de reinício informada' in ops.sent[0][2], 'a refused probe sends no notice'
    assert session.said_in_the_topic() == [] and not adapter._send_with_retry.await_count

    session.refusal = None
    await _through(runner, clock, 120, ops, step=60)
    request, payload, cards = _board()
    assert [int(at - start) for at in session.calls] == [0, 300, 900, 2100], 'the probe the provider answers'
    assert session.offered() == [request_id] * 4 and (request['status'], cards) == ('attached', 1)
    assert walls.read(*PRINCIPAL) is None
    await _through(runner, clock, 3600, ops)
    assert len(session.calls) == 4 and len(_cards()) == 1 and len(ops.sent) == 1


@pytest.mark.asyncio
async def test_two_requests_held_by_the_wall_are_resumed_in_the_order_they_arrived(desk, monkeypatch, ops):
    """Here the reset time the provider gave comes before the probe of the hour: it brings the launch forward."""
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    reset = start + 1800
    session.refusal = lambda: CodexUsageLimit(resets_in=int(reset - clock.now))

    await runner._nfos_receive(event)
    await _settle(runner)
    await _through(runner, clock, 600, ops)
    # The second request arrives with the route closed: it is persisted, and not offered.
    assert await runner._nfos_receive(_second(event)) == ''
    await _settle(runner)
    first, second = _requests()
    assert [r['status'] for r in (first, second)] == ['coordinating', 'coordinating']
    assert second['payload']['text'] == SECOND_REQUEST and 'quota' not in second['payload']['coordination']
    assert session.offered() == [first['id']] and session.seen.provider_calls == 1, 'no call for the second one'
    assert walls.open_at(walls.read(*PRINCIPAL)) == reset < walls.read(*PRINCIPAL)['next_probe_at'] == start + 3600

    await _tick(runner, clock, int(reset - 1 - clock.now), ops)
    assert session.offered() == [first['id']] and not _cards()
    session.refusal = None
    await _tick(runner, clock, 1, ops)
    assert session.offered() == [first['id'], first['id']], 'the oldest first, alone: it is the probe of the route'
    assert [r['status'] for r in _requests()] == ['attached', 'coordinating'] and len(_cards()) == 1
    await _tick(runner, clock, 5, ops)
    assert session.offered() == [first['id'], first['id'], second['id']]
    first, second = _requests()
    cards = _cards()
    assert [r['status'] for r in (first, second)] == ['attached', 'attached']
    assert [first['task_id'], second['task_id']] == [card['id'] for card in cards], 'one card each, in that order'
    assert 'total' in cards[0]['body'] and 'ordenação' in cards[1]['body']
    assert [call.kwargs['reply_to'] for call in adapter._send_with_retry.await_args_list] == ['123', '124']

    await _through(runner, clock, 3600, ops)
    assert len(session.wakes) == 3 and len(_cards()) == 2 and len(ops.sent) == 1
    assert len(session.said_in_the_topic()) == 2 and not session.status_sent


@pytest.mark.asyncio
async def test_a_portal_request_becomes_a_card_as_before_with_the_principal_on_the_wall(desk, monkeypatch, ops):
    """Holds on main as well: this path never went through the Principal, and the wall does not change it."""
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    session.refusal = lambda: CodexUsageLimit(resets_in=WEEKLY_RESET)
    await runner._nfos_receive(event)
    await _settle(runner)
    assert session.results[0]['failure_reason'] == 'rate_limit', 'the Principal is on the wall'

    with kb.connect_closing() as conn:
        ticket = delivery.receive_request(
            conn, source=PORTAL, text='[Lucas (suporte)|portal:dov:lucas] Página de acompanhamento',
            project={'profile': 'default', 'delivery_type': 'report'}, origin={'portal': {'chamado': 'DV-0031'}},
            support_approval={'required': True, 'approved_at': int(clock.now), 'approved_by': 'Maikol',
                              'decided_at': int(clock.now), 'decided_by': 'Maikol', 'decision': 'approved'})
        assert delivery.get_request(conn, ticket)['status'] == 'pending', 'no Principal between a ticket and its card'
    await _through(runner, clock, 600, ops)
    telegram, portal = sorted(_requests(), key=lambda r: r['payload']['source']['platform'] == 'portal')
    (card,) = _cards()
    assert (portal['id'], portal['status'], portal['task_id']) == (ticket, 'attached', card['id'])
    assert 'Página de acompanhamento' in card['body']
    assert telegram['status'] == 'coordinating' and len(session.wakes) == 1, 'no turn of the Principal for the ticket'
    assert session.seen.provider_calls == 1 and not adapter._send_with_retry.await_count


@pytest.mark.asyncio
async def test_a_request_the_principal_released_before_its_turn_died_is_not_offered_again(desk, monkeypatch, ops):
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    reset = start + 3600
    session.refusal = lambda: CodexUsageLimit(resets_in=int(reset - clock.now))
    session.releases_then_dies = True  # the intake command ran, and the call after it met the window

    await runner._nfos_receive(event)
    await _settle(runner)
    request, payload, cards = _board()
    assert session.results[0]['failure_reason'] == 'rate_limit'
    assert request['status'] == 'pending' and 'quota' not in payload['coordination'], 'released: nothing to return'
    await _tick(runner, clock, 60, ops)
    assert len(_cards()) == 1

    session.refusal, session.releases_then_dies = None, False
    await _through(runner, clock, 2 * 3600, ops)
    request, payload, cards = _board()
    assert (request['status'], cards) == ('attached', 1) and len(session.wakes) == 1, 'one card, no second offer'
    assert session.said_in_the_topic() == []


@pytest.mark.asyncio
async def test_a_turn_that_ends_on_quota_with_no_window_named_closes_the_route_with_no_reset_time(
        desk, monkeypatch, ops):
    """A plain 429 with no limit named and no reset time, refused past the client's own retries: the turn ended on
    quota all the same. The request is not left on a clock of its own with nobody told: the route closes with no
    reset time, the management topic gets the wall's notice, and one probe at the route's spacing resumes it."""
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    session.refusal = ProviderRateLimit

    await runner._nfos_receive(event)
    await _settle(runner)
    request, payload, cards = _board()
    request_id, entry = request['id'], payload['coordination']
    assert session.results[0]['failure_reason'] == 'rate_limit' and session.seen.provider_calls == 3
    assert (request['status'], entry['wake_accepted'], cards) == ('coordinating', False, 0)
    assert entry['quota']['route'] == list(PRINCIPAL) and entry['quota']['reset_at'] is None
    assert entry['next_attempt_at'] == start, 'no clock on the request: the wall is what holds it'
    wall = walls.read(*PRINCIPAL)
    assert (wall['limit'], wall['reset_at'], wall['next_probe_at']) == ('rate_limit', None, start + 300)
    assert wall['closed_by']['request_id'] == request_id

    # A second request arrives: it waits behind the first, which is the one that probes. A probe the provider
    # refuses leaves on that refusal: one call, not the client's three.
    await _through(runner, clock, 120, ops, step=60)
    await runner._nfos_receive(_second(event))
    await _settle(runner)
    await _through(runner, clock, 720, ops, step=60)
    first, second = _requests()
    assert session.offered() == [first['id'], first['id']], 'the oldest probes; the newer one does not pass it'
    assert [int(at - start) for at in session.calls] == [0, 0, 0, 300] and session.seen.provider_calls == 4
    wall = walls.read(*PRINCIPAL)
    assert (wall['probes'], wall['probe'], wall['next_probe_at']) == (1, None, start + 900)
    (chat, topic, notice), = ops.sent
    assert (chat, topic) == MANAGEMENT_TOPIC and 'sem hora de reinício informada' in notice
    assert 'sessão coordenadora (Principal)' in notice
    assert session.said_in_the_topic() == [] and not adapter._send_with_retry.await_count

    session.refusal = None
    await _through(runner, clock, 180, ops, step=60)
    first, second = _requests()
    assert [int(at - start) for at in session.calls][4:] == [900, 900 + 60]
    assert session.offered() == [first['id']] * 3 + [second['id']], 'then the other one, in the order they arrived'
    assert [r['status'] for r in (first, second)] == ['attached', 'attached'] and len(_cards()) == 2
    assert walls.read(*PRINCIPAL) is None and len(ops.sent) == 1
    assert len(session.said_in_the_topic()) == 2 and not session.status_sent


@pytest.mark.asyncio
@pytest.mark.parametrize('refusal,notices', [
    (lambda: CodexUsageLimit(resets_in=WEEKLY_RESET), 1), (GoUsageLimitFromSDK, 1), (ProviderRateLimit, 1),
], ids=['window-with-reset', 'named-window-no-reset', 'plain-throttle'])
async def test_nothing_is_said_in_the_topic_of_who_asked_while_the_principal_is_on_the_wall(
        desk, monkeypatch, ops, refusal, notices):
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    session.refusal = refusal
    await runner._nfos_receive(event)
    await _settle(runner)
    await _through(runner, clock, 600, ops)
    await runner._nfos_receive(_second(event))
    await _settle(runner)
    await _through(runner, clock, OUTAGE, ops)
    assert all(result['failed'] for result in session.results) and session.results
    assert [r['status'] for r in _requests()] == ['coordinating'] * 2 and not _cards()
    assert session.handed and not any(session.handed), 'no reply of a turn that died on quota'
    assert session.status_sent == [], 'no status of it either'
    adapter._send_with_retry.assert_not_called()
    # What the gateway did send went to the management topic, once per wall, and is the runtime's own text.
    assert len(ops.sent) == notices
    assert all((chat, topic) == MANAGEMENT_TOPIC and text.startswith('Rota ') for chat, topic, text in ops.sent)


@pytest.mark.asyncio
async def test_a_turn_the_provider_had_answered_before_it_died_is_told_so_when_its_request_comes_back(
        intake, monkeypatch):
    """The window can close between two calls of one turn. By then the Principal may have acted on the message
    without releasing it (a correction on an existing card, an urgency): the request comes back all the same,
    because nothing says the decision was finished, and the next offer tells the Principal what happened."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    await runner._nfos_receive(event)
    await _settle(runner)
    (wake,) = inbox.wakes
    died = {'failed': True, 'failure_reason': 'rate_limit', 'api_calls': 3, 'model': PRINCIPAL[2],
            'retry_not_before': clock.now + 3600}
    await runner._nfos_turn_ended(wake, event.source, died, clock.now)
    request, payload, cards = _board()
    entry = payload['coordination']
    assert (request['status'], entry['wake_accepted'], entry['quota']['answered_calls']) == ('coordinating', False, 2)
    # The intake is what offers the request again: the gateway's own continuation of a rate-limited turn, which
    # it schedules from this field, would run the same turn a second time.
    assert 'retry_not_before' not in died

    await _tick(runner, clock, 300)
    first, again = inbox.wakes
    assert again.metadata['nfos_coordinator_intake']['request'] == first.metadata['nfos_coordinator_intake']['request']
    assert again.text.startswith('[NFOS: mensagem original preservada') and REQUEST in again.text
    assert 'NFOS resumed coordination' not in first.text
    assert 'after 2 answered model call(s)' in again.text and 'must not run again' in again.text


def test_with_a_route_closed_the_oldest_request_of_any_board_is_offered_first(intake, monkeypatch, tmp_path):
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    boards, ids = [], {}
    # The board the scan reads first holds the newer request.
    for slug, message_id, later in (('alpha', '201', 600), ('beta', '202', 0)):
        path = tmp_path / f'{slug}.db'
        monkeypatch.setattr(time, 'time', lambda later=later: clock.now + later)
        with kb.connect_closing(path) as conn:
            delivery.init_schema(conn)
            ids[slug] = delivery.receive_request(
                conn, source=dict(SOURCE, message_id=message_id), text=REQUEST,
                project={'profile': 'default', 'delivery_type': 'report'}, defer_to_principal=True)
        boards.append({'slug': slug, 'db_path': str(path)})
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    monkeypatch.setattr(kb, 'list_boards', lambda include_archived=False: boards)

    def offered():
        return [rid for path, rid, payload in runner._nfos_receipt_candidates({'default'}, coordination=True)]

    clock.advance(900)
    assert offered() == [ids['alpha'], ids['beta']], 'no route closed: every board is offered at once, as before'
    walls.close(*PRINCIPAL, limit='usage_limit_reached', reset_at=clock.now + 3600, request_id=ids['beta'])
    assert offered() == [], 'the route of both is closed'
    clock.advance(3600)
    assert offered() == [ids['beta'], ids['alpha']], 'the oldest first: its turn is the probe of the route'


def test_a_turn_the_provider_answers_reopens_the_route_of_its_profile(intake, monkeypatch):
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    answered = {'final_response': 'ok', 'failed': False, 'api_calls': 2, 'model': PRINCIPAL[2]}
    person = MessageEvent(text='E o card da folha?', message_type=MessageType.TEXT, source=event.source)

    def ended(result, started_at):
        return asyncio.run(runner._nfos_turn_ended(person, event.source, result, started_at))

    walls.close(*PRINCIPAL, limit='usage_limit_reached', reset_at=clock.now + WEEKLY_RESET, request_id='req_x')
    clock.advance(60)
    # A turn begun before the wall was met, a turn on another model, a turn that failed, a turn with no call.
    ended(answered, clock.now - 120)
    ended(dict(answered, model='gpt-5.6-luna'), clock.now - 1)
    ended(dict(answered, failed=True, failure_reason='server_error'), clock.now - 1)
    ended(dict(answered, api_calls=0), clock.now - 1)
    assert walls.read(*PRINCIPAL) is not None
    # A turn of the profile's own model, begun after the wall and answered: the window is over, whatever reset
    # time the provider had given.
    ended(answered, clock.now - 1)
    assert walls.read(*PRINCIPAL) is None


def test_only_a_turn_nobody_typed_keeps_its_quota_refusal_out_of_the_chat(intake):
    runner, event, adapter, inbox = intake
    on_quota = {'failed': True, 'failure_reason': 'rate_limit', 'final_response': 'API call failed after 1 retries'}
    receipt = {'kanban_wake_delivery': {'delivery_id': 'nfos-input:req_x'}}

    def turn(text, internal, metadata=None):
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=event.source, internal=internal,
                            metadata=metadata)

    withheld = gateway_run._runtime_turn_ended_on_quota
    # A wake the runtime delivered with its receipt, and the gateway's own continuation of an interrupted turn.
    assert withheld(turn('[NFOS: mensagem original preservada', True, receipt), on_quota)
    assert withheld(turn('[kanban] Task t_1 asks', True, receipt), dict(on_quota, failure_reason='billing'))
    assert withheld(turn('', True), on_quota)
    # A person's message keeps the gateway's answer about the provider, as before.
    assert not withheld(turn('E o card da folha?', False), on_quota)
    # Other synthetic turns a person set off (a loop tick, a handoff) are not the runtime's wakes.
    assert not withheld(turn('[Session was just handed off from CLI', True), on_quota)
    # A failure that is not quota, and a turn that did not fail, are untouched.
    assert not withheld(turn('', True, receipt), dict(on_quota, failure_reason='server_error'))
    # Nor is a billing verdict the classifier itself holds unverified: it may be this message being rejected.
    assert not withheld(turn('', True, receipt), dict(on_quota, failure_reason='billing', billing_unverified=True))
    assert not withheld(turn('', True, receipt), {'failed': False, 'final_response': 'Entregue'})


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


def test_quota_error_of_a_request_turn_is_withheld_from_the_topic(intake, monkeypatch, tmp_path):
    runner, event, adapter, inbox = intake

    async def receive():
        await runner._nfos_receive(event)
        await _settle(runner)

    asyncio.run(receive())
    assert _board()[1]['coordination']['wake_accepted'] is True, 'accepted when the turn started'
    handed, result = _handed_to_the_topic(monkeypatch, tmp_path, inbox.wakes[0])
    assert not handed, f'handed to the adapter for the topic of who asked: {handed}'
    request, payload, cards = _board()
    assert request['status'] == 'coordinating' and payload['coordination']['wake_accepted'] is False
    assert payload['coordination']['quota']['reason'] == 'rate_limit', 'and the request is back in the inbox'


def _received(runner, event, inbox):
    """The request is in the inbox and its turn has started: the wake it was delivered with, and its receipt."""
    async def receive():
        await runner._nfos_receive(event)
        await _settle(runner)

    asyncio.run(receive())
    (wake,) = inbox.wakes
    return wake, wake.metadata['kanban_wake_delivery']


def _session_agent(provider=PRINCIPAL[1], model=PRINCIPAL[2], **more):
    """What `on_quota_error` reads of the agent a refusal reached."""
    return SimpleNamespace(provider=provider, model=model, _fallback_activated=False,
                           _has_pending_fallback=lambda: False, **more)


def test_a_delegated_child_inside_the_turn_of_a_request_is_not_the_coordinating_session(intake, monkeypatch):
    """A child the Principal delegates to runs inside the turn and inherits its context, the wake's receipt
    included, on a provider and model of its own. A window it meets closes no route in the session's name and
    writes nothing on the request."""
    from agent.delegation_context import delegated_child_context
    runner, event, adapter, inbox = intake
    Clock(monkeypatch)
    wake, receipt = _received(runner, event, inbox)
    child = _session_agent(provider='opencode-go', model='deepseek-v4.1-flash')
    token = current_notify_receipt.set(receipt)
    try:
        with delegated_child_context('child-session'):
            assert walls.on_quota_error(child, CodexUsageLimit(resets_in=WEEKLY_RESET)) is True, 'its own turn leaves'
            assert walls._route_owner() is None and not walls.wake_refused_on_quota(child)
        assert not walls.any_closed() and 'quota' not in _board()[1]['coordination']
        # The session's own agent on the same receipt is the coordinating session.
        principal = _session_agent()
        assert walls.on_quota_error(principal, CodexUsageLimit(resets_in=WEEKLY_RESET)) is True
        assert walls.wake_refused_on_quota(principal)
    finally:
        current_notify_receipt.reset(token)
    assert walls.read(*PRINCIPAL)['closed_by']['request_id'] == receipt['request_id']
    assert [wall['model'] for wall in walls.walls()] == [PRINCIPAL[2]]
    assert _board()[1]['coordination']['quota']['route'] == list(PRINCIPAL)


def test_with_several_credentials_the_route_reopens_at_the_soonest_reset_the_pool_knows(intake, monkeypatch):
    """The production profile has more than one Codex account. The refusal that ends the turn is of the last
    credential the pool tried; the route answers again when the first of them is back."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    start = clock.now
    wake, receipt = _received(runner, event, inbox)
    day = 86400
    pool = SimpleNamespace(entries=lambda: [
        SimpleNamespace(last_error_reset_at=start + 5 * day), SimpleNamespace(last_error_reset_at=start + 2 * day),
        SimpleNamespace(last_error_reset_at=None), SimpleNamespace(last_error_reset_at=start - 60)])
    token = current_notify_receipt.set(receipt)
    try:
        assert walls.on_quota_error(_session_agent(_credential_pool=pool), CodexUsageLimit(resets_in=5 * day)) is True
    finally:
        current_notify_receipt.reset(token)
    wall = walls.read(*PRINCIPAL)
    # The reset time the wall keeps is the soonest one; the route's next launch is the probe of the hour.
    assert (wall['reset_at'], walls.open_at(wall)) == (start + 2 * day, start + 3600)
    assert _board()[1]['coordination']['quota']['reset_at'] == start + 2 * day
    # One credential, or a pool that knows nothing sooner: the refusal's own reset time.
    walls.reopen(*PRINCIPAL)
    clock.advance(3600)
    token = current_notify_receipt.set(receipt)
    try:
        walls.on_quota_error(_session_agent(_credential_pool=None), CodexUsageLimit(resets_in=5 * day))
    finally:
        current_notify_receipt.reset(token)
    assert walls.read(*PRINCIPAL)['reset_at'] == clock.now + 5 * day


def test_a_turn_that_failed_for_another_reason_is_no_verdict_on_the_route(intake, monkeypatch):
    """Not quota: a provider error of another kind, or a billing verdict the classifier itself holds unverified
    (the body may be the provider rejecting this very message). No route closes for it and the request is as main
    leaves it. When that turn was the probe of a closed route, the probing goes back at once."""
    runner, event, adapter, inbox = intake
    clock = Clock(monkeypatch)
    wake, receipt = _received(runner, event, inbox)

    def ended(**result):
        asyncio.run(runner._nfos_turn_ended(wake, event.source, dict(result, failed=True, api_calls=1,
                                                                    model=PRINCIPAL[2]), clock.now))

    ended(failure_reason='billing', billing_unverified=True)
    assert not walls.any_closed() and _board()[1]['coordination']['wake_accepted'] is True

    walls.close(*PRINCIPAL, limit='usage_limit_reached', reset_at=None, request_id=receipt['request_id'])
    clock.advance(300)
    walls.take_probe(*PRINCIPAL, task_id=receipt['request_id'])
    assert walls.closed(*PRINCIPAL) is not None, 'a probe in flight holds the route'
    ended(failure_reason='server_error')
    wall = walls.read(*PRINCIPAL)
    assert (wall['probe'], wall['probes']) == (None, 0) and walls.closed(*PRINCIPAL) is None, 'the next entry probes'
    assert _board()[1]['coordination']['wake_accepted'] is True, 'not returned: this front returns a turn dead on quota'


def test_the_urgency_command_of_the_intake_is_recorded_once_for_the_same_request(intake, monkeypatch, tmp_path, capsys):
    """The turn of a request can die on quota after the Principal ran the urgency command for it, and the request
    is then offered again. The command the wake hands over names the request: run again for the same request it
    adds no second comment and no second event to the card. `receive` already was idempotent for it."""
    import sys
    runner, event, adapter, inbox = intake
    Clock(monkeypatch)
    wake, receipt = _received(runner, event, inbox)
    context = wake.metadata['nfos_coordinator_intake']
    assert f"--task CARD_ID --input urgency.json --request {context['request_id']}" in wake.text
    with kb.connect_closing() as conn:
        delivery.receive_request(conn, source=dict(SOURCE, message_id='77'), text='[Japa|1]\nAjuste o relatório.',
                                 project={'profile': 'default', 'delivery_type': 'report'})
        reservation = delivery.reserve_request(conn, capacity=5)
        card = delivery.bootstrap_card(conn, reservation['id'], reservation['claim_token'], pid=os.getpid()).id
    reason = tmp_path / 'urgency.json'
    reason.write_text(json.dumps({'reason': 'O cliente pode cancelar hoje'}), encoding='utf-8')

    def urgent(*request):
        monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'urgent', '--db', context['db_path'], '--task', card,
                                          '--input', str(reason), *request])
        capsys.readouterr()
        delivery.main()
        with kb.connect_closing() as conn:
            comments = conn.execute("SELECT count(*) FROM task_comments WHERE task_id=? AND body LIKE '[urgente]%'",
                                    (card,)).fetchone()[0]
            return (json.loads(capsys.readouterr().out.strip().splitlines()[-1]),
                    len(_events(conn, card, 'priority_escalated')), comments)

    first, events, comments = urgent('--request', context['request_id'])
    assert (first['priority'], events, comments) == (delivery.URGENT_PRIORITY, 1, 1) and 'already_recorded' not in first
    again, events, comments = urgent('--request', context['request_id'])
    assert (again['priority'], again['reason'], events, comments) == (first['priority'], first['reason'], 1, 1)
    assert again['already_recorded'] is True, 'the same request: nothing is added to the card'
    # Another request that makes the same card urgent is another judgment, and the command with no request is as
    # it was.
    assert urgent('--request', 'req_another')[1:] == (2, 2)
    assert urgent()[1:] == (3, 3)


@pytest.mark.asyncio
async def test_the_days_notice_counts_the_requests_and_the_cards_that_wait_and_stops_when_the_route_reopens(
        desk, monkeypatch, ops):
    """WALL_DAILY_NOTICE_20261012. A held request has no card and no row in the wait registry: while its route is
    closed the only word about it is the wall's. One notice on the first day and silence for a week left the
    owner to find out by himself that requests were piling up. Once a day the management topic is told for how
    long the route has been closed and how many inbox entries and cards wait for it. Nothing of it reaches the
    topic of who asked."""
    runner, event, adapter, session = desk
    clock = Clock(monkeypatch)
    start = clock.now
    day = 86400
    session.refusal = lambda: CodexUsageLimit(resets_in=int(start + WEEKLY_RESET - clock.now))

    await runner._nfos_receive(event)
    await _settle(runner)
    await _through(runner, clock, 600, ops)
    await runner._nfos_receive(_second(event))
    await _settle(runner)
    # A card of the profile that pins no route runs on the profile's own, which is the Principal's: the dispatcher
    # holds it there and declares its wait (the hold it asks for before any claim).
    with kb.connect_closing() as conn:
        card = kb.create_task(conn, title='No route pinned', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (card,))
        assert kb._route_wall_hold(conn, card) == f'route_closed:{PRINCIPAL[1]}/{PRINCIPAL[2]}@{PRINCIPAL[0]}'
    (chat, topic, opening), = ops.sent
    assert (chat, topic) == MANAGEMENT_TOPIC and 'sai aqui um aviso por dia' in opening
    announced = start + TICK  # the first round of the gateway after the wall

    # The first day, a round an hour: the oldest request probes the route every hour and nothing more is said.
    await _through(runner, clock, 23 * 3600, ops, step=3600)
    assert clock.now < announced + day and len(ops.sent) == 1
    await _tick(runner, clock, 3600, ops)
    assert clock.now - announced == day + TICK
    (_, (chat, topic, daily)) = ops.sent
    assert (chat, topic) == MANAGEMENT_TOPIC
    assert f'Rota {PRINCIPAL[1]}/{PRINCIPAL[2]} do perfil {PRINCIPAL[0]} segue fechada há 1 d 10 min, ' in daily
    assert 'pelo limite usage_limit_reached.' in daily
    assert 'Cards esperando por ela: 1.' in daily
    assert 'Pedidos na caixa do Principal, ainda sem card: 2.' in daily
    assert time.strftime('%d/%m %H:%M UTC', time.gmtime(start + WEEKLY_RESET)) in daily
    first, second = _requests()
    assert [r['status'] for r in (first, second)] == ['coordinating', 'coordinating'] and len(_cards()) == 1
    assert set(session.offered()) == {first['id']}, 'the oldest request is the one that probes'

    # The same day: nothing more, from this gateway or from one that restarts.
    restarted = Operations()
    await restarted.runner._kanban_route_wall_notices()
    await _through(runner, clock, 23 * 3600, ops, step=3600)
    await restarted.runner._kanban_route_wall_notices()
    assert len(ops.sent) == 2 and restarted.sent == []
    # The second day, once more.
    await _tick(runner, clock, 3600, ops)
    assert len(ops.sent) == 3 and 'segue fechada há 2 d 10 min, ' in ops.sent[2][2]
    assert 'ainda sem card: 2.' in ops.sent[2][2]
    await restarted.runner._kanban_route_wall_notices()
    assert restarted.sent == []

    # In two days nothing was said in the topic of who asked, and no text names an account.
    assert session.said_in_the_topic() == [] and not adapter._send_with_retry.await_count
    assert all((chat, topic) == MANAGEMENT_TOPIC and 'conta ' not in text for chat, topic, text in ops.sent)

    # The limit is lifted: the probe of the next hour is answered, the route reopens, the requests become their
    # cards in the order they arrived. No notice after that, that day or the three after it.
    session.refusal = None
    await _tick(runner, clock, 3600, ops)
    await _tick(runner, clock, 5, ops)
    assert walls.read(*PRINCIPAL) is None and [r['status'] for r in _requests()] == ['attached', 'attached']
    await _through(runner, clock, 3 * day, ops, step=3600)
    await restarted.runner._kanban_route_wall_notices()
    assert len(ops.sent) == 3 and restarted.sent == []
    assert len(session.said_in_the_topic()) == 2 and not session.status_sent
    assert not [text for text in session.said_in_the_topic() if text.startswith('Rota ')]
