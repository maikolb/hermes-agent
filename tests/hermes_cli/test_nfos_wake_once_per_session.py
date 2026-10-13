"""One wake for each decision request in each session it lands in (WAKE_ONCE_PER_SESSION_20261012), and none for a
decision that no longer waits (WAKE_STILL_ASKED_20261012).

What production showed on 2026-10-10, read from the gateway log and the boards: a card born in the portal has two
subscriptions, the portal one and the project topic's, both `notify+wake`. The portal has no chat adapter, so its
wake is re-routed to the Principal route of the project, which is the topic the other subscription already wakes.
The notifier claimed every request once per subscription and woke the same Principal session twice, the second
time after the first turn ended, which is mostly after the answer was on the card: 299 wakes for 114 decisions on
three cards, 120 of them after the recorded answer.

What these tests hold:

* two channels following one card into one session: one turn for each decision request, however many of the card's
  subscriptions lead there, and the wake still says the card is followed by the portal;
* a request answered between being written and the session being free to take it opens no turn;
* two sessions following one card each get their own wake;
* what a person reads in each channel is as it was: the topic gets its notice of every request, the portal gets no
  chat post and its cursor is kept;
* a gateway restart does not wake again for what was delivered;
* a subscription that starts to follow the card later does not take away the wake of an older request;
* a wake is yielded only to a subscription that has the request in hand in the same pass: with the topic's text
  failing (its claim held on older events) the next request of the card still reaches the session, through the
  portal subscription, and so does a request the topic only notified of before it was raised to `notify+wake`;
* a subscription row the notifier cannot read does not end the pass for the cards that come after it;
* a wake that carries a block or a completion besides the request has the text the chat subscription always had;
* the reminder of an open decision (`nudge_open_decisions`) keeps its schedule and its stall, and each charge
  reaches the session once; the one wake still binds the Principal's turn to the card's open decisions
  (`agent/kanban_stop.py`).

The notifier, the board, the decisions and the adapter's admission of a wake (`BasePlatformAdapter.handle_message`:
a session that is running a turn takes no other) are the real ones. A session's turn is reduced to what the
notifier sees of it: its checkpoint, which accepts the wake, and how long it holds the session. The clock is moved
by hand and a worker is a real child process that stays alive.
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent.i18n import t as wake_words
from agent.kanban_stop import build_kanban_stop_nudge
from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway.config import Platform, PlatformConfig
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import build_session_key
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_kanban_notifier import _make_runner
from tests.hermes_cli.nfos_quota_wall import Clock, launcher, quota_board  # noqa: F401

posix_only = pytest.mark.skipif(os.name != 'posix', reason='the dispatcher reaps a worker exit status only on POSIX')

REAL_SLEEP = asyncio.sleep
PASS = 5  # seconds between two passes of the notifier
# The project's topic, which the router binds to the board: the Principal's session of the project lives there.
PROJECT_TOPIC = ('-1000', '8')
OTHER_TOPIC = ('-1000', '9')
# A ticket of the portal, as `suporte` hands it over once approved.
PORTAL = {'platform': 'portal', 'chat_id': 'dov', 'thread_id': 'suporte', 'chat_type': 'portal',
          'user_id': 'portal:dov:lucas'}
NOTICE = 'Principal analisando'
PORTAL_ORIGIN = 'Origem: card do portal'
CARD = re.compile(r't_[0-9a-f]{8}')


class Sessions:
    """The sessions behind the adapter. A wake the adapter admits opens a turn: the turn's checkpoint accepts the
    wake, and the turn holds its session until the test ends it."""

    def __init__(self, root):
        self.root = root
        self.turns = []

    def bind(self, adapter):
        async def handler(event):
            await self.turn(adapter, event)
        adapter._message_handler = handler

    async def turn(self, adapter, event):
        receipt = event.metadata['kanban_wake_delivery']
        key = build_session_key(
            event.source,
            group_sessions_per_user=adapter.config.extra.get('group_sessions_per_user', True),
            thread_sessions_per_user=adapter.config.extra.get('thread_sessions_per_user', False),
            profile=adapter._session_key_profile(event.source))
        assert key in adapter._active_sessions, 'the turn runs in the session the adapter admitted the wake to'
        # The claim behind the wake carried a decision request when the receipt names the card it is owed for.
        asks = bool(receipt.get('principal_task_id'))
        card = receipt.get('principal_task_id') or CARD.search(event.text).group(0)
        with kb.connect_closing() as conn:
            waiting = [row['id'] for row in conn.execute(
                "SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending'", (card,))]
        turn = SimpleNamespace(
            adapter=adapter, key=key, topic=(event.source.chat_id, event.source.thread_id), text=event.text,
            receipt=dict(receipt), card=card, asks=asks, waiting=waiting, opened=time.time(), over=asyncio.Event(),
            entered=asyncio.Event())
        self.turns.append(turn)
        agent = SimpleNamespace(session_id='principal-' + hashlib.sha256(key.encode()).hexdigest()[:12],
                                _session_db=SimpleNamespace(db_path=self.root / 'state.db'))
        await asyncio.to_thread(initialize_agent_turn_checkpoint, agent, turn_id=f'turn-{len(self.turns)}',
                                user_content=event.text, messages=[])
        turn.entered.set()
        await turn.over.wait()

    def open(self):
        return [turn for turn in self.turns if not turn.over.is_set()]

    async def end(self, turn, *, answer=True):
        """The turn is over. With `answer`, the Principal first decided what its card was waiting for."""
        if answer:
            with kb.connect_closing() as conn:
                for row in conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending'",
                                        (turn.card,)).fetchall():
                    delivery.resolve_decision(conn, row['id'], action='continue', answer='Use the first source.',
                                              author='Principal')
        turn.over.set()
        deadline = time.monotonic() + 10
        while turn.key in turn.adapter._active_sessions and time.monotonic() < deadline:
            await REAL_SLEEP(0.005)
        assert turn.key not in turn.adapter._active_sessions, 'the session is free again'


class Topic(BasePlatformAdapter):
    """The gateway's Telegram adapter with its own admission of a wake. Only what it would send is recorded."""

    def __init__(self, sessions):
        super().__init__(PlatformConfig(), Platform.TELEGRAM)
        self.config.typing_indicator = False
        self.said = []
        self.down = None  # the platform's refusal of a text, while it lasts
        sessions.bind(self)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, **kwargs):
        if self.down:
            return SendResult(success=False, error=self.down)
        thread = str((kwargs.get('metadata') or {}).get('thread_id') or '')
        self.said.append((str(chat_id), thread, content))
        return SendResult(success=True, message_id=str(len(self.said)))

    def notices(self, topic=PROJECT_TOPIC):
        return [text for chat, thread, text in self.said if (chat, thread) == topic and NOTICE in text]


