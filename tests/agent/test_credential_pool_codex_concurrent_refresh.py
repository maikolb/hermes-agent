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
  and d525b443f5);
* a login of the same account in another chain (the ``auth.json`` singleton) is not adopted by a pool entry;
  an entry still follows the singleton while both hold one refresh token;
* a terminal 401 met on the auxiliary path writes the login off with the provider's reason, as the main loop does,
  and the auxiliary path and the main loop are not offered it again.
"""
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
ACCOUNT = {'A': 'a', 'B': 'b', 'C': 'c', 'S': 'a'}  # chain -> account; S is another login of account a
USAGE_LIMIT = {'reason': 'usage_limit_reached', 'message': 'The usage limit has been reached'}


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
        chain = (generation_of(bearer) or '?')[:1]
        self.calls.append(chain)
        if chain in self.revoked:
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
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-codex-cli'))

    def write(entries, singleton=None, config=''):
        data = {'version': 1, 'active_provider': 'openai-codex', 'providers': {},
                'credential_pool': {'openai-codex': entries},
                'suppressed_sources': {'openai-codex': ['device_code', 'manual:device_code']}}
        if singleton:
            data['providers']['openai-codex'] = {
                'tokens': {'access_token': singleton[0], 'refresh_token': singleton[1]},
                'last_refresh': '2026-10-13T16:55:43.660365Z', 'auth_mode': 'chatgpt',
                'label': 'Codex Desktop account'}
        (home / 'auth.json').write_text(json.dumps(data), encoding='utf-8')
        (home / 'config.yaml').write_text(config + 'codex:\n  use_credits: false\n', encoding='utf-8')

    def disk():
        data = json.loads((home / 'auth.json').read_text(encoding='utf-8'))
        rows = {row['id']: row for row in data['credential_pool']['openai-codex']}
        return rows, (data.get('providers') or {}).get('openai-codex')

    write.disk = disk
    return write


def three_logins(first_expires_in=10 * DAY, base_url='https://chatgpt.com/backend-api/codex'):
    return [login('e-A', 'A', 0, first_expires_in, base_url), login('e-B', 'B', 1, base_url=base_url),
            login('e-C', 'C', 2, base_url=base_url)]


def held(pool, ident='e-A'):
    found = next(e for e in pool.entries() if e.id == ident)
    return found.refresh_token, found.last_status


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


def test_processes_that_loaded_the_pool_before_a_refresh_post_its_refresh_token_once(store, endpoints):
    """Login A expires in a minute. Four processes hold the pool and select at once. One posts A's refresh token;
    the others wait on the auth-store lock and take the pair it wrote. Before the fix each of them posted the same
    token, three were answered refresh_token_reused, and the losers wrote the spent pair back over the new one."""
    store(three_logins(first_expires_in=60))
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


def test_a_pool_holding_the_old_pair_never_writes_it_over_the_new_one(store, endpoints):
    """The gateway loaded the pool hours ago. A card worker refreshes login A. Then the provider refuses login B to
    the gateway, which writes its pool. Before the fix that write put A's spent pair back on disk, and the gateway's
    next refresh of A posted it again."""
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


# ── A pool entry and the auth.json singleton ────────────────────────────

def test_a_login_of_the_same_account_in_another_chain_is_not_adopted(store, endpoints):
    """The production state of 2026-10-13: login A and the singleton are two logins of one account, two chains.
    Before the fix login A took the singleton's pair (same account), leaving two holders of one single-use refresh
    token; now it is refreshed with its own."""
    store(three_logins(first_expires_in=60), singleton=(token('S', 1), 'rt-S-1'))
    pool = cp.load_pool('openai-codex')
    pool.select()
    assert held(pool) == ('rt-A-2', 'ok')
    assert endpoints.posted == ['rt-A-1']
    rows, singleton = store.disk()
    assert rows['e-A']['refresh_token'] == 'rt-A-2'
    assert singleton['tokens']['refresh_token'] == 'rt-S-1', 'the singleton keeps its own chain'


def test_an_entry_holding_the_singletons_refresh_token_follows_its_new_access_token(store, endpoints):
    entries = three_logins(first_expires_in=60)
    entries[0]['refresh_token'] = 'rt-S-1'  # one chain: the entry and the singleton hold the same refresh token
    store(entries, singleton=(token('S', 2), 'rt-S-1'))
    pool = cp.load_pool('openai-codex')
    pool.select()
    found = next(e for e in pool.entries() if e.id == 'e-A')
    assert (generation_of(found.access_token), found.refresh_token) == ('S2', 'rt-S-1')
    assert endpoints.posted == []


# ── A terminal 401 on the auxiliary path ────────────────────────────────

AUX_CONFIG = 'auxiliary:\n  compression:\n    provider: openai-codex\n    model: gpt-5.6-luna-900k\n    timeout: 30\n'


@pytest.mark.parametrize('singleton', [False, True], ids=['no-singleton', 'singleton-of-another-chain'])
def test_a_terminal_401_on_the_auxiliary_path_writes_the_login_off(store, endpoints, monkeypatch, singleton):
    """The provider revoked login A. A compression call is refused 401 token_revoked. As the main loop does, the
    entry is refreshed (refused: the chain is revoked) and written off with the provider's reason, and the call goes
    on with login B. Before the fix the auxiliary path refreshed the auth.json login instead (another chain, when
    there is one), retried login A, raised the 401, and left login A in rotation for both paths."""
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, '_client_cache', {})
    store(three_logins(base_url=endpoints.codex_base), singleton=(token('S', 1), 'rt-S-1') if singleton else None,
          config=AUX_CONFIG)
    endpoints.revoked.add('A')

    response = auxiliary_client.call_llm(task='compression', messages=[{'role': 'user', 'content': 'summarise'}])

    assert response.choices[0].message.content == 'summary ok'
    assert endpoints.calls == ['A', 'B']
    assert endpoints.posted == ['rt-A-1'], "login A's own refresh is tried; the singleton's chain is not touched"
    rows, stored_singleton = store.disk()
    assert (rows['e-A']['last_status'], rows['e-A']['last_error_reason'], rows['e-A']['last_error_code']) == (
        'dead', 'token_revoked', 401)
    if singleton:
        assert stored_singleton['tokens']['refresh_token'] == 'rt-S-1'
    assert generation_of(auxiliary_client._read_codex_access_token()) == 'B1'
    assert cp.load_pool('openai-codex').select().id == 'e-B'
