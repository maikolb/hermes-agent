"""Native user intake keeps worker selection and decision engine independent."""
import json
import os
import socket

import pytest
import yaml

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli import nfos_jev as engines, nfos_principal_review as review


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    config = {'deepseek_worker_trial': True, 'worker_model': 'gpt-5.6-luna',
              'worker_provider': 'openai-codex', 'worker_reasoning_effort': 'high',
              'decision_engines': {'allowed': ['jev', 'laya']},
              'laya': {'model_revision': 'a' * 40, 'source_revision': 'b' * 40}}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'delivery': config}}), encoding='utf-8')
    monkeypatch.setattr(review, 'settings', lambda: config)
    return tmp_path


def receive(conn, text, message='1', platform='telegram', **kwargs):
    return delivery.receive_request(conn,
        source={'platform': platform, 'chat_id': 'synthetic', 'thread_id': 'synthetic', 'message_id': message},
        text=text, project={'profile': 'default', 'delivery_type': 'report'}, **kwargs)


def claim(conn, rid):
    reservation = delivery.reserve_request(conn, capacity=10)
    assert reservation['id'] == rid
    return delivery.bootstrap_card(conn, rid, reservation['claim_token'], pid=os.getpid())


@pytest.mark.parametrize('tags,worker,engine', [
    ('', False, None), ('#deepseek', True, None), ('#jev', False, 'jev'),
    ('#laya', False, 'laya'), ('#deepseek #jev', True, 'jev'),
    ('#deepseek #laya', True, 'laya'), ('#Laya,', False, 'laya'),
    ('#layaish #jev_other #laya-other', False, None),
])
def test_native_intake_axes(configured, tags, worker, engine):
    text = 'Confira o resultado sintético. ' + tags
    with kb.connect_closing() as conn:
        rid = receive(conn, text)
        task = claim(conn, rid)
        payload = json.loads(delivery.get_request(conn, rid)['payload'])
        assert payload['text'] == text
        assert task.model_override == ('deepseek-v4.1-flash' if worker else None)
        selected = delivery.decision_engine_selection(conn, task.id)
        if engine is None:
            assert selected is None
        else:
            assert selected == payload['decision_engine_selection']
            assert selected['engine'] == engine
            assert selected['source'] == 'user_hashtag'
            assert selected['revision'] == engines._digest(selected['config'])
            assert engines.settings(conn, task.id)['engine'] == engine


@pytest.mark.parametrize('text', ['#jev #laya', '#LAYA, #JEV.'])
def test_conflict_is_explicit_and_atomic(configured, text):
    with kb.connect_closing() as conn:
        with pytest.raises(delivery.WorkflowError, match='Conflicting decision engines'):
            receive(conn, text)
        assert conn.execute('SELECT count(*) FROM nfos_requests').fetchone()[0] == 0


def test_request_only_not_attachments_or_non_native_platform(configured):
    with kb.connect_closing() as conn:
        native = claim(conn, receive(conn, 'Confira o relatório.', attachments=[{'text': '#laya'}]))
        external = claim(conn, receive(conn, '#laya', message='2', platform='fixture'))
        assert delivery.decision_engine_selection(conn, native.id) is None
        assert delivery.decision_engine_selection(conn, external.id) is None


def test_selection_survives_coordinator_reload_continuation_and_progress(configured):
    with kb.connect_closing() as conn:
        rid = receive(conn, '#deepseek #laya Audite o resultado sintético.', defer_to_principal=True)
        assert receive(conn, '#deepseek #laya Audite o resultado sintético.') == rid
        task = claim(conn, rid)
        selected = delivery.decision_engine_selection(conn, task.id)
        delivery.advance(conn, task.id, task.current_run_id, 'analysis', next_action='Synthetic read only',
                         state={'decision_engine_selection': {'engine': 'jev'}})
        assert delivery.decision_engine_selection(conn, task.id) == selected
        child_id = delivery.create_continuation(conn, task.id)['task_id']
        with kb.write_txn(conn):
            conn.execute('INSERT INTO nfos_workflows(task_id,request_id,updated_at) VALUES(?,?,0)', (child_id, rid))
        other = claim(conn, receive(conn, '#jev Outro relatório sintético.', message='2'))
        assert delivery.decision_engine_selection(conn, other.id)['engine'] == 'jev'
    with kb.connect_closing() as conn:
        restored = kb.get_task(conn, task.id)
        assert restored.model_override == 'deepseek-v4.1-flash'
        assert delivery.decision_engine_selection(conn, restored.id) == selected
        assert delivery.decision_engine_selection(conn, child_id) == selected
        assert engines.settings(conn, child_id)['engine'] == 'laya'


def test_duplicate_does_not_change_existing_selection_on_config_drift(configured):
    with kb.connect_closing() as conn:
        rid = receive(conn, '#laya Confira o relatório sintético.')
        task = claim(conn, rid)
        selected = delivery.decision_engine_selection(conn, task.id)
        config = yaml.safe_load((configured / 'config.yaml').read_text(encoding='utf-8'))
        config['kanban']['delivery']['laya']['model_revision'] = 'c' * 40
        (configured / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
        assert receive(conn, '#laya Confira o relatório sintético.') == rid
        assert delivery.decision_engine_selection(conn, task.id) == selected
        assert engines.settings(conn, task.id)['enabled'] is False
        assert engines.settings(conn, task.id)['error'] == 'selection_config_changed'


def test_interrupted_native_retry_preserves_both_axes(configured):
    with kb.connect_closing() as conn:
        rid = receive(conn, '#deepseek #laya Confira o relatório sintético.')
        reservation = delivery.reserve_request(conn, capacity=10)
        task = delivery.bootstrap_card(conn, rid, reservation['claim_token'], pid=2147483647)
        selected = delivery.decision_engine_selection(conn, task.id)
        dead_claim = socket.gethostname() + ':2147483647'
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET claim_lock=?,worker_pid=?,worker_started_at=NULL WHERE id=?',
                         (dead_claim, 2147483647, task.id))
            conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?', (2147483647, task.current_run_id))
        assert kb.recover_interrupted_task(conn, task.id, expected_run_id=task.current_run_id,
                                          expected_claim=dead_claim, expected_heartbeat=task.last_heartbeat_at)
        retry = kb.claim_task(conn, task.id)
        assert retry.current_run_id != task.current_run_id
        assert retry.model_override == 'deepseek-v4.1-flash'
        assert retry.provider_override == 'opencode-go'
        assert delivery.decision_engine_selection(conn, retry.id) == selected
        assert engines.settings(conn, retry.id)['engine'] == 'laya'


def test_allowlist_absence_preserves_selected_identity_without_enabling(configured):
    (configured / 'config.yaml').write_text('kanban:\n  delivery: {}\n', encoding='utf-8')
    with kb.connect_closing() as conn:
        task = claim(conn, receive(conn, '#laya Confira o relatório sintético.'))
        assert delivery.decision_engine_selection(conn, task.id)['engine'] == 'laya'
        assert engines.settings(conn, task.id)['enabled'] is False
