"""Shared pieces of the quota-wall suites (test_nfos_executor_quota_wall, test_nfos_principal_quota_wall,
test_nfos_route_walls).

Nothing here reaches a provider. The board is a real SQLite board in a temporary home, the dispatcher runs its real
tick, and a worker is a real child process whose exit status the test decides, so the dispatcher reaps and classifies
a real wait status. The clock is the only thing moved by hand.
"""
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery

EXECUTOR_MODEL = 'deepseek-v4.1-flash'
EXECUTOR_PROVIDER = 'opencode-go'

# Names and non-secret values read from the hermes-project-factory profile on 2026-10-12: the executor route, no
# fallback provider, and the Principal on another provider.
PROFILE_CONFIG = {
    'model': {'default': 'gpt-6-astra', 'provider': 'openai-codex'},
    'fallback_providers': [],
    'kanban': {
        'agent_wake_on_events': True,
        'delivery': {
            'worker_model': EXECUTOR_MODEL, 'worker_provider': EXECUTOR_PROVIDER, 'worker_reasoning_effort': 'max',
            'worker_escalation': True, 'principal_validation': False, 'result_review': True,
        },
    },
}

# Variables the dispatcher gives its worker; a test that runs the worker in this process sets them and nothing else.
WORKER_ENV = ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKSPACE', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK',
              'HERMES_KANBAN_GOAL_MODE', 'HERMES_KANBAN_GOAL_MAX_TURNS', 'HERMES_KANBAN_BOARD',
              'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_SESSION_SOURCE', 'HERMES_PROFILE', 'HERMES_KANBAN_ROUTE')
# Where the dispatcher puts its worker: the home of the profile the card is launched as (its config and its
# credentials) and the board's database. This process has its own value of each, put back when the worker leaves.
WORKER_PLACE = ('HERMES_HOME', 'HERMES_KANBAN_DB')
# Variables the single-query path writes into the process on its own.
PROCESS_ENV = ('HERMES_INTERACTIVE', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_SESSION_ID')

SOURCE = {'platform': 'telegram', 'chat_id': '-1000', 'thread_id': '8', 'message_id': '123', 'user_id': 'owner',
          'chat_type': 'group'}
# What the Telegram group adapter hands over: one attribution line, then the message.
REQUEST = '[Japa|1]\nCorrija o total da tela de pedidos.'


def clear_env(monkeypatch, keys):
    """Unset now and unset again at teardown, including a variable the code under test sets on its own."""
    for key in keys:
        monkeypatch.setenv(key, '')
        monkeypatch.delenv(key)


class ProviderRateLimit(Exception):
    """What a provider answers on its quota wall."""
    status_code = 429

    def __str__(self):
        return 'Error code: 429 - Rate limit exceeded.'


class GoUsageLimit(ProviderRateLimit):
    """The opencode-go weekly wall, as the profile's log has it on 2026-10-10: this body (the workspace id is not the
    real one) and a Retry-After longer than the client's 600 s cap. The classifier reads it as a throttle; the name
    of the limit and the distance of the reset say it is a usage window."""
    body = {'type': 'error', 'error': {'type': 'GoUsageLimitError', 'message': 'Go usage limit exceeded'},
            'metadata': {'workspace': 'wrk_quota_wall_suite', 'limitName': 'weekly'}}
    response = SimpleNamespace(status_code=429, headers={'retry-after': '86400'})

    def __str__(self):
        return f'Error code: 429 - {self.body}'


class GoUsageLimitNoReset(GoUsageLimit):
    """The same body with no reset signal: the classifier reads it as billing and the client does not retry."""
    response = SimpleNamespace(status_code=429, headers={})


class GoUsageLimitFromSDK(ProviderRateLimit):
    """The same wall as the OpenAI SDK hands it over: `.body` keeps only the inner `error` object, and the whole
    answer, where `metadata.limitName` is, stays in the HTTP response. No Retry-After."""
    body = GoUsageLimit.body['error']
    response = SimpleNamespace(status_code=429, headers={}, json=lambda: GoUsageLimit.body)

    def __str__(self):
        return f'Error code: 429 - {GoUsageLimit.body}'


class CodexUsageLimit(ProviderRateLimit):
    """A Codex usage window: no limit name, the reset time in the body (the four weekly windows read in the
    profile's log on 2026-10-12 reopen between the 15th and the 17th)."""
    response = SimpleNamespace(status_code=429, headers={})

    def __init__(self, resets_in=4 * 86400):
        super().__init__()
        self.body = {'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                               'plan_type': 'pro', 'resets_at': int(time.time()) + resets_in,
                               'resets_in_seconds': resets_in}}

    def __str__(self):
        return f'Error code: 429 - {self.body}'


