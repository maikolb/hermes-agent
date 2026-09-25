"""The temporary Telegram flag pins only the requested worker, including retries."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review


@pytest.fixture
def trial(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    policy = {'deepseek_worker_trial': True, 'worker_model': 'gpt-5.6-luna',
              'worker_provider': 'openai-codex', 'worker_reasoning_effort': 'high'}
    monkeypatch.setattr(review, 'settings', lambda: policy)
    return policy


def receive(conn, text, message='1', platform='telegram', **kwargs):
    return delivery.receive_request(conn,
        source={'platform': platform, 'chat_id': 'test', 'thread_id': 'test', 'message_id': message},
        text=text, project={'profile': 'default', 'delivery_type': 'report'}, **kwargs)


def claim(conn, rid):
    reservation = delivery.reserve_request(conn, capacity=10)
    assert reservation['id'] == rid
    return delivery.bootstrap_card(conn, rid, reservation['claim_token'], pid=os.getpid())


@pytest.mark.parametrize('punctuation', ['', ',', '.', ':', ';', '!', '?', '…', ')', ']'])
def test_flag_preserves_request_and_pins_first_worker(trial, punctuation):
    original = '@hermes_nexafactory_bot #deepseek' + punctuation + ' Confira o relatório somente em HML.'
    with kb.connect_closing() as conn:
        rid = receive(conn, original)
        assert receive(conn, original) == rid
        payload = json.loads(delivery.get_request(conn, rid)['payload'])
        assert payload['text'] == original
        assert payload['project']['model'] == 'deepseek-v4.1-flash'
        task = claim(conn, rid)
        assert task.provider_override == 'opencode-go'
        assert review.worker_model_args(task) == [
            '-m', 'deepseek-v4.1-flash', '--provider', 'opencode-go', '--reasoning', 'max']
    assert trial['worker_model'] == 'gpt-5.6-luna'


@pytest.mark.parametrize('text,platform,enabled', [
    ('Confira o relatório.', 'telegram', True),
    ('#deepseekish Confira o relatório.', 'telegram', True),
    ('#deepseek_other Confira o relatório.', 'telegram', True),
    ('#deepseek123 Confira o relatório.', 'telegram', True),
    ('#deepseek-other Confira o relatório.', 'telegram', True),
    ('#deepseek Confira o relatório.', 'telegram', False),
    ('#deepseek Confira o relatório.', 'fixture', True),
])
def test_unmarked_or_disabled_requests_keep_default(trial, text, platform, enabled):
    trial['deepseek_worker_trial'] = enabled
    with kb.connect_closing() as conn:
        task = claim(conn, receive(conn, text, platform=platform))
        assert task.model_override is None
        assert review.worker_model_args(task) == [
            '-m', 'gpt-5.6-luna', '--provider', 'openai-codex', '--reasoning', 'high']


def test_coordinator_dispatch_keeps_original_flag_and_does_not_leak(trial):
    with kb.connect_closing() as conn:
        rid = receive(conn, '#DeepSeek Audite o resultado.', defer_to_principal=True)
        assert delivery.get_request(conn, rid)['status'] == 'coordinating'
        assert receive(conn, '#DeepSeek Audite o resultado.') == rid
        marked = claim(conn, rid)
        normal = claim(conn, receive(conn, 'Confira outro relatório.', message='2'))
        assert marked.model_override == 'deepseek-v4.1-flash'
        assert normal.model_override is None


def test_persisted_pin_reaches_spawn_and_continuation(trial, monkeypatch, tmp_path):
    with kb.connect_closing() as conn:
        task_id = claim(conn, receive(conn, '#deepseek, Audite o resultado somente em HML.')).id
    captured = []
    monkeypatch.setattr(kb, '_retag_legacy_worker_sessions', lambda path: None)
    monkeypatch.setattr(kb, '_resolve_worker_cli_toolsets', lambda path: [])
    monkeypatch.setattr(kb, '_worker_resume_context', lambda *a, **kw: (None, ''))
    monkeypatch.setattr('subprocess.Popen', lambda cmd, **kw: captured.append(cmd) or SimpleNamespace(pid=123))
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        kb._default_spawn(task, str(tmp_path))
        child = kb.get_task(conn, delivery.create_continuation(conn, task.id)['task_id'])
        assert child.model_override == task.model_override
        assert child.provider_override == task.provider_override
        assert child.reasoning_effort == task.reasoning_effort
    command = captured[0]
    assert command[command.index('--provider') + 1] == 'opencode-go'
    assert command[command.index('--reasoning') + 1] == 'max'
    assert 'deepseek-v4.1-flash' in command


@pytest.mark.parametrize('tag', ['#luna', '#LUNA,', '#luna.'])
def test_luna_opt_in_with_deepseek_default_survives_continuation(trial, tag):
    trial.update(worker_model='deepseek-v4.1-flash', worker_provider='opencode-go',
                 worker_reasoning_effort='max', worker_escalation=True)
    with kb.connect_closing() as conn:
        task = claim(conn, receive(conn, tag + ' Confira o relatório.'))
        assert review.worker_model_args(task, conn) == [
            '-m', 'gpt-5.6-luna', '--provider', 'openai-codex', '--reasoning', 'high']
        child = kb.get_task(conn, delivery.create_continuation(conn, task.id)['task_id'])
        assert review.worker_model_args(child, conn) == review.worker_model_args(task, conn)
        normal = claim(conn, receive(conn, 'Confira outro relatório.', message='2'))
        assert review.worker_model_args(normal, conn) == [
            '-m', 'deepseek-v4.1-flash', '--provider', 'opencode-go', '--reasoning', 'max']


@pytest.mark.parametrize('text,platform', [
    ('#lunaish Confira.', 'telegram'), ('#luna-other Confira.', 'telegram'),
    ('#luna_other Confira.', 'telegram'), ('#luna Confira.', 'fixture'),
])
def test_luna_requires_exact_native_tag(trial, text, platform):
    trial.update(worker_model='deepseek-v4.1-flash', worker_provider='opencode-go',
                 worker_reasoning_effort='max')
    with kb.connect_closing() as conn:
        task = claim(conn, receive(conn, text, platform=platform))
        assert task.model_override is None
        assert review.worker_model_args(task, conn)[1] == 'deepseek-v4.1-flash'


def test_deepseek_default_supersedes_pending_luna_escalation(trial):
    trial.update(worker_model='deepseek-v4.1-flash', worker_provider='opencode-go',
                 worker_reasoning_effort='max', worker_escalation=True)
    with kb.connect_closing() as conn:
        task = claim(conn, receive(conn, 'Retome o relatório.'))
        state = {'worker_escalation': {'model': 'gpt-6-luna',
                 'reasoning_effort': 'max', 'status': 'pending', 'source_run_id': 'old-run'}}
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',
                     (json.dumps(state), task.id))
        assert review.worker_model_args(task, conn) == [
            '-m', 'deepseek-v4.1-flash', '--provider', 'opencode-go', '--reasoning', 'max']
        review.confirm_worker_dispatch(conn, task.id, task.current_run_id,
                                       'deepseek-v4.1-flash', 'max')
        assert review.worker_escalation(conn, task.id)['model'] == 'deepseek-v4.1-flash'
        assert review.worker_escalation(conn, task.id)['status'] == 'applied'
