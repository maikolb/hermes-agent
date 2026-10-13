"""One openai-codex pool read by several Hermes processes, with single-use refresh tokens.

On the NFOS production profile the gateway, the card workers, the reviewer console and the Pulso analyst read one
``auth.json`` holding three Codex logins, one per account; each process holds its own pool over it. A refresh token
is single-use: posting it a second time is answered ``refresh_token_reused`` and the login is lost.

What these tests hold, on real pools over a real ``auth.json`` (the OAuth token endpoint and the Codex responses
endpoint are stand-ins on 127.0.0.1, and every token is a fake written here):

* processes that loaded the pool before a refresh post the expiring refresh token once; the others take the pair
  the first one wrote (the pool re-reads its own row under the auth-store lock);
* a pool holding the old pair never writes it over the new one, and a terminal verdict it met on the old pair does
  not kill the new one (token-generation bound writes, ported from NousResearch/hermes-agent d2f54de2cc, badd8f56e4
  and d525b443f5); a pool whose own write failed after its refresh keeps the pair it got;
* a login of the same account in another chain (the ``auth.json`` singleton, in the profile or in the root) is not
  adopted by a pool entry; an entry still follows the singleton while both hold one refresh token, and a row of a
  chain that rotated elsewhere takes the new pair;
* a terminal 401 met on the auxiliary path writes the login off with the provider's reason, as the main loop does,
  and the auxiliary path and the main loop are not offered it again.

The profile lives where production has it, ``<root>/profiles/hermes-project-factory``, in three forms: no Codex
login outside the pool (``none``), a login of account a in another chain in the profile (``profile_singleton``, the
state since 2026-10-13 16:55:43Z) and that login only in the root's ``auth.json`` (``root_singleton``, the state
before it).
"""
import asyncio
import base64
import json
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

import hermes_cli.auth as auth_mod
from agent import credential_pool as cp

DAY = 86400
# chain -> account; S (in the profile) and R (in the root) are other logins of account a
ACCOUNT = {'A': 'a', 'B': 'b', 'C': 'c', 'S': 'a', 'R': 'a'}
USAGE_LIMIT = {'reason': 'usage_limit_reached', 'message': 'The usage limit has been reached'}
FORMS = ['none', 'profile_singleton', 'root_singleton']
SUPPRESSED = ('device_code', 'manual:device_code')


def token(chain, generation, expires_in=10 * DAY):
    """A fake Codex access token of ``chain``: unsigned, never sent beyond 127.0.0.1."""
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    account = ACCOUNT[chain]
    claims = {'exp': int(time.time() + expires_in), 'jti': f'{chain}{generation}', 'sub': f'user-{account}',
              'https://api.openai.com/auth': {'chatgpt_account_id': f'acct-{account}'}}
    return f"{part({'alg': 'none'})}.{part(claims)}.fake"


def generation_of(access_token):
    return auth_mod._decode_jwt_claims(access_token).get('jti')


