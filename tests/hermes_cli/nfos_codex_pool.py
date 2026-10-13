"""Shared pieces of the suites that put several ChatGPT accounts behind one openai-codex route
(tests/agent/test_credential_pool_codex_accounts, tests/hermes_cli/test_nfos_codex_account_rotation).

Nothing here reaches a provider and no real credential is read: every token is a fake written by `jwt`, and the
provider is `Provider`, which stands in for the three endpoints the runtime talks to (the model, the usage endpoint
the pool probes and the token endpoint a refresh posts to). The credential path itself is the real one: a real
`auth.json` in a temporary home, the real pool over it, the real route resolution of the worker and a real agent.
"""
import base64
import json
import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.auth as auth_mod
from tests.hermes_cli.nfos_quota_wall import (
    PROCESS_ENV, WORKER_ENV, WORKER_PLACE, CodexUsageLimit, SkippedWaits, clear_env, metered_reply, option, reply,
)

DAY = 86400
CODEX_PROVIDER, CODEX_MODEL = 'openai-codex', 'gpt-6-astra'
CODEX_BASE_URL = 'https://chatgpt.com/backend-api/codex'
# Model calls one run may make before the stand-in provider stops it: a rotation that never ends is a result to
# report, not a suite that hangs.
CALL_CEILING = 60


def jwt(account, expires_in=10 * DAY, identity=True):
    """A fake Codex access token of ``account``: unsigned, never sent anywhere."""
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    claims = {'exp': int(time.time() + expires_in)}
    if identity:
        claims.update({'sub': f'user-{account}',
                       'https://api.openai.com/auth': {'chatgpt_account_id': f'acct-{account}'}})
    return f"{part({'alg': 'none'})}.{part(claims)}.fake-{account}"


def account_of(token):
    return auth_mod._decode_jwt_claims(token).get('sub', 'user-?').split('-', 1)[1]


def entry(ident, source, priority, access, refresh, **extra):
    """A pool entry as `hermes auth add openai-codex` leaves it. The label is a position, never an account name."""
    return dict({'id': ident, 'label': f'login {priority + 1}', 'auth_type': 'oauth', 'priority': priority,
                 'source': source, 'access_token': access, 'refresh_token': refresh, 'base_url': CODEX_BASE_URL},
                **extra)


def write_auth(home, entries, singleton=None, singleton_refreshed='2026-10-08T21:35:20Z'):
    data = {'version': 1, 'providers': {}, 'credential_pool': {CODEX_PROVIDER: entries}}
    if singleton:
        data['providers'][CODEX_PROVIDER] = {
            'tokens': {'access_token': singleton[0], 'refresh_token': singleton[1]},
            'last_refresh': singleton_refreshed, 'auth_mode': 'chatgpt'}
    (home / 'auth.json').write_text(json.dumps(data), encoding='utf-8')


def read_pool(home):
    rows = json.loads((home / 'auth.json').read_text(encoding='utf-8'))['credential_pool'][CODEX_PROVIDER]
    return {row['id']: row for row in rows}


class Stopped(BaseException):
    """The stand-in provider's ceiling of model calls was reached."""


class Provider:
    """The provider as the runtime reaches it, for the accounts of one profile.

    ``closed_until`` says which accounts are out of quota and when the window of each reopens (epoch seconds);
    an account not in it has quota. A model call of an account out of quota is refused with the body Codex sends
    (`usage_limit_reached` and the reset time). The usage endpoint reports a closed window at 100% and an open one
    at 30%, unless ``used`` says otherwise for an account; ``credits`` are the accounts with purchased credits."""

    def __init__(self, monkeypatch, closed_until=None):
        self.closed_until = dict(closed_until or {})
        self.used, self.credits = {}, set()
        self.calls, self.usage_asked, self.refresh_posted = [], [], []
        monkeypatch.setattr(auth_mod.httpx, 'Client', self)
        monkeypatch.setattr(auth_mod, '_codex_quota_probe_cache', {})

    # the model
    def answer(self, account, text='The total is read from the wrong column; correcting it next.'):
        self.calls.append(account)
        if len(self.calls) > CALL_CEILING:
            raise Stopped(f'{len(self.calls)} model calls')
        until = self.closed_until.get(account)
        if until is not None:
            self.used.pop(account, None)  # from its refusal on, the usage endpoint shows the window closed
            raise CodexUsageLimit(resets_in=int(until - time.time()))
        return metered_reply(text)

    # httpx.Client, for the usage endpoint and the token endpoint
    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        assert url.endswith('/usage'), url
        account = (headers or {}).get('ChatGPT-Account-Id', 'acct-?').split('-', 1)[1]
        self.usage_asked.append(account)
        used = self.used.get(account, 100.0 if self.closed_until.get(account) is not None else 30.0)
        payload = {'rate_limit': {'primary_window': {'used_percent': used}, 'secondary_window': {'used_percent': used}},
                   'credits': {'has_credits': account in self.credits}}
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def post(self, url, headers=None, data=None):
        sent = (data or {}).get('refresh_token')
        self.refresh_posted.append(sent)
        account = str(sent)[1:2]  # 'rB' is account B's refresh token
        payload = {'access_token': jwt(account), 'refresh_token': f'{sent}+'}
        return SimpleNamespace(status_code=200, json=lambda: payload, headers={})