class Gateway:
    """The notifier of one gateway process, its adapter, and the router's binding of the board to its topic."""

    def __init__(self, sessions, home, monkeypatch):
        self.sessions, self.home, self.monkeypatch = sessions, home, monkeypatch
        self.start()

    def start(self):
        """A gateway process starts: nothing it held in memory is left."""
        self.adapter = Topic(self.sessions)
        self.runner = _make_runner(self.adapter)
        self.runner._active_profile_name = lambda: 'default'
        router = SimpleNamespace(config=SimpleNamespace(project_router=SimpleNamespace(enabled=True, db_path=None)))
        self.runner._kanban_board_display_targets = (
            lambda profiles, management=False: GatewayKanbanWatchersMixin._kanban_board_display_targets(
                router, profiles, management))

        async def sleep(delay):
            if delay == 5:
                return  # the watcher's delay before its first pass
            self.runner._running = False  # its pause between passes: one pass for each call
            await REAL_SLEEP(0)
        self.monkeypatch.setattr(asyncio, 'sleep', sleep)

    async def notify(self, clock=None, seconds=PASS):
        """One pass of the notifier, then every turn it started has written its checkpoint."""
        if clock is not None:
            clock.advance(seconds)
        self.runner._running = True
        await self.runner._kanban_notifier_watcher(interval=1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await REAL_SLEEP(0)
            running = [turn for turn in self.sessions.open() if turn.adapter is self.adapter]
            if len(running) >= len(self.adapter._active_sessions) and all(turn.entered.is_set() for turn in running):
                return
            await REAL_SLEEP(0.005)
        raise AssertionError('a turn the adapter admitted did not start')

    async def quiet(self, clock, passes=4):
        """Passes with nothing new on the board: what was owed is delivered, and nothing is delivered twice."""
        for _ in range(passes):
            await self.notify(clock)


def _router(home, *bindings):
    """The project router's database of the profile: which topic is the project's, for which board."""
    conn = sqlite3.connect(home / 'project_router.db')
    try:
        conn.executescript(
            "CREATE TABLE projects (profile TEXT NOT NULL, project_id TEXT NOT NULL, slug TEXT NOT NULL, "
            "board_slug TEXT NOT NULL, workdir TEXT, status TEXT NOT NULL, PRIMARY KEY (profile, project_id));"
            "CREATE TABLE topic_bindings (profile TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL, "
            "thread_id TEXT NOT NULL, project_id TEXT NOT NULL, is_management INTEGER NOT NULL DEFAULT 0, "
            "is_closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (profile, platform, chat_id, thread_id));")
        conn.execute("INSERT INTO projects VALUES ('default', 'dov', 'dov', ?, NULL, 'active')", (kb.DEFAULT_BOARD,))
        for chat, thread in bindings:
            conn.execute("INSERT INTO topic_bindings VALUES ('default', 'telegram', ?, ?, 'dov', 0, 0)", (chat, thread))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def board(quota_board, monkeypatch):
    """A profile whose router binds the board to the project's topic, and the sessions behind that chat."""
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda name: quota_board)
    _router(quota_board, PROJECT_TOPIC)
    sessions = Sessions(quota_board)
    return SimpleNamespace(home=quota_board, sessions=sessions, gateway=Gateway(sessions, quota_board, monkeypatch))