class ShortThrottle(ProviderRateLimit):
    """A request-rate throttle that resets inside the client's own wait."""
    response = SimpleNamespace(status_code=429, headers={'retry-after': '30'})


def reply(text, usage=None):
    """What a provider answers when it has quota: plain text, no tool call."""
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')], model=EXECUTOR_MODEL,
                           usage=usage)


def metered_reply(text):
    """The same answer with the token count a real provider sends, which is what the runtime counts as a call the
    provider answered."""
    return reply(text, usage=SimpleNamespace(prompt_tokens=120, completion_tokens=12, total_tokens=132))


class SkippedWaits:
    """`time` for the retry loop only: its backoff advances this clock instead of sleeping."""

    def __init__(self):
        self._time = time
        self.waited = 0.0

    def __getattr__(self, name):
        return getattr(self._time, name)

    def time(self):
        return self._time.time() + self.waited

    def sleep(self, seconds):
        self.waited += seconds


def agent_on_quota_wall(monkeypatch, *, provider, model, answer=None):
    """A real AIAgent with the profile's fallback chain whose provider answers every call with 429, or with what
    `answer(seen)` returns or raises when a test scripts the provider.
    Returns the agent and what it did: provider calls, refusals, turns, results, fallback switches, other clients
    asked for."""
    from agent import auxiliary_client, conversation_loop
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from run_agent import AIAgent

    clear_env(monkeypatch, PROCESS_ENV)
    seen = SimpleNamespace(provider_calls=0, refused=0, turns=0, results=[], fallback=[], reserve=[])
    with (patch('run_agent.get_tool_definitions', return_value=[]),
          patch('run_agent.check_toolset_requirements', return_value={}),
          patch('run_agent.OpenAI')):
        # The key is never used: `wall` answers every call.
        agent = AIAgent(api_key='unused-in-this-suite', base_url='https://quota-wall.invalid/v1', provider=provider,
                        model=model, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        fallback_model=get_fallback_chain(load_config()))
    agent.client = MagicMock()
    seen.route = (agent.provider, agent.model, str(agent.base_url))

    def wall(*args, **kwargs):
        seen.provider_calls += 1
        try:
            if answer is None:
                raise ProviderRateLimit()
            return answer(seen)
        except ProviderRateLimit:
            seen.refused += 1
            raise

    agent._interruptible_api_call = wall
    agent._interruptible_streaming_api_call = wall
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
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

    def reserve(provider=None, *args, **kwargs):
        seen.reserve.append(provider)
        return None, None

    agent.run_conversation, agent._try_activate_fallback = turn, fallback
    monkeypatch.setattr(auxiliary_client, 'resolve_provider_client', reserve)
    seen.waits = SkippedWaits()
    monkeypatch.setattr(conversation_loop, 'time', seen.waits)
    return agent, seen


def option(argv, flag):
    """The value of the last `flag` (the worker argv starts with `python -m hermes_cli.main`)."""
    return argv[len(argv) - argv[::-1].index(flag)] if flag in argv else None


def _is_worker_launch(cmd):
    return isinstance(cmd, (list, tuple)) and 'chat' in cmd and '-q' in cmd


