"""Shared pieces of the two quota-wall suites (test_nfos_executor_quota_wall, test_nfos_principal_quota_wall).

Nothing here reaches a provider. The board is a real SQLite board in a temporary home, the dispatcher runs its real
tick, and a worker is a real child process whose exit status the test decides, so the dispatcher reaps and classifies
a real wait status. The clock is the only thing moved by hand.
"""
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
              'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_SESSION_SOURCE', 'HERMES_PROFILE')
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


def agent_on_quota_wall(monkeypatch, *, provider, model):
    """A real AIAgent with the profile's fallback chain whose provider answers every call with 429.
    Returns the agent and what it did: provider calls, turns, results, fallback switches, other clients asked for."""
    from agent import auxiliary_client, conversation_loop
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from run_agent import AIAgent

    clear_env(monkeypatch, PROCESS_ENV)
    seen = SimpleNamespace(provider_calls=0, turns=0, results=[], fallback=[], reserve=[])
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
        raise ProviderRateLimit()

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