def follow(conn, card, topic=PROJECT_TOPIC, mode='notify+wake'):
    """A Telegram topic follows the card, in the shape the production rows have: no user, the topic in its
    metadata."""
    kb.add_notify_sub(conn, task_id=card.id, platform='telegram', chat_id=topic[0], thread_id=topic[1],
                      chat_type='group', notifier_profile='default', delivery_mode=mode,
                      delivery_metadata={'chat_type': 'group', 'thread_id': topic[1]})


def portal_card(conn, launcher, number, *, topic='notify+wake'):
    """A ticket of the portal becomes a card with its worker running. The card is followed by the portal (the
    origin of the request) and, unless `topic` is None, by the project's topic."""
    now = int(time.time())
    request_id = delivery.receive_request(
        conn, source=dict(PORTAL, message_id=f'DV-{number:04d}'),
        text=f'[Lucas (suporte)|portal:dov:lucas] Corrija o item {number} da tela de pedidos',
        project={'profile': 'default', 'delivery_type': 'report'}, origin={'portal': {'chamado': f'DV-{number:04d}'}},
        support_approval={'required': True, 'approved_at': now, 'approved_by': 'Maikol', 'decided_at': now,
                          'decided_by': 'Maikol', 'decision': 'approved'})
    reservation = delivery.reserve_request(conn, capacity=20)
    assert reservation['id'] == request_id
    worker = launcher.start()
    card = delivery.bootstrap_card(conn, request_id, reservation['claim_token'], pid=worker.pid)
    if topic:
        follow(conn, card, mode=topic)
    return kb.get_task(conn, card.id)


def ask(conn, card, number):
    """The card's worker asks the Principal for a decision."""
    return delivery.ask_principal(conn, card.id, card.current_run_id, kind='impediment',
                                  question=f'Which of the two sources satisfies criterion C{number}',
                                  context={'tried': [f'Read the existing evidence of C{number}']})


def subscriptions(conn, card):
    """Each subscription of the card with what the notifier still owes it."""
    out = {}
    for sub in kb.list_notify_subs(conn, card.id):
        identity = {key: sub[key] for key in ('task_id', 'platform', 'chat_id', 'thread_id')}
        _, owed = kb.unseen_events_for_sub(conn, **identity, kinds=('nfos_principal_requested',))
        out[(sub['platform'], sub['chat_id'], sub['thread_id'])] = SimpleNamespace(
            mode=sub['delivery_mode'], owed=[event.id for event in owed], claim=kb.get_notify_claim(conn, **identity))
    return out


def requests(conn, card=None):
    """The decision requests on the board, in the order they were written."""
    rows = conn.execute("SELECT task_id, payload FROM task_events WHERE kind='nfos_principal_requested' ORDER BY id")
    return [json.loads(row['payload'])['decision_id'] for row in rows if card is None or row['task_id'] == card.id]