class Launcher:
    """Replaces only the program of a worker launch: the argv and environment are the runtime's, the child is a
    real process that waits for the exit code the test writes to it."""

    def __init__(self):
        self.popen = subprocess.Popen
        self.launches = []
        self.processes = {}
        self.exited = []
        self.reuse = None

    def __call__(self, cmd, *args, **kwargs):
        if not _is_worker_launch(cmd):
            return self.popen(cmd, *args, **kwargs)
        self.launches.append((list(cmd), dict(kwargs.get('env') or {})))
        process, self.reuse = (self.reuse or self.start()), None
        return process

    def start(self):
        process = self.popen([sys.executable, '-c', 'import sys; sys.exit(int(sys.stdin.readline() or 1))'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes[process.pid] = process
        return process

    def exit(self, pid, code):
        """The worker leaves with this code. The dispatcher's own reap collects the status on its next tick."""
        process = self.processes.pop(pid)
        # Kept referenced: a collected Popen polls its child, and the status must stay for the dispatcher to reap.
        self.exited.append(process)
        process.stdin.write(f'{code}\n'.encode())
        process.stdin.close()
        deadline = time.monotonic() + 30
        while kb._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not kb._pid_alive(pid), 'the stand-in worker did not exit'

    def close(self):
        for process in self.processes.values():
            try:
                process.stdin.close()
                process.kill()
                process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        for process in self.exited:
            process.poll()  # already reaped by the dispatcher: this only settles the Popen object
        self.processes.clear()
        self.exited.clear()


class Clock:
    """`time.time` for the whole process, moved only by the test."""

    def __init__(self, monkeypatch):
        self.now = float(int(time.time()))
        monkeypatch.setattr(time, 'time', lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def quota_board(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.delenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', raising=False)
    clear_env(monkeypatch, WORKER_ENV + PROCESS_ENV)
    (home / 'config.yaml').write_text(yaml.safe_dump(PROFILE_CONFIG), encoding='utf-8')
    # A profile's state.db always has its session tables. Without this the first test of a process gets a state.db
    # holding only what another store created there, and a relaunch that looks its session up fails to spawn.
    from hermes_state import SessionDB
    SessionDB(db_path=home / 'state.db').close()
    monkeypatch.setattr(kb, '_retag_legacy_worker_sessions', lambda path: None)
    monkeypatch.setattr(kb, '_resolve_worker_cli_toolsets', lambda home: ['terminal'])
    monkeypatch.setattr(kb, '_memory_pressure_level', lambda: 'normal')
    return home


@pytest.fixture
def launcher(monkeypatch):
    stand_in = Launcher()
    monkeypatch.setattr(subprocess, 'Popen', stand_in)
    yield stand_in
    stand_in.close()


def enrol(conn, launcher, *, text=REQUEST, message_id='123'):
    """Request, card and first executor launch, in the order `nfos_runtime.bootstrap` does them."""
    request_id = delivery.receive_request(conn, source=dict(SOURCE, message_id=message_id), text=text,
                                          project={'profile': 'default', 'delivery_type': 'report'})
    reservation = delivery.reserve_request(conn, capacity=2)
    assert reservation['id'] == request_id
    worker = launcher.start()
    task = delivery.bootstrap_card(conn, request_id, reservation['claim_token'], pid=worker.pid)
    workspace = kb.resolve_workspace(task)
    kb.set_workspace_path(conn, task.id, str(workspace))
    task = kb.get_task(conn, task.id)
    launcher.reuse = worker
    assert kb._default_spawn(task, str(workspace)) == worker.pid
    return kb.get_task(conn, task.id)


def assert_executor_route(argv):
    """The launch names the executor's own provider and model, and nothing of the reserve."""
    launch = argv[:argv.index('chat')]
    assert option(launch, '-m') == EXECUTOR_MODEL
    assert option(launch, '--provider') == EXECUTOR_PROVIDER
    assert not [part for part in launch if 'codex' in part.lower() or part.lower().startswith('gpt-')]


class RateLimitError(Exception):
    """The goal judge shares the executor's provider. Same class name as on the boards in September."""


def run_executor(monkeypatch, argv, env, answer=None, judge_reply=None):
    """The executor child, run in this process: the real quiet single-query path of `cli.main`, driving a real
    AIAgent whose provider answers every call with 429 (or as `answer` scripts it). The goal judge fails like the
    provider unless `judge_reply` gives its verdict. Returns what the process did and the code it left with."""
    import cli as cli_mod
    from agent import auxiliary_client

    place = {key: os.environ.get(key) for key in WORKER_PLACE}
    for key in WORKER_ENV + WORKER_PLACE:
        if key in env:
            monkeypatch.setenv(key, env[key])
    agent, seen = agent_on_quota_wall(monkeypatch, provider=option(argv, '--provider'), model=option(argv, '-m'),
                                      answer=answer)
    seen.agent, seen.judge_calls = agent, 0

    def judge(*args, **kwargs):
        seen.judge_calls += 1
        if judge_reply is None:
            raise RateLimitError('Error code: 429 - Rate limit exceeded.')
        return reply(judge_reply)

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
        for key, value in place.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    seen.exit_code = left.value.code
    return seen