class Endpoints:
    """The token endpoint (each refresh token is accepted once) and the Codex responses endpoint."""

    def __init__(self):
        self.lock = threading.Lock()
        self.spent, self.revoked = set(), set()
        self.posted, self.answers, self.calls = [], [], []

    def refresh(self, refresh_token):
        time.sleep(0.2)  # the network round trip the other processes wait through
        with self.lock:
            self.posted.append(refresh_token)
            chain, _, generation = refresh_token.rpartition('-')
            chain = chain.removeprefix('rt-')
            if chain in self.revoked:
                self.answers.append('invalid_grant')
                return 401, {'error': 'invalid_grant', 'error_description': 'stand-in: revoked'}
            if refresh_token in self.spent:
                self.answers.append('refresh_token_reused')
                return 401, {'error': {'code': 'refresh_token_reused', 'message': 'stand-in: already used'}}
            self.spent.add(refresh_token)
            self.answers.append('issued')
            following = int(generation) + 1
            return 200, {'access_token': token(chain, following), 'refresh_token': f'rt-{chain}-{following}'}

    def respond(self, bearer):
        generation = generation_of(bearer) or '?'
        self.calls.append(generation)
        if generation[:1] in self.revoked:
            return 401, 'application/json', json.dumps({'error': {
                'code': 'token_revoked', 'type': 'invalid_request_error',
                'message': 'Encountered invalidated oauth token for user'}}).encode()
        events = [
            {'type': 'response.created', 'response': {'id': 'resp_1', 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_text.delta', 'item_id': 'msg_1', 'output_index': 0, 'delta': 'summary ok'},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': {
                'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'summary ok', 'annotations': []}]}},
            {'type': 'response.completed', 'response': {
                'id': 'resp_1', 'status': 'completed', 'output': [],
                'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}},
        ]
        body = ''.join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        return 200, 'text/event-stream', body


@pytest.fixture
def endpoints(monkeypatch):
    stand_in = Endpoints()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if self.path.startswith('/oauth/token'):
                code, payload = stand_in.refresh((parse_qs(raw.decode()).get('refresh_token') or [''])[0])
                kind, body = 'application/json', json.dumps(payload).encode()
            elif self.path.endswith('/responses'):
                code, kind, body = stand_in.respond((self.headers.get('Authorization') or '')[len('Bearer '):])
            else:
                code, kind, body = 404, 'application/json', b'{}'
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    stand_in.token_url, stand_in.codex_base = f'{base}/oauth/token', f'{base}/backend-api/codex'
    monkeypatch.setattr(auth_mod, 'CODEX_OAUTH_TOKEN_URL', stand_in.token_url)
    yield stand_in
    server.shutdown()


def login(ident, chain, priority, expires_in=10 * DAY, base_url='https://chatgpt.com/backend-api/codex'):
    """A pool entry as `hermes auth add openai-codex` leaves it."""
    return {'id': ident, 'label': f'login {priority + 1}', 'auth_type': 'oauth', 'priority': priority,
            'source': 'manual:device_code', 'access_token': token(chain, 1, expires_in),
            'refresh_token': f'rt-{chain}-1', 'base_url': base_url,
            'last_refresh': '2026-10-12T23:45:00.000000Z'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / 'srv-hermes'
    home = root / 'profiles' / 'hermes-project-factory'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-codex-cli'))

    def write(entries, singleton=None, config='', form='none', suppressed=SUPPRESSED):
        if form == 'profile_singleton':
            singleton = (token('S', 1), 'rt-S-1')
        data = {'version': 1, 'active_provider': 'openai-codex', 'providers': {},
                'credential_pool': {'openai-codex': entries}}
        if suppressed:
            data['suppressed_sources'] = {'openai-codex': list(suppressed)}
        if singleton:
            data['providers']['openai-codex'] = {
                'tokens': {'access_token': singleton[0], 'refresh_token': singleton[1]},
                'last_refresh': '2026-10-13T16:55:43.660365Z', 'auth_mode': 'chatgpt',
                'label': 'Codex Desktop account'}
        (home / 'auth.json').write_text(json.dumps(data), encoding='utf-8')
        (home / 'config.yaml').write_text(config + 'codex:\n  use_credits: false\n', encoding='utf-8')
        if form == 'root_singleton':
            (root / 'auth.json').write_text(json.dumps({
                'version': 1, 'active_provider': 'openai-codex', 'providers': {'openai-codex': {
                    'tokens': {'access_token': token('R', 1), 'refresh_token': 'rt-R-1'},
                    'last_refresh': '2026-09-23T17:50:00.000000Z', 'auth_mode': 'chatgpt',
                    'label': 'Codex Desktop account'}}}), encoding='utf-8')

    def disk():
        data = json.loads((home / 'auth.json').read_text(encoding='utf-8'))
        rows = {row['id']: row for row in data['credential_pool']['openai-codex']}
        return rows, (data.get('providers') or {}).get('openai-codex')

    def root_refresh_token():
        path = root / 'auth.json'
        if not path.exists():
            return None
        state = (json.loads(path.read_text(encoding='utf-8')).get('providers') or {}).get('openai-codex') or {}
        return (state.get('tokens') or {}).get('refresh_token')

    write.disk = disk
    write.root_refresh_token = root_refresh_token
    return write


def three_logins(first_expires_in=10 * DAY, base_url='https://chatgpt.com/backend-api/codex'):
    return [login('e-A', 'A', 0, first_expires_in, base_url), login('e-B', 'B', 1, base_url=base_url),
            login('e-C', 'C', 2, base_url=base_url)]


def held(pool, ident='e-A'):
    found = next(e for e in pool.entries() if e.id == ident)
    return found.refresh_token, found.last_status


def access_of(pool, ident='e-A'):
    return next(e for e in pool.entries() if e.id == ident).access_token


def assert_other_chains_untouched(store, form):
    """The login of another chain keeps its own pair, wherever it lives, and the root's is never read into the
    profile."""
    _rows, singleton = store.disk()
    if form == 'profile_singleton':
        assert singleton['tokens']['refresh_token'] == 'rt-S-1'
    if form == 'root_singleton':
        assert store.root_refresh_token() == 'rt-R-1'
        assert singleton is None, 'the root login was not copied into the profile'


# ── The refresh of one row by several processes ─────────────────────────

def _process_that_loaded_the_pool_before(token_url, barrier, out):
    """A Hermes process (gateway, card worker, reviewer console, Pulso): its own interpreter and its own pool."""
    import hermes_cli.auth as auth_in_process
    auth_in_process.CODEX_OAUTH_TOKEN_URL = token_url
    from agent.credential_pool import load_pool

    pool = load_pool('openai-codex')
    barrier.wait(timeout=120)
    pool.select()
    out.put(held(pool))


@pytest.mark.parametrize('form', FORMS)
def test_processes_that_loaded_the_pool_before_a_refresh_post_its_refresh_token_once(store, endpoints, form):
    """Login A expires in a minute. Four processes hold the pool and select at once. One posts A's refresh token;
    the others wait on the auth-store lock and take the pair it wrote. Before the fix each of them posted the same
    token, three were answered refresh_token_reused, and the losers wrote the spent pair back over the new one."""
    store(three_logins(first_expires_in=60), form=form)
    context = multiprocessing.get_context('spawn')
    barrier, out = context.Barrier(4), context.Queue()
    processes = [context.Process(target=_process_that_loaded_the_pool_before, args=(endpoints.token_url, barrier, out))
                 for _ in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=180)
    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    assert endpoints.posted == ['rt-A-1'] and endpoints.answers == ['issued']
    assert sorted(out.get(timeout=10) for _ in range(4)) == [('rt-A-2', 'ok')] * 4
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2' and rows['e-A']['last_status'] == 'ok'
    assert generation_of(rows['e-A']['access_token']) == 'A2'
    assert_other_chains_untouched(store, form)


def _gateway_holding_the_first_pair(token_url, loaded, go, out):
    import hermes_cli.auth as auth_in_process
    auth_in_process.CODEX_OAUTH_TOKEN_URL = token_url
    from agent.credential_pool import load_pool

    pool = load_pool('openai-codex')
    first_pair = access_of(pool)
    loaded.set()
    go.wait(timeout=120)
    # The provider refused the first pair (401): the main loop asks for the entry that made the call.
    refreshed = pool.try_refresh_matching(api_key_hint=first_pair, credential_id='e-A')
    out.put((None if refreshed is None else refreshed.refresh_token,) + held(pool))


def _worker_refreshing_login_a(token_url, out):
    import hermes_cli.auth as auth_in_process
    auth_in_process.CODEX_OAUTH_TOKEN_URL = token_url
    from agent.credential_pool import load_pool

    pool = load_pool('openai-codex')
    refreshed = pool.try_refresh_matching(api_key_hint=access_of(pool))
    out.put(None if refreshed is None else refreshed.refresh_token)


@pytest.mark.parametrize('form', FORMS)
def test_a_forced_refresh_after_another_process_rotated_the_row_posts_nothing(store, endpoints, form):
    """The gateway loaded the pool; a card worker then refreshed login A (a 401 on its side). When the gateway's own
    call with the first pair is refused, the refresh it asks for takes the worker's pair. Before the fix it posted
    the spent refresh token and lost login A."""
    store(three_logins(), form=form)
    context = multiprocessing.get_context('spawn')
    loaded, go, gateway_out, worker_out = context.Event(), context.Event(), context.Queue(), context.Queue()
    gateway = context.Process(target=_gateway_holding_the_first_pair, args=(endpoints.token_url, loaded, go, gateway_out))
    gateway.start()
    assert loaded.wait(120)
    worker = context.Process(target=_worker_refreshing_login_a, args=(endpoints.token_url, worker_out))
    worker.start()
    worker.join(timeout=180)
    go.set()
    gateway.join(timeout=180)
    assert [gateway.exitcode, worker.exitcode] == [0, 0]
    assert worker_out.get(timeout=10) == 'rt-A-2'
    assert gateway_out.get(timeout=10) == ('rt-A-2', 'rt-A-2', 'ok')
    assert endpoints.posted == ['rt-A-1'] and endpoints.answers == ['issued']
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2'
    assert_other_chains_untouched(store, form)


def _long_lived_gateway(token_url, loaded, go, out):
    import hermes_cli.auth as auth_in_process
    auth_in_process.CODEX_OAUTH_TOKEN_URL = token_url
    from agent.credential_pool import load_pool

    pool = load_pool('openai-codex')
    loaded.set()
    go.wait(timeout=120)
    b = next(e for e in pool.entries() if e.id == 'e-B')
    pool.mark_exhausted_and_rotate(status_code=429, error_context=dict(USAGE_LIMIT, reset_at=time.time() + DAY),
                                   credential_id='e-B', api_key_hint=b.access_token)
    after_write = held(pool)[0]
    pool.select()
    out.put((after_write,) + held(pool))


def _card_worker_selecting(token_url, out):
    import hermes_cli.auth as auth_in_process
    auth_in_process.CODEX_OAUTH_TOKEN_URL = token_url
    from agent.credential_pool import load_pool

    pool = load_pool('openai-codex')
    pool.select()
    out.put(held(pool)[0])


@pytest.mark.parametrize('form', FORMS)
def test_a_long_lived_process_holding_the_old_pair_never_writes_it_back(store, endpoints, form):
    """The gateway process loaded the pool before a card worker refreshed login A. Then the provider refuses login B
    to the gateway, which writes its whole pool, and selects. Before the fix that write put A's spent pair back on
    disk and the gateway's next refresh of A posted it again."""
    store(three_logins(first_expires_in=60), form=form)
    context = multiprocessing.get_context('spawn')
    loaded, go, gateway_out, worker_out = context.Event(), context.Event(), context.Queue(), context.Queue()
    gateway = context.Process(target=_long_lived_gateway, args=(endpoints.token_url, loaded, go, gateway_out))
    gateway.start()
    assert loaded.wait(120)
    worker = context.Process(target=_card_worker_selecting, args=(endpoints.token_url, worker_out))
    worker.start()
    worker.join(timeout=180)
    go.set()
    gateway.join(timeout=180)
    assert [gateway.exitcode, worker.exitcode] == [0, 0]
    assert worker_out.get(timeout=10) == 'rt-A-2'
    after_write, gateway_holds, status = gateway_out.get(timeout=10)
    assert (after_write, gateway_holds) == ('rt-A-2', 'rt-A-2') and status in (None, 'ok')
    assert endpoints.posted == ['rt-A-1'] and endpoints.answers == ['issued']
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2' and rows['e-B']['last_status'] == 'exhausted'
    assert_other_chains_untouched(store, form)


def test_a_pool_holding_the_old_pair_never_writes_it_over_the_new_one(store, endpoints):
    """The same in one process: the gateway's write after the worker's refresh keeps the new pair on disk and the
    gateway adopts it."""
    store(three_logins(first_expires_in=60))
    gateway, worker = cp.load_pool('openai-codex'), cp.load_pool('openai-codex')
    worker.select()
    assert endpoints.posted == ['rt-A-1']

    b = next(e for e in gateway.entries() if e.id == 'e-B')
    gateway.mark_exhausted_and_rotate(status_code=429, error_context=dict(USAGE_LIMIT, reset_at=time.time() + DAY),
                                      credential_id='e-B', api_key_hint=b.access_token)
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2', 'the new pair stays on disk'
    assert rows['e-B']['last_status'] == 'exhausted', "the gateway's own mark is written"
    assert held(gateway)[0] == 'rt-A-2', 'and the gateway holds the new pair from its write on'
    gateway.select()
    assert endpoints.posted == ['rt-A-1'] and 'refresh_token_reused' not in endpoints.answers


def test_a_refresh_whose_write_failed_is_followed_by_the_pair_it_got(store, endpoints, monkeypatch):
    """The refresh POST of login A was answered (rt-A-2 issued, rt-A-1 spent) but writing auth.json failed once. The
    process holds the new pair; the disk still has the spent one. Its next refresh of A (a 401 on the new pair) posts
    rt-A-2. Re-reading the row must not trade the pair the process got for the spent one on disk."""
    store(three_logins(first_expires_in=60))
    pool = cp.load_pool('openai-codex')
    real_save = auth_mod._save_auth_store
    failures = {'left': 1}

    def save_failing_once(auth_store):
        if failures['left']:
            failures['left'] -= 1
            raise OSError(28, 'No space left on device')
        return real_save(auth_store)

    monkeypatch.setattr(auth_mod, '_save_auth_store', save_failing_once)
    with pytest.raises(OSError):
        pool.select()
    assert endpoints.posted == ['rt-A-1'] and held(pool)[0] == 'rt-A-2'
    assert store.disk()[0]['e-A']['refresh_token'] == 'rt-A-1'

    refreshed = pool.try_refresh_matching(api_key_hint=access_of(pool))

    assert refreshed is not None and refreshed.refresh_token == 'rt-A-3'
    assert endpoints.posted == ['rt-A-1', 'rt-A-2'] and endpoints.answers == ['issued', 'issued']
    assert store.disk()[0]['e-A']['refresh_token'] == 'rt-A-3'


def test_a_terminal_verdict_met_on_the_old_pair_does_not_kill_the_new_one(store, endpoints):
    store(three_logins(first_expires_in=60))
    stale, peer = cp.load_pool('openai-codex'), cp.load_pool('openai-codex')
    old_access = next(e for e in stale.entries() if e.id == 'e-A').access_token
    peer.select()
    stale.mark_exhausted_and_rotate(status_code=401, credential_id='e-A', api_key_hint=old_access,
                                    error_context={'reason': 'invalid_grant', 'message': 'refresh token already used'})
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2' and rows['e-A']['last_status'] != 'dead'
    assert cp.load_pool('openai-codex').select().id == 'e-A'


def test_a_terminal_verdict_on_the_current_pair_is_still_written(store, endpoints):
    store(three_logins())
    pool = cp.load_pool('openai-codex')
    current = next(e for e in pool.entries() if e.id == 'e-A').access_token
    pool.mark_exhausted_and_rotate(status_code=401, credential_id='e-A', api_key_hint=current,
                                   error_context={'reason': 'token_revoked', 'message': 'revoked'})
    rows, _singleton = store.disk()
    assert (rows['e-A']['refresh_token'], rows['e-A']['last_status']) == ('rt-A-1', 'dead')
    assert cp.load_pool('openai-codex').select().id == 'e-B'


def test_a_forced_refresh_does_not_hand_back_a_login_another_process_wrote_off(store, endpoints):
    """A card worker refreshed login A and then met a terminal 401 on the new pair: it wrote A off. The gateway's
    call with the first pair is refused too; the refresh it asks for finds the row written off and recovers
    nothing, so the main loop rotates to login B instead of retrying a revoked login."""
    store(three_logins())
    gateway, worker = cp.load_pool('openai-codex'), cp.load_pool('openai-codex')
    first_pair = access_of(gateway)
    refreshed = worker.try_refresh_matching(credential_id='e-A')
    assert refreshed is not None and refreshed.refresh_token == 'rt-A-2'
    worker.mark_exhausted_and_rotate(status_code=401, credential_id='e-A', api_key_hint=refreshed.access_token,
                                     error_context={'reason': 'token_revoked', 'message': 'revoked'})
    assert store.disk()[0]['e-A']['last_status'] == 'dead'

    assert gateway.try_refresh_matching(api_key_hint=first_pair, credential_id='e-A') is None
    assert endpoints.posted == ['rt-A-1']
    assert gateway.select().id == 'e-B'


# ── A pool entry and the auth.json singleton ────────────────────────────

@pytest.mark.parametrize('form', ['profile_singleton', 'root_singleton'])
def test_a_login_of_the_same_account_in_another_chain_is_not_adopted(store, endpoints, form):
    """The production state of 2026-10-13: login A and the singleton (in the profile since 16:55:43Z, in the root
    before) are two logins of one account, two chains. Before the fix login A took the singleton's pair (same
    account), leaving two holders of one single-use refresh token; now it is refreshed with its own."""
    store(three_logins(first_expires_in=60), form=form)
    pool = cp.load_pool('openai-codex')
    pool.select()
    assert held(pool) == ('rt-A-2', 'ok')
    assert endpoints.posted == ['rt-A-1']
    rows, _singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2'
    assert_other_chains_untouched(store, form)


def test_an_entry_holding_the_singletons_refresh_token_follows_its_new_access_token(store, endpoints):
    entries = three_logins(first_expires_in=60)
    entries[0]['refresh_token'] = 'rt-S-1'  # one chain: the entry and the singleton hold the same refresh token
    store(entries, singleton=(token('S', 2), 'rt-S-1'))
    pool = cp.load_pool('openai-codex')
    pool.select()
    found = next(e for e in pool.entries() if e.id == 'e-A')
    assert (generation_of(found.access_token), found.refresh_token) == ('S2', 'rt-S-1')
    assert endpoints.posted == []


def test_an_entry_of_the_singletons_chain_follows_a_rotation_made_on_the_singleton_side(store, endpoints):
    """Login A holds the singleton's refresh token with an older access token. The singleton side refreshes the
    chain (resolve_codex_runtime_credentials); the entry's refresh token is now spent and it takes the new pair,
    instead of posting the spent one when it expires."""
    entries = three_logins()
    entries[0].update(access_token=token('S', 0, expires_in=60), refresh_token='rt-S-1')
    store(entries, singleton=(token('S', 1, expires_in=60), 'rt-S-1'))
    auth_mod.resolve_codex_runtime_credentials(force_refresh=True)
    rows, singleton = store.disk()
    assert singleton['tokens']['refresh_token'] == 'rt-S-2'
    assert rows['e-A']['refresh_token'] == 'rt-S-2'
    pool = cp.load_pool('openai-codex')
    pool.select()
    assert endpoints.posted == ['rt-S-1'] and endpoints.answers == ['issued']
    assert held(pool)[0] == 'rt-S-2'


def test_a_legacy_alias_follows_the_rotation_of_its_device_code_login(store, endpoints):
    """The singleton is seeded as the device_code entry and a manual:device_code alias holds the same chain (the
    #33000 era). The pool refreshes the device_code entry and writes the pair back to the singleton; the alias's
    refresh token is spent with it and the alias takes the new pair, in this pool and in a pool loaded before."""
    singleton_access = token('S', 1, expires_in=60)
    device_code = dict(login('e-dc', 'S', 0, expires_in=60), source='device_code', access_token=singleton_access)
    alias = login('e-alias', 'S', 1, expires_in=DAY)
    store([device_code, alias, login('e-B', 'B', 2)], singleton=(singleton_access, 'rt-S-1'), suppressed=())
    loaded_before = cp.load_pool('openai-codex')
    pool = cp.load_pool('openai-codex')
    pool.select()
    assert endpoints.posted == ['rt-S-1']
    rows, singleton = store.disk()
    assert (singleton['tokens']['refresh_token'], rows['e-dc']['refresh_token'], rows['e-alias']['refresh_token']) == (
        'rt-S-2', 'rt-S-2', 'rt-S-2')
    assert held(pool, 'e-alias')[0] == 'rt-S-2'

    got = loaded_before.try_refresh_matching(credential_id='e-alias')
    assert got is not None and got.refresh_token == 'rt-S-2'
    assert endpoints.posted == ['rt-S-1'] and 'refresh_token_reused' not in endpoints.answers


# ── A terminal 401 on the auxiliary path ────────────────────────────────

AUX_CONFIG = 'auxiliary:\n  compression:\n    provider: openai-codex\n    model: gpt-5.6-luna-900k\n    timeout: 30\n'


def _compress(auxiliary_client):
    response = auxiliary_client.call_llm(task='compression', messages=[{'role': 'user', 'content': 'summarise'}])
    return response.choices[0].message.content


@pytest.mark.parametrize('form', FORMS)
def test_a_terminal_401_on_the_auxiliary_path_writes_the_login_off(store, endpoints, monkeypatch, form):
    """The provider revoked login A. A compression call is refused 401 token_revoked. As the main loop does, the
    entry is refreshed (refused: the chain is revoked) and written off with the provider's reason, and the call goes
    on with login B. Before the fix the auxiliary path refreshed the auth.json login instead (another chain, when
    there is one), retried login A, raised the 401, and left login A in rotation for both paths."""
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, '_client_cache', {})
    store(three_logins(base_url=endpoints.codex_base), form=form, config=AUX_CONFIG)
    endpoints.revoked.add('A')

    assert _compress(auxiliary_client) == 'summary ok'
    assert endpoints.calls == ['A1', 'B1']
    assert endpoints.posted == ['rt-A-1'], "login A's own refresh is tried; the singleton's chain is not touched"
    rows, _singleton = store.disk()
    assert (rows['e-A']['last_status'], rows['e-A']['last_error_reason'], rows['e-A']['last_error_code']) == (
        'dead', 'token_revoked', 401)
    assert_other_chains_untouched(store, form)
    assert generation_of(auxiliary_client._read_codex_access_token()) == 'B1'
    assert cp.load_pool('openai-codex').select().id == 'e-B'


@pytest.mark.parametrize('form', FORMS)
def test_a_terminal_401_on_the_async_auxiliary_path_writes_the_login_off(store, endpoints, monkeypatch, form):
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, '_client_cache', {})
    store(three_logins(base_url=endpoints.codex_base), form=form, config=AUX_CONFIG)
    endpoints.revoked.add('A')

    async def compress():
        return await auxiliary_client.async_call_llm(
            task='compression', messages=[{'role': 'user', 'content': 'summarise'}])

    assert asyncio.run(compress()).choices[0].message.content == 'summary ok'
    assert endpoints.calls == ['A1', 'B1']
    assert endpoints.posted == ['rt-A-1']
    rows, _singleton = store.disk()
    assert (rows['e-A']['last_status'], rows['e-A']['last_error_reason']) == ('dead', 'token_revoked')
    assert_other_chains_untouched(store, form)


@pytest.mark.parametrize('form', FORMS)
def test_a_second_revoked_login_met_on_the_auxiliary_retry_is_written_off_too(store, endpoints, monkeypatch, form):
    """Logins A and B are revoked. The auxiliary path writes A off and retries with B, which is refused too: B is
    refreshed and written off with the provider's reason as well (before, it was benched five minutes without a
    reason and came back to both paths), and the next call goes to login C."""
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, '_client_cache', {})
    store(three_logins(base_url=endpoints.codex_base), form=form, config=AUX_CONFIG)
    endpoints.revoked.update({'A', 'B'})

    with pytest.raises(Exception) as refused:
        _compress(auxiliary_client)
    assert getattr(refused.value, 'status_code', None) == 401
    assert endpoints.calls == ['A1', 'B1']
    assert endpoints.posted == ['rt-A-1', 'rt-B-1']
    rows, _singleton = store.disk()
    for ident in ('e-A', 'e-B'):
        assert (rows[ident]['last_status'], rows[ident]['last_error_reason']) == ('dead', 'token_revoked')
    assert_other_chains_untouched(store, form)
    assert _compress(auxiliary_client) == 'summary ok'
    assert endpoints.calls[-1] == 'C1'


@pytest.mark.parametrize('form', FORMS)
def test_a_cached_auxiliary_client_holding_a_rotated_pair_goes_to_the_pool(store, endpoints, monkeypatch, form):
    """The gateway's compression client was built with login A's first pair and stays cached under A's entry. A card
    worker then refreshed login A, and the provider revoked it. The cached client's 401 is not the auth.json login's:
    it is not answered by refreshing that login (another chain, or the root's login read into the profile), as the
    main loop never does. The pool takes it, the current pair of A is tried and A is written off with the provider's
    reason; the next call goes to login B."""
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, '_client_cache', {})
    store(three_logins(base_url=endpoints.codex_base), form=form, config=AUX_CONFIG)
    assert _compress(auxiliary_client) == 'summary ok'
    worker = cp.load_pool('openai-codex')
    assert worker.try_refresh_matching(api_key_hint=access_of(worker)).refresh_token == 'rt-A-2'
    endpoints.revoked.add('A')

    with pytest.raises(Exception) as refused:
        _compress(auxiliary_client)
    assert getattr(refused.value, 'status_code', None) == 401
    assert endpoints.calls == ['A1', 'A1', 'A2']
    assert endpoints.posted == ['rt-A-1', 'rt-A-2'], 'only chain A: the worker, then A refused on its current pair'
    rows, _singleton = store.disk()
    assert (rows['e-A']['last_status'], rows['e-A']['last_error_reason']) == ('dead', 'token_revoked')
    assert_other_chains_untouched(store, form)
    assert _compress(auxiliary_client) == 'summary ok'
    assert endpoints.calls[-1] == 'B1'