def settled(conn, card):
    """Nothing of the card's requests is owed to any of its subscriptions, and no delivery is left half done."""
    return all(not sub.owed and sub.claim is None for sub in subscriptions(conn, card).values())


async def decisions_in_sequence(gateway, clock, conn, plan, *, turn=60, work=30, limit=40000):
    """The shape of 2026-10-10: each card's worker asks a decision, waits for the answer, works for `work` seconds
    and asks the next one; one Principal session answers, each of its turns taking `turn` seconds and deciding what
    its own card was waiting for. `plan` is a list of (card, how many decisions it asks). Returns what was counted.
    """
    sessions = gateway.sessions
    numbered = conn.execute('SELECT count(*) FROM nfos_decisions').fetchone()[0]
    state = [SimpleNamespace(card=card, left=count, asked=0, next_at=clock.now) for card, count in plan]
    first_turn, started, said = len(sessions.turns), clock.now, len(gateway.adapter.notices())
    for _ in range(limit):
        for item in state:
            waiting = conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND status='pending'",
                                   (item.card.id,)).fetchone()
            if waiting:
                item.next_at = None
            elif item.next_at is None:
                item.next_at = clock.now + work  # just answered: the worker goes on
            if item.left and not waiting and clock.now >= item.next_at:
                item.asked += 1
                item.left -= 1
                ask(conn, item.card, numbered + item.asked)
                item.next_at = None
        at_rest = (not sessions.open()
                   and not conn.execute("SELECT 1 FROM nfos_decisions WHERE status='pending'").fetchone()
                   and all(settled(conn, item.card) for item in state))
        if at_rest and not any(item.left for item in state):
            break
        if at_rest:
            # Nothing is owed and nobody is in a turn: the passes until the next question find nothing to do.
            clock.now = max(clock.now, min(item.next_at for item in state if item.left) - PASS)
        await gateway.notify(clock)
        for running in sessions.open():
            if clock.now - running.opened >= turn:
                await sessions.end(running)
    else:
        raise AssertionError('the scenario did not come to rest')
    turns = sessions.turns[first_turn:]
    return {
        'cards': len(state),
        'decisions': sum(item.asked for item in state),
        'request_events': len(requests(conn)),
        'turns_opened': len(turns),
        'turns_with_nothing_waiting': len([t for t in turns if not t.waiting]),
        'turns_by_card': {item.card.id: len([t for t in turns if t.card == item.card.id]) for item in state},
        'sessions': len({t.key for t in turns}),
        'notices_in_the_topic': len(gateway.adapter.notices()) - said,
        'decisions_left_pending': conn.execute("SELECT count(*) FROM nfos_decisions WHERE status='pending'").fetchone()[0],
        'seconds': int(clock.now - started),
    }


@pytest.mark.asyncio
async def test_two_channels_of_one_card_wake_its_session_once_for_each_decision(board, launcher, monkeypatch):
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 31)
        assert sorted((key[0], sub.mode) for key, sub in subscriptions(conn, card).items()) == [
            ('portal', 'notify+wake'), ('telegram', 'notify+wake')], 'the two subscriptions of a portal card'

        first = ask(conn, card, 1)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1, 'one turn for the request, not one for each subscription'
        (turn,) = sessions.turns
        assert turn.topic == PROJECT_TOPIC and turn.waiting == [first] and card.id in turn.text
        assert PORTAL_ORIGIN in turn.text, 'the one wake still says the card is followed by the portal'
        assert len(gateway.adapter.notices()) == 1 and first not in requests(conn)[1:]

        await sessions.end(turn)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1, 'the session is free and the answer is on the card: nothing wakes it'
        assert delivery.get_decision(conn, first)['status'] == 'resolved' and settled(conn, card)

        second = ask(conn, card, 2)
        await gateway.quiet(clock)
        assert [t.waiting for t in sessions.turns] == [[first], [second]], 'the next request wakes it once more'
        await sessions.end(sessions.turns[-1])
        await gateway.quiet(clock)
        assert len(sessions.turns) == 2 and settled(conn, card)
        assert requests(conn, card) == [first, second]
        # What a person reads is as it was: the topic has its notice of each request and the portal has no chat post.
        assert len(gateway.adapter.notices()) == 2
        assert {(chat, thread) for chat, thread, _ in gateway.adapter.said} == {PROJECT_TOPIC}