def three_logins(home, **first):
    """Three accounts, one login each, in the order the owner added them. Returns the access token of each."""
    tokens = {name: jwt(name) for name in 'ABC'}
    write_auth(home, [entry(f'e-{name}', 'manual:device_code', place, tokens[name], f'r{name}',
                            **(first if name == 'A' else {}))
                      for place, name in enumerate('ABC')])
    return tokens


class RateLimitError(Exception):
    """The goal judge shares the executor's provider."""


def run_pooled_executor(monkeypatch, argv, env, provider, judge_reply=None):
    """The executor child, run in this process: the real quiet single-query path of `cli.main`.

    As in `nfos_quota_wall.run_executor` the model call, the goal judge and the client's sleeps are stand-ins.
    Nothing of the credential path is: the worker resolves its route with the real `_ensure_runtime_credentials`
    over the `auth.json` of the home the launch gave it, and the real agent it builds holds the pool that
    resolution returned. Each model call is answered for the account whose token the agent holds at that moment.
    Returns what the process did and the code it left with (`None` when the provider's ceiling stopped it)."""
    import cli as cli_mod
    from agent import auxiliary_client, conversation_loop
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from run_agent import AIAgent

    place = {key: os.environ.get(key) for key in WORKER_PLACE}
    for key in WORKER_ENV + WORKER_PLACE:
        if key in env:
            monkeypatch.setenv(key, env[key])
    clear_env(monkeypatch, PROCESS_ENV)
    # A worker is a new process: it starts with no usage verdict kept from an earlier one.
    monkeypatch.setattr(auth_mod, '_codex_quota_probe_cache', {})
    first_call = len(provider.calls)
    seen = SimpleNamespace(agent=None, turns=0, results=[], judge_calls=0, waits=SkippedWaits(), stopped=False,
                           fallback=[])
    monkeypatch.setattr(conversation_loop, 'time', seen.waits)

    def model_call(*args, **kwargs):
        return provider.answer(account_of(seen.agent.api_key))

    def judge(*args, **kwargs):
        seen.judge_calls += 1
        if judge_reply is None:
            raise RateLimitError('Error code: 429 - Rate limit exceeded.')
        return reply(judge_reply)

    class WorkerCLI(CLIAgentSetupMixin):
        """HermesCLI reduced to what the quiet single-query path reads, on the real credential resolution."""

        def __init__(self, **options):
            self.requested_provider = self.provider = options.get('provider')
            self.model = options.get('model')
            self._explicit_api_key = self._explicit_base_url = None
            self._fallback_model = []
            self.api_key = self.base_url = self.api_mode = self.acp_command = None
            self.acp_args = []
            self._credential_pool = self.agent = None
            self.session_id = 'executor'
            self.conversation_history = []
            self._active_agent_route_signature = 'executor'

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _normalize_model_for_provider(self, resolved_provider):
            return False

        def _resolve_turn_agent_config(self, query):
            return {'signature': 'executor', 'model': None, 'runtime': None, 'request_overrides': None}

        def _init_agent(self, **kwargs):
            with (patch('run_agent.get_tool_definitions', return_value=[]),
                  patch('run_agent.check_toolset_requirements', return_value={}),
                  patch('run_agent.OpenAI')):
                agent = AIAgent(api_key=self.api_key, base_url=self.base_url, provider=self.provider,
                                model=self.model, credential_pool=self._credential_pool, quiet_mode=True,
                                skip_context_files=True, skip_memory=True, fallback_model=[])
            # The stand-in provider answers in the chat-completions shape, whatever transport the route would use.
            agent.api_mode = 'chat_completions'
            agent.client = MagicMock()
            agent._interruptible_api_call = agent._interruptible_streaming_api_call = model_call
            agent._persist_session = agent._save_trajectory = lambda *args, **kwargs: None
            # A rotation rebuilds the client for the next account; no client is used here.
            agent._replace_primary_openai_client = lambda **kwargs: True
            real_turn, real_fallback = agent.run_conversation, agent._try_activate_fallback

            def turn(*args, **kwargs):
                seen.turns += 1
                result = real_turn(*args, **kwargs)
                seen.results.append(result)
                return result

            def fallback(*args, **kwargs):
                switched = real_fallback(*args, **kwargs)
                seen.fallback.append(switched)
                return switched

            agent.run_conversation, agent._try_activate_fallback = turn, fallback
            self.agent = seen.agent = agent
            self.session_id = agent.session_id
            return True

    monkeypatch.setattr(auxiliary_client, 'call_llm', judge)
    monkeypatch.setattr(auxiliary_client, 'resolve_provider_client', lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cli_mod, 'HermesCLI', WorkerCLI)
    monkeypatch.setattr(cli_mod.atexit, 'register', lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_mod, '_finalize_single_query', lambda cli: None)
    monkeypatch.setattr(cli_mod, '_single_query_finalize_attempted_session_ids', set())
    monkeypatch.setattr(cli_mod, '_cleanup_done', False)
    handlers = {number: signal.getsignal(number) for number in
                (signal.SIGINT, signal.SIGTERM, getattr(signal, 'SIGHUP', None)) if number is not None}
    seen.exit_code = None
    try:
        with pytest.raises(SystemExit) as left:
            cli_mod.main(query=option(argv, '-q'), quiet='-Q' in argv, toolsets=option(argv, '--toolsets'),
                         model=option(argv, '-m'), provider=option(argv, '--provider'),
                         reasoning=option(argv, '--reasoning'))
        seen.exit_code = left.value.code
    except Stopped:
        seen.stopped = True
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        for key in WORKER_ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in place.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    seen.calls = provider.calls[first_call:]
    return seen
