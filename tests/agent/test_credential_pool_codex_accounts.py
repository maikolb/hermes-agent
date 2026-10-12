"""Several ChatGPT accounts in one openai-codex pool: when one runs out of quota the next one takes the turn.

Read on a production profile on 2026-10-12 (read-only): five pool entries held one account's token pair, because
each login of another account had been overwritten by the profile's singleton login at its first refresh; on
2026-10-10 one session "rotated" to the account it had just marked exhausted 187 times in 122 s; and the reset time
of the weekly window was forgotten whenever the auxiliary path wrote the mark, so the exhausted account came back
every hour.

What these tests hold, on a real pool over a real ``auth.json`` (only the HTTP transport is a stand-in, and every
token is a fake written here):

* a ``manual:device_code`` entry of another account keeps its own token pair beside the singleton, on the proactive
  refresh and on the forced refresh after a 401; an entry of the singleton's own account still follows it, and one
  whose account cannot be told adopts nothing (ported from NousResearch/hermes-agent 46503f1672, 20512a47a6);
* the rotation that follows a refusal never hands back the refused account, whatever the usage probe says: a
  verdict it kept from before the refusal, or purchased credits (the no-recovery contract of 8530d81b55, by
  exclusion); with three accounts the rotation goes A, B, C and then reports that nothing is left;
* a refusal stands for one interval of the usage probe (five minutes) and no longer: past it an early reopening is
  seen by a pool that has just been loaded and by one an agent has held for hours without selecting again (a cached
  gateway session, a long card worker), and with credits authorized a new process starts on the next account;
* a refusal met on the auxiliary path leaves the same reason and reset time on the entry as one met by the main
  loop, and nothing else of that path changes: a 401 is marked as before, an answer that cannot be read too.
"""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

import hermes_cli.auth as auth_mod
from agent import auxiliary_client
from agent import credential_pool as cp
from tests.hermes_cli.nfos_codex_pool import DAY, Provider, account_of, entry, jwt, read_pool, write_auth

RESET_IN = 5 * DAY
REFUSAL = {'reason': 'usage_limit_reached', 'message': 'The usage limit has been reached'}
PROBE_INTERVAL = auth_mod.CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS


class Clocks:
    """The two clocks of the process (the pool's marks are on one, the probe's kept verdicts on the other), moved
    together and only forward by the test."""

    def __init__(self, monkeypatch):
        self.ahead = 0.0
        wall, steady = time.time, time.monotonic
        monkeypatch.setattr(time, 'time', lambda: wall() + self.ahead)
        monkeypatch.setattr(time, 'monotonic', lambda: steady() + self.ahead)

    def go(self, seconds):
        self.ahead += seconds