@pytest.mark.asyncio
@pytest.mark.parametrize('followers', ['portal and topic', 'portal only', 'topic only'])
async def test_a_decision_answered_before_the_session_is_free_wakes_nobody(board, launcher, monkeypatch, followers):
    """The pending state is read when the wake is delivered, not when it was queued."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        busy = portal_card(conn, launcher, 40, topic=None)
        ask(conn, busy, 1)
        await gateway.quiet(clock)
        (holding,) = sessions.turns  # the session is in the turn of another card

        if followers == 'topic only':
            card = portal_card(conn, launcher, 41, topic=None)
            portal = next(iter(subscriptions(conn, card)))
            kb.remove_notify_sub(conn, task_id=card.id, platform=portal[0], chat_id=portal[1], thread_id=portal[2])
            follow(conn, card)
        else:
            card = portal_card(conn, launcher, 41, topic='notify+wake' if followers == 'portal and topic' else None)
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1, 'a busy session takes no wake'
        assert not settled(conn, card), 'the wake stays owed while the session is busy'

        # The answer is recorded while the wake still waits for the session.
        delivery.resolve_decision(conn, decision, action='continue', answer='Use the first source.', author='Principal')
        await sessions.end(holding)
        await gateway.quiet(clock, passes=6)
        assert [t.card for t in sessions.turns] == [busy.id], 'no turn opens for a decision that has its answer'
        assert settled(conn, card), 'and the request is not owed to any subscription any more'
        notices = [text for text in gateway.adapter.notices() if card.id in text]
        assert len(notices) == (0 if followers == 'portal only' else 1), 'the topic still got its notice of the request'


@pytest.mark.asyncio
async def test_two_sessions_following_one_card_each_get_their_own_wake(board, launcher, monkeypatch):
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 50)
        follow(conn, card, OTHER_TOPIC)  # another topic, which is another session, follows the same card
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        assert sorted(t.topic for t in sessions.turns) == sorted([PROJECT_TOPIC, OTHER_TOPIC])
        assert len({t.key for t in sessions.turns}) == 2 and all(t.waiting == [decision] for t in sessions.turns)
        mine = {t.topic: PORTAL_ORIGIN in t.text for t in sessions.turns}
        assert mine == {PROJECT_TOPIC: True, OTHER_TOPIC: False}, 'the portal is counted with its own session only'
        assert len(gateway.adapter.notices(PROJECT_TOPIC)) == 1 and len(gateway.adapter.notices(OTHER_TOPIC)) == 1
        for turn in sessions.open():
            await sessions.end(turn)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 2 and settled(conn, card)


@pytest.mark.asyncio
async def test_a_topic_that_only_notifies_keeps_its_notice_and_the_portal_wakes_the_principal(
        board, launcher, monkeypatch):
    """22 of the 26 two-subscription cards read on the production board on 2026-10-12 have the topic on plain
    `notify`: there the portal subscription is the only one that wakes. Holds on main as well."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 60, topic='notify')
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        (turn,) = sessions.turns
        assert turn.topic == PROJECT_TOPIC and turn.waiting == [decision] and PORTAL_ORIGIN in turn.text
        assert len(gateway.adapter.notices()) == 1
        await sessions.end(turn)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1 and settled(conn, card)


@pytest.mark.asyncio
async def test_a_gateway_restart_does_not_wake_again_for_what_was_delivered(board, launcher, monkeypatch):
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 70)
        decision = ask(conn, card, 1)
        # The process dies after the session accepted the wake and before any cursor was moved.
        gateway.runner._kanban_advance = lambda *args: False
        await gateway.quiet(clock)
        (turn,) = sessions.turns
        assert turn.waiting == [decision] and not settled(conn, card)
        accepted = [sub.claim['wake_accepted'] for sub in subscriptions(conn, card).values() if sub.claim]
        assert 1 in accepted, 'the acceptance is on the board, the cursors are not'
        await sessions.end(turn, answer=False)  # the turn died with the process; the decision is still pending

        gateway.start()
        clock.advance(61)  # the dead process's claims expire
        await gateway.quiet(clock, passes=6)
        assert len(sessions.turns) == 1, 'the new process finds the acceptance and opens no second turn'
        assert settled(conn, card) and delivery.get_decision(conn, decision)['status'] == 'pending'
        assert len(gateway.adapter.notices()) <= 1

        # And a restart with everything delivered and answered wakes nobody.
        delivery.resolve_decision(conn, decision, action='continue', answer='Use the first source.', author='Principal')
        gateway.start()
        clock.advance(61)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1 and gateway.adapter.said == []


@pytest.mark.asyncio
async def test_a_subscription_that_comes_later_does_not_take_away_the_wake_of_an_older_request(
        board, launcher, monkeypatch):
    """The topic's subscription starts after the request: its cursor is already past it, so it never has the
    request in hand. The portal subscription, which has, still owes that wake."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        busy = portal_card(conn, launcher, 80, topic=None)
        ask(conn, busy, 1)
        await gateway.quiet(clock)
        (holding,) = sessions.turns

        card = portal_card(conn, launcher, 81, topic=None)
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        assert len(sessions.turns) == 1 and not settled(conn, card)
        clock.advance(30)
        follow(conn, card)
        await gateway.quiet(clock)
        await sessions.end(holding)
        await gateway.quiet(clock, passes=6)
        mine = [t for t in sessions.turns if t.card == card.id]
        assert len(mine) == 1 and mine[0].waiting == [decision], 'the older request still reached the session, once'

        # From here on the topic's subscription was there when the request was made: still one turn for each.
        await sessions.end(mine[0])
        later = ask(conn, card, 2)
        await gateway.quiet(clock)
        mine = [t for t in sessions.turns if t.card == card.id]
        assert [t.waiting for t in mine] == [[decision], [later]]
        await sessions.end(mine[-1])
        await gateway.quiet(clock)
        assert len([t for t in sessions.turns if t.card == card.id]) == 2, 'and the free session is not woken again'
        assert len([text for text in gateway.adapter.notices() if card.id in text]) == 1, 'the notice of the later one'


@pytest.mark.asyncio
async def test_a_topic_held_behind_its_failing_text_does_not_keep_the_next_request_from_the_session(
        board, launcher, monkeypatch):
    """A delivery whose text failed is never dropped: the topic's claim stays on the events it could not post and
    does not reach what the card asks next. The portal subscription yields its wake only to a subscription that
    has the request in hand, so the next request still wakes the session, once."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    gateway.adapter.down = 'Bad Request: message thread not found'
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 33)
        topic = ('telegram', *PROJECT_TOPIC)
        first = ask(conn, card, 1)
        await gateway.quiet(clock)
        (turn,) = sessions.turns
        assert turn.waiting == [first], 'the text failing does not keep the wake back, and it is still one wake'
        await sessions.end(turn)
        await gateway.quiet(clock)
        held = subscriptions(conn, card)[topic]
        assert len(sessions.turns) == 1 and held.claim['wake_accepted'] == 1 and not held.claim['notified']

        second = ask(conn, card, 2)
        await gateway.quiet(clock, passes=8)
        assert [t.waiting for t in sessions.turns] == [[first], [second]], 'the next request reaches the session'
        assert sessions.turns[-1].topic == PROJECT_TOPIC and PORTAL_ORIGIN in sessions.turns[-1].text
        held = subscriptions(conn, card)[topic]
        assert held.claim['cursor'] < held.owed[-1], 'through the portal: the topic is still on the first request'

        # The text comes back while that turn runs: the topic posts what it owed and reaches the second request.
        gateway.adapter.down = None
        await gateway.quiet(clock)
        assert len(sessions.turns) == 2, 'the session is in the turn of that request: nothing else opens'
        await sessions.end(sessions.turns[-1])
        await gateway.quiet(clock, passes=6)
        assert len(sessions.turns) == 2, 'and the answer is on the card when the topic gets there'
        assert settled(conn, card) and len(gateway.adapter.notices()) == 2