@pytest.fixture
def clocks(monkeypatch):
    return Clocks(monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    """Every account out of quota for the usage endpoint, unless a test says otherwise."""
    return Provider(monkeypatch, {name: time.time() + RESET_IN for name in 'ABC'})


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-codex-cli'))

    def write(entries, singleton=None, use_credits=False, **more):
        write_auth(home, entries, singleton=singleton, **more)
        (home / 'config.yaml').write_text(yaml.safe_dump({'codex': {'use_credits': use_credits}}), encoding='utf-8')
        return cp.load_pool('openai-codex')

    write.disk = lambda: read_pool(home)
    return write


def three_accounts(**first):
    a, b, c = jwt('A'), jwt('B'), jwt('C')
    return (a, b, c), [entry('e-A', 'manual:device_code', 0, a, 'rA', **first),
                       entry('e-B', 'manual:device_code', 1, b, 'rB'),
                       entry('e-C', 'manual:device_code', 2, c, 'rC')]


def refuse(pool, ident, key, reset_in=RESET_IN):
    """The provider refused the call ``key`` made: what the conversation loop tells the pool."""
    nxt = pool.mark_exhausted_and_rotate(status_code=429, error_context=dict(REFUSAL, reset_at=time.time() + reset_in),
                                         credential_id=ident, api_key_hint=key)
    return nxt.id if nxt else None


def new_process():
    """A process that starts now: its own pool over the same ``auth.json``, and no usage verdict kept."""
    auth_mod._codex_quota_probe_cache.clear()
    return cp.load_pool('openai-codex')


# ── One login per account stays one login per account ───────────────────

def test_a_login_of_another_account_is_refreshed_with_its_own_pair_beside_the_singleton(store, provider):
    """The singleton is account A's login. Account B's login is about to expire: it is refreshed with B's own
    refresh token and stays B's. Before the fix it took A's pair with no request at all, in memory and on disk."""
    a, b_expiring, c = jwt('A'), jwt('B', expires_in=30), jwt('C')
    pool = store([entry('e-A', 'device_code', 0, a, 'rA'), entry('e-B', 'manual:device_code', 1, b_expiring, 'rB'),
                  entry('e-C', 'manual:device_code', 2, c, 'rC')], singleton=(a, 'rA'))
    assert pool.select().id == 'e-A'
    assert {e.id: account_of(e.access_token) for e in pool.entries()} == {'e-A': 'A', 'e-B': 'B', 'e-C': 'C'}
    assert provider.refresh_posted == ['rB']
    on_disk = store.disk()
    assert account_of(on_disk['e-B']['access_token']) == 'B' and on_disk['e-B']['refresh_token'] == 'rB+'
    assert on_disk['e-A']['refresh_token'] == 'rA', "the singleton's pair was not spent"


def test_the_forced_refresh_after_a_401_keeps_the_login_of_another_account(store, provider):
    a, b, c = jwt('A'), jwt('B'), jwt('C')
    pool = store([entry('e-A', 'device_code', 0, a, 'rA'), entry('e-B', 'manual:device_code', 1, b, 'rB'),
                  entry('e-C', 'manual:device_code', 2, c, 'rC')], singleton=(a, 'rA'))
    refreshed = pool.try_refresh_matching(api_key_hint=b, credential_id='e-B')
    assert refreshed is not None and account_of(refreshed.access_token) == 'B'
    assert provider.refresh_posted == ['rB']
    assert account_of(store.disk()['e-B']['access_token']) == 'B'


def test_a_login_of_the_singletons_own_account_still_follows_it(store, provider):
    """A new login of the same account, written to the singleton after a 429, frees the entries that alias it:
    the ``device_code`` entry and a ``manual:device_code`` entry of that account."""
    old, new = jwt('A'), jwt('A', expires_in=9 * DAY)
    frozen = {'last_status': 'exhausted', 'last_status_at': time.time() - 60, 'last_error_code': 401,
              'last_error_reason': 'token_expired'}
    pool = store([entry('e-A', 'device_code', 0, old, 'rA-old', **frozen),
                  entry('e-alias', 'manual:device_code', 1, old, 'rA-old')], singleton=(new, 'rA-new'))
    assert pool.select().id == 'e-A'
    seeded = next(e for e in pool.entries() if e.id == 'e-A')
    assert (seeded.access_token, seeded.refresh_token) == (new, 'rA-new') and seeded.last_status != 'exhausted'
    alias = next(e for e in pool.entries() if e.id == 'e-alias')
    synced = pool._sync_codex_entry_from_auth_store(alias)
    assert (synced.access_token, synced.refresh_token) == (new, 'rA-new')
    assert provider.refresh_posted == []


def test_a_login_whose_account_cannot_be_told_adopts_nothing(store, provider):
    a, opaque = jwt('A'), jwt('B', identity=False)
    pool = store([entry('e-A', 'device_code', 0, a, 'rA'), entry('e-B', 'manual:device_code', 1, opaque, 'rB')],
                 singleton=(a, 'rA'))
    kept = next(e for e in pool.entries() if e.id == 'e-B')
    assert pool._sync_codex_entry_from_auth_store(kept) is kept
    # And the other way round: the entry is known, the singleton's token carries no identity.
    pool = store([entry('e-B', 'manual:device_code', 0, jwt('B'), 'rB')], singleton=(jwt('B', identity=False), 'rX'))
    kept = next(e for e in pool.entries() if e.id == 'e-B')
    assert pool._sync_codex_entry_from_auth_store(kept) is kept


def test_a_singleton_older_than_the_entrys_own_rotation_is_not_adopted_again(store, provider):
    """The entry rotated its pair after the singleton was last written (a manual entry never writes back): taking
    the singleton again would post a refresh token that is already spent."""
    stale, fresh = jwt('A', expires_in=9 * DAY), jwt('A')
    pool = store([entry('e-A', 'manual:device_code', 0, fresh, 'rA-new', last_refresh='2026-10-09T10:00:00Z')],
                 singleton=(stale, 'rA-old'), singleton_refreshed='2026-10-08T21:35:20Z')
    kept = next(e for e in pool.entries() if e.id == 'e-A')
    assert pool._sync_codex_entry_from_auth_store(kept) is kept
    newer = replace(kept, last_refresh='2026-10-08T00:00:00Z')
    assert pool._sync_codex_entry_from_auth_store(newer).refresh_token == 'rA-old', 'a newer singleton is followed'


# ── The rotation leaves the account the provider refused ────────────────

def test_the_rotation_leaves_an_account_the_usage_probe_had_seen_restored(store, provider, clocks):
    """Account A was in its cooldown, the usage endpoint showed its window open again and the pool went back to
    it. Then the provider refuses A. Before the fix the verdict kept from before the refusal lifted the mark the
    refusal had just written, and the "rotation" handed A back on every 429 for up to five minutes."""
    (a, b, c), entries = three_accounts(last_status='exhausted', last_status_at=time.time() - 3600,
                                        last_error_code=429, last_error_reason='usage_limit_reached',
                                        last_error_reset_at=time.time() + RESET_IN)
    pool = store(entries)
    provider.used['A'] = 99.0
    assert pool.select().id == 'e-A' and provider.usage_asked == ['A']
    provider.used['A'] = 100.0

    assert [refuse(pool, 'e-A', a) for _ in range(3)] == ['e-B', 'e-B', 'e-B'], 'A again and again is still B'
    assert store.disk()['e-A']['last_status'] == 'exhausted'
    assert refuse(pool, 'e-B', b) == 'e-C'
    assert refuse(pool, 'e-C', c) is None, 'every account refused: nothing is left to rotate to'
    assert not pool.has_available()
    assert abs(pool.next_available_at() - (time.time() + RESET_IN)) < 5
    assert pool.select() is None
    assert provider.usage_asked == ['A'], 'the three refusals stand: nothing was asked of the usage endpoint'

    # Past the probe's interval a selection asks again, once per refused account, and keeps the answer.
    clocks.go(PROBE_INTERVAL + 1)
    assert pool.select() is None
    assert sorted(provider.usage_asked[1:]) == ['A', 'B', 'C']
    assert pool.select() is None and len(provider.usage_asked) == 4, 'that answer is kept for its five minutes'


def test_an_early_reopening_is_seen_once_the_refusal_is_older_than_the_probes_interval(store, provider, clocks):
    (a, b, c), entries = three_accounts()
    pool = store(entries)
    assert pool.select().id == 'e-A'
    assert refuse(pool, 'e-A', a) == 'e-B'
    provider.used['A'] = 12.0  # the owner redeemed a reset of A's window
    assert pool.select().id == 'e-B' and provider.usage_asked == [], 'the refusal stands for the interval'
    clocks.go(PROBE_INTERVAL + 1)
    assert pool.select().id == 'e-A' and provider.usage_asked == ['A']
    assert next(e for e in pool.entries() if e.id == 'e-A').last_status == 'ok'


def test_an_agent_that_never_selects_again_goes_back_to_an_account_that_reopened(store, provider, clocks):
    """One pool object for the life of an agent: `select()` ran once, when the agent was built (a cached gateway
    session, a card worker that runs for hours). The first account refuses and the second serves. The owner
    redeems a reset of the first; an hour later the second runs out. The rotation asks the usage endpoint about
    the first and goes back to it, as a process that starts at that moment does."""
    (a, b, c), entries = three_accounts()
    pool = store(entries)
    assert pool.select().id == 'e-A'
    assert refuse(pool, 'e-A', a) == 'e-B'
    provider.used['A'] = 12.0
    del provider.closed_until['A']
    clocks.go(3600)
    assert refuse(pool, 'e-B', b) == 'e-A'
    assert provider.usage_asked == ['A']
    assert pool.has_available() and pool.next_available_at() is None
    assert new_process().select().id == 'e-A'


def test_after_the_three_refused_the_same_agent_goes_back_to_the_one_that_reopened(store, provider, clocks):
    """The three refused in one lap (the route closed). The first account reopens early. The same agent, still
    holding the third account's key, makes the next call (the route's probe): refused, and the rotation goes to
    the first instead of confirming a wall while an account is open."""
    (a, b, c), entries = three_accounts()
    pool = store(entries)
    pool.select()
    assert [refuse(pool, 'e-A', a), refuse(pool, 'e-B', b), refuse(pool, 'e-C', c)] == ['e-B', 'e-C', None]
    provider.used['A'] = 12.0
    del provider.closed_until['A']
    assert refuse(pool, 'e-C', c) is None and provider.usage_asked == [], 'inside the interval the refusals stand'
    clocks.go(3600)
    assert refuse(pool, 'e-C', c) == 'e-A'
    assert sorted(provider.usage_asked) == ['A', 'B'], 'the two that had not just refused were asked about'


def test_a_refusal_younger_than_the_probes_interval_stands(provider, clocks):
    token = jwt('A')
    provider.used['A'] = 50.0
    assert auth_mod._probe_codex_quota_restored(token) is True
    provider.used['A'] = 100.0
    assert auth_mod._probe_codex_quota_restored(token) is True, 'inside the throttle the kept verdict is served'
    # The provider refuses the account now: the verdict kept from before the refusal is no answer about it.
    refused_at = time.time()
    assert auth_mod._probe_codex_quota_restored(token, not_before=refused_at) is None
    assert auth_mod._probe_codex_quota_restored(token, not_before=str(refused_at)) is None
    assert len(provider.usage_asked) == 1, 'and the endpoint is not asked either'
    # A mark the clock has not reached (the host's time was set back) and one that cannot be read hold nothing.
    assert auth_mod._probe_codex_quota_restored(token, not_before=refused_at + 3600) is True
    assert auth_mod._probe_codex_quota_restored(token, not_before='not a time') is True
    clocks.go(PROBE_INTERVAL + 1)
    assert auth_mod._probe_codex_quota_restored(token, not_before=refused_at) is False
    assert len(provider.usage_asked) == 2, 'past the interval the endpoint is asked, and says the window is closed'


def test_with_credits_authorized_the_rotation_still_leaves_an_account_that_refuses(store, provider, clocks):
    """``codex.use_credits`` is on and every account has purchased credits, so the usage probe answers "usable"
    for a window at 100%. The provider refuses all the same. Before the fix the rotation handed account A back on
    every 429 without end; now each account is tried once and the pool reports that nothing is left."""
    (a, b, c), entries = three_accounts()
    pool = store(entries, use_credits=True)
    provider.credits = {'A', 'B', 'C'}
    keys = {'e-A': a, 'e-B': b, 'e-C': c}
    for lap in range(2):
        current, tried = pool.select().id, []
        while current is not None:
            tried.append(current)
            assert len(tried) <= 3, tried
            current = refuse(pool, current, keys[current])
        assert tried == ['e-A', 'e-B', 'e-C'], (lap, tried)
        assert not pool.has_available() and pool.select() is None
        clocks.go(PROBE_INTERVAL + 1)  # the probe is believed again: one more refused call for each account


def test_with_credits_authorized_a_new_process_starts_on_the_next_account(store, provider, clocks):
    """The first account has purchased credits and refuses all the same. The refusal is on disk with its time, so
    a process that starts inside the probe's interval (a new card worker, an auxiliary call, the gateway resolving
    its next turn) starts on the second account without asking anything. Before, each of them asked the usage
    endpoint, was told "usable" and spent a refused call on the first account."""
    (a, b, c), entries = three_accounts()
    pool = store(entries, use_credits=True)
    provider.credits = {'A'}
    del provider.closed_until['B'], provider.closed_until['C']
    assert pool.select().id == 'e-A' and refuse(pool, 'e-A', a) == 'e-B'
    assert [new_process().select().id for _ in range(3)] == ['e-B', 'e-B', 'e-B'] and provider.usage_asked == []
    # Past the interval the probe is believed again: one process tries the first account, is refused, and the
    # mark it writes sends the next ones to the second for another interval.
    clocks.go(PROBE_INTERVAL + 1)
    later = new_process()
    assert later.select().id == 'e-A' and provider.usage_asked == ['A']
    assert refuse(later, 'e-A', a) == 'e-B'
    assert [new_process().select().id for _ in range(2)] == ['e-B', 'e-B'] and provider.usage_asked == ['A']


def test_entries_sharing_the_refused_key_are_marked_and_left_together(store, provider):
    """Five entries, one token pair (the production pool of 2026-10-12) and one more account behind them."""
    a, b = jwt('A'), jwt('B')
    pool = store([entry('d', 'manual:desktop', -3, a, 'rA'), entry('m1', 'manual:device_code', -1, a, 'rA'),
                  entry('s', 'device_code', 0, a, 'rA'), entry('m2', 'manual:device_code', 1, a, 'rA'),
                  entry('m3', 'manual:device_code', 2, a, 'rA'), entry('other', 'manual:device_code', 3, b, 'rB')],
                 singleton=(a, 'rA'))
    assert pool.select().id == 'd'
    assert refuse(pool, 'd', a) == 'other'
    states = {ident: row['last_status'] for ident, row in store.disk().items()}
    assert states == {'d': 'exhausted', 'm1': 'exhausted', 's': 'exhausted', 'm2': 'exhausted', 'm3': 'exhausted',
                      'other': None}
    assert refuse(pool, 'other', b) is None


def test_a_sole_account_revived_inside_the_selection_is_no_recovery(store, provider):
    """NousResearch/hermes-agent 8530d81b55 (#97315): the auth-store sync adopts fresher tokens and clears the
    mark while the rotation is selecting. Handing that entry back reported a rotation that changed nothing."""
    a = jwt('A')
    pool = store([entry('e-A', 'device_code', 0, a, 'rA')], singleton=(a, 'rA'))

    def revive(found):
        updated = replace(found, access_token=jwt('A', expires_in=9 * DAY), last_status=None, last_status_at=None,
                          last_error_code=None, last_error_reason=None, last_error_message=None,
                          last_error_reset_at=None)
        pool._replace_entry(found, updated)
        return updated

    pool._sync_codex_entry_from_auth_store = revive
    assert pool.select().id == 'e-A'
    assert refuse(pool, 'e-A', a) is None


# ── The auxiliary path tells the pool what the main loop tells it ───────

class UsageLimit(Exception):
    """The Codex weekly window, as the OpenAI SDK hands the refusal over (body read in a production log)."""
    status_code = 429
    response = SimpleNamespace(status_code=429, headers={})

    def __init__(self, resets_at):
        super().__init__('Error code: 429 - The usage limit has been reached')
        self.body = {'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                               'plan_type': 'pro', 'resets_at': resets_at}}


class Revoked(Exception):
    """A login the provider revoked: the terminal 401 the main loop writes an entry off for."""
    status_code = 401
    response = SimpleNamespace(status_code=401, headers={})
    body = {'error': {'code': 'token_revoked', 'message': 'Encountered invalidated oauth token for user'}}


@pytest.fixture
def auxiliary(monkeypatch):
    monkeypatch.setattr(auxiliary_client, '_evict_cached_clients', lambda provider: None)
    return auxiliary_client


def test_a_refusal_on_the_auxiliary_path_keeps_the_reset_time(store, provider, auxiliary):
    """A goal judge or a title call meets the weekly window. Before the fix the mark it wrote carried neither the
    reason nor the reset time, so the account was offered again one hour later, and a mark of the main loop that
    did carry them was overwritten."""
    (a, b, c), entries = three_accounts()
    store(entries)
    resets_at = int(time.time()) + RESET_IN
    refusal = UsageLimit(resets_at)

    from agent.agent_runtime_helpers import extract_api_error_context
    told = auxiliary._pool_quota_error_context(refusal)
    main_loop = extract_api_error_context(refusal)
    assert (told['reason'], told['reset_at']) == (main_loop['reason'], main_loop['reset_at'])
    assert (told['reason'], told['reset_at'], told['status_code']) == ('usage_limit_reached', resets_at, 429)

    assert auxiliary._recover_provider_pool('openai-codex', refusal, failed_api_key=a) is True
    marked = store.disk()['e-A']
    assert (marked['last_status'], marked['last_error_code'], marked['last_error_reason']) == (
        'exhausted', 429, 'usage_limit_reached')
    assert marked['last_error_reset_at'] == resets_at
    again = cp.load_pool('openai-codex')
    waits_until = cp._exhausted_until(next(e for e in again.entries() if e.id == 'e-A'))
    assert waits_until == resets_at, 'the window, not the one-hour default'
    assert again.select().id == 'e-B'


def test_a_401_on_the_auxiliary_path_is_marked_as_before(store, provider, auxiliary):
    """What the pool is told of a quota refusal changed, and only that. An auxiliary call answered 401 with a
    terminal reason still leaves the one-hour mark with no reason, where the main loop writes the entry off.
    Making the two agree is another change, with a decision of its own: a manual entry written off is pruned a
    day later."""
    (a, b, c), entries = three_accounts()
    store(entries)
    assert auxiliary._pool_error_context(Revoked('Error code: 401')) == {
        'message': 'Error code: 401', 'status_code': 401}
    assert auxiliary._recover_provider_pool('openai-codex', Revoked('Error code: 401'), failed_api_key=a) is True
    marked = store.disk()['e-A']
    assert (marked['last_status'], marked['last_error_code'], marked.get('last_error_reason')) == (
        'exhausted', 401, None)


def test_a_quota_refusal_that_cannot_be_read_is_marked_as_before(store, provider, auxiliary):
    (a, b, c), entries = three_accounts()
    store(entries)
    refusal = UsageLimit(resets_at=['not', 'a', 'time'])  # the reader of the main loop raises on a value like this
    assert auxiliary._pool_quota_error_context(refusal) == {'message': str(refusal), 'status_code': 429}
    assert auxiliary._recover_provider_pool('openai-codex', refusal, failed_api_key=a) is True
    marked = store.disk()['e-A']
    assert (marked['last_status'], marked['last_error_code'], marked.get('last_error_reset_at')) == (
        'exhausted', 429, None)
    assert cp.load_pool('openai-codex').select().id == 'e-B'