@pytest.mark.asyncio
async def test_a_topic_raised_to_wake_after_it_notified_does_not_take_the_wake_the_portal_owes(
        board, launcher, monkeypatch):
    """The topic only notifies when the request is written (22 of the 26 cards read on 2026-10-12): it posts its
    notice and moves past the request. Raised to `notify+wake` before the busy session takes the portal's wake,
    it is ahead of the portal subscription but does not have the request in hand."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        busy = portal_card(conn, launcher, 82, topic=None)
        ask(conn, busy, 1)
        await gateway.quiet(clock)
        (holding,) = sessions.turns

        card = portal_card(conn, launcher, 83, topic='notify')
        clock.advance(30)
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        followers = subscriptions(conn, card)
        assert not followers[('telegram', *PROJECT_TOPIC)].owed, 'the topic posted its notice and moved on'
        assert len(sessions.turns) == 1 and not settled(conn, card), 'the portal still owes the wake'

        follow(conn, card, mode='notify+wake')
        await gateway.quiet(clock)
        await sessions.end(holding)
        await gateway.quiet(clock, passes=6)
        mine = [t for t in sessions.turns if t.card == card.id]
        assert len(mine) == 1 and mine[0].waiting == [decision], 'the request reached the session, once'
        await sessions.end(mine[0])
        await gateway.quiet(clock)
        assert len([t for t in sessions.turns if t.card == card.id]) == 1 and settled(conn, card)


@pytest.mark.asyncio
async def test_a_subscription_row_that_cannot_be_read_does_not_end_the_pass_for_the_other_cards(
        board, launcher, monkeypatch):
    """Working out who owes a wake reads the card's other subscriptions. A row it cannot read makes the delivery
    wake as it always did; it does not stop the deliveries that come after it in the same pass."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        broken = portal_card(conn, launcher, 84)
        conn.execute("UPDATE kanban_notify_subs SET created_at='not a time' WHERE task_id=? AND platform='telegram'",
                     (broken.id,))
        conn.commit()
        other = portal_card(conn, launcher, 85)
        asked = ask(conn, broken, 1)
        clock.advance(30)  # the broken card's request is the older one: it is offered first in every pass
        later = ask(conn, other, 1)
        await gateway.quiet(clock)
        (turn,) = sessions.turns
        assert turn.card == broken.id and turn.waiting == [asked], 'its portal subscription wakes on its own'
        await sessions.end(turn)
        await gateway.quiet(clock, passes=6)
        assert [(t.card, t.waiting) for t in sessions.turns] == [(broken.id, [asked]), (other.id, [later])]
        await sessions.end(sessions.turns[-1])
        await gateway.quiet(clock)
        assert len(sessions.turns) == 2 and settled(conn, other)


@pytest.mark.asyncio
async def test_a_wake_that_also_carries_a_block_keeps_the_text_the_chat_subscription_always_had(
        board, launcher, monkeypatch):
    """The line about the portal stands for the portal's wake of the request, which is no longer sent. A wake of
    the chat subscription that also reports a block or a completion never had that line, and still has not. The
    block is written straight on the board: what is held here is what the notifier does with a claim of both."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        busy = portal_card(conn, launcher, 86, topic=None)
        ask(conn, busy, 1)
        await gateway.quiet(clock)
        (holding,) = sessions.turns

        card = portal_card(conn, launcher, 87)
        decision = ask(conn, card, 1)
        with kb.write_txn(conn):
            kb._append_event(conn, card.id, 'blocked', {'reason': 'Waiting for the Principal.'})
        await gateway.quiet(clock)
        await sessions.end(holding)
        await gateway.quiet(clock, passes=6)
        (turn,) = [t for t in sessions.turns if t.card == card.id]
        assert turn.waiting == [decision] and 'worker aguardando sua decisão' in turn.text
        assert wake_words('gateway.kanban.wake.blocked') in turn.text, 'the one wake carries the request and the block'
        assert PORTAL_ORIGIN not in turn.text
        await sessions.end(turn)
        await gateway.quiet(clock)
        assert len([t for t in sessions.turns if t.card == card.id]) == 1 and settled(conn, card)


@posix_only
@pytest.mark.asyncio
async def test_the_reminder_of_an_open_decision_keeps_its_schedule_and_reaches_the_session_once(
        board, launcher, monkeypatch):
    """`nudge_open_decisions` is as it was: the same charges at the same times, then the stall. Each charge is a
    new request event and opens one turn, where it opened one for each subscription."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 90)
        workspace = kb.resolve_workspace(card)
        kb.set_workspace_path(conn, card.id, str(workspace))
        start = clock.now
        decision = ask(conn, card, 1)
        launcher.exit(card.worker_pid, 0)
        assert (card.id, 'open_decision') in kb.dispatch_once(conn).claim_held
        await gateway.quiet(clock, passes=2)
        while clock.now - start < 3300:  # the dispatcher runs every 300 s, the notifier twice after it
            for turn in sessions.open():
                await sessions.end(turn, answer=False)  # the Principal's turn ends having decided nothing
            clock.advance(300 - 2 * PASS)
            kb.dispatch_once(conn)
            await gateway.quiet(clock, passes=2)

        def at(kind):
            return [int(row['created_at'] - start) for row in conn.execute(
                'SELECT created_at FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (card.id, kind))]

        assert at('nfos_principal_requested') == [0, 600, 1500, 2400] and at('nfos_decision_stalled') == [3300]
        assert set(requests(conn, card)) == {decision}, 'one decision, charged four times'
        assert [t.asks for t in sessions.turns] == [True] * 4 + [False], 'a turn for each charge, then the stall'
        assert all(t.waiting == [decision] and t.card == card.id for t in sessions.turns)
        after = kb.get_task(conn, card.id)
        assert (after.status, after.block_kind) == ('blocked', 'awaiting_principal')
        assert delivery.get_decision(conn, decision)['status'] == 'pending'
        await sessions.end(sessions.turns[-1], answer=False)


@pytest.mark.asyncio
async def test_the_one_wake_still_binds_the_turn_to_the_cards_open_decisions(board, launcher, monkeypatch):
    """`agent/kanban_stop.py` reads the receipt of the wake: the card it is for and the board it is on."""
    gateway, sessions, clock = board.gateway, board.sessions, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 95)
        decision = ask(conn, card, 1)
        await gateway.quiet(clock)
        (turn,) = sessions.turns
        assert turn.receipt['principal_task_id'] == card.id
        nudge = build_kanban_stop_nudge(messages=[], principal_receipt=turn.receipt)
        assert nudge and decision in nudge and card.id in nudge
        await sessions.end(turn)
        assert build_kanban_stop_nudge(messages=[], principal_receipt=turn.receipt) is None


@pytest.mark.asyncio
async def test_three_cards_asking_in_sequence_open_one_turn_for_each_decision(board, launcher, monkeypatch):
    """The scenario of the measurement, small: three portal cards of one project, each followed by the portal and
    by the topic, asking decision after decision of the one Principal session."""
    gateway, clock = board.gateway, Clock(monkeypatch)
    with kb.connect_closing() as conn:
        plan = [(portal_card(conn, launcher, number), count) for number, count in ((1, 4), (2, 3), (3, 3))]
            # Questions twenty minutes apart, as on that day, and then one right after the other.
        spaced = await decisions_in_sequence(gateway, clock, conn, plan, work=1200)
        dense = await decisions_in_sequence(gateway, clock, conn, plan, work=30)
    for counted in (spaced, dense):
        assert counted['decisions'] == 10 and counted['decisions_left_pending'] == 0
        assert counted['turns_opened'] == 10, 'one turn of the Principal for each decision'
        assert counted['turns_with_nothing_waiting'] == 0, 'and none opened after the answer was recorded'
        assert counted['sessions'] == 1 and counted['notices_in_the_topic'] == 10
    assert dense['request_events'] == 20, 'the two rounds, on the same three cards'


def test_a_request_is_woken_for_while_its_decision_waits_for_what_the_event_asked(quota_board, launcher):
    """Read from the decision's own state, never from the text of the request."""
    from gateway.kanban_watchers import _requests_still_asked
    with kb.connect_closing() as conn:
        card = portal_card(conn, launcher, 99, topic=None)
        pending, answered, with_person, back = (ask(conn, card, number) for number in (1, 2, 3, 4))
        delivery.resolve_decision(conn, answered, action='continue', answer='Use the first source.', author='Principal')
        conn.execute("UPDATE nfos_decisions SET status='human' WHERE id IN (?,?)", (with_person, back))
        conn.execute("UPDATE nfos_decisions SET status='pending' WHERE id=?", (back,))
        conn.commit()

    def event(number, **payload):
        return SimpleNamespace(id=number, kind='nfos_principal_requested', payload=payload)

    events = [
        event(1, decision_id=pending),
        event(2, decision_id=answered),
        event(3, decision_id=with_person),  # the Principal already answered it with a question to a person
        event(4, decision_id=with_person, awaiting='human_reply'),  # the reminder that the person has not answered
        event(5, decision_id=back, awaiting='human_reply'),  # the person answered: the decision is the Principal's again
        event(6, decision_id='dec_not_on_this_board'),
        event(7),
        event(8, decision_id=back),
    ]
    assert [ev.id for ev in _requests_still_asked(kb.DEFAULT_BOARD, card.id, events)] == [1, 4, 6, 7, 8]
