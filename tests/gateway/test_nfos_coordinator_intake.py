"""Coordinator inbox persists the real event without creating worker work."""
import asyncio
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_nfos_intake import setup
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery


@pytest.fixture
def coordinator(setup, monkeypatch):
    runner, event, adapter, root = setup
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    runner._adapter_profile_for_source = lambda source: 'default'
    adapter.handle_message = AsyncMock()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return runner, event, adapter, root


def _counts(db):
    with kb.connect_closing(db) as conn:
        return tuple(conn.execute('SELECT count(*) FROM '+table).fetchone()[0]
                     for table in ('nfos_requests', 'tasks'))


def _inbox(root, event):
    # Use a new connection: the gateway's event object is not the authority.
    with kb.connect_closing(root/'kanban.db') as conn:
        rows = conn.execute('SELECT * FROM nfos_requests').fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row['status'] == 'coordinating'
        assert row['task_id'] is None
        assert row['worker_pid'] is None
        payload = json.loads(row['payload'])
        assert payload['text'] == event.text
        assert payload['source']['message_id'] == event.message_id
        assert payload['coordination'].get('reply_to_message_id') == event.reply_to_message_id
        assert delivery.reserve_request(conn, capacity=2) is None
    return row, payload


async def _context(runner, event, adapter, root):
    # The watcher, using the persisted input, constructs the internal event.
    # Acceptance by this adapter alone intentionally does not fabricate ACK.
    await asyncio.wait_for(asyncio.gather(*list(runner._background_tasks)), 5)
    wake_event = adapter.handle_message.call_args.args[0]
    assert wake_event.internal is True
    context = wake_event.metadata['nfos_coordinator_intake']
    assert Path(context['db_path']).resolve() == (root/'kanban.db').resolve()
    request = context['request']
    assert request['source'] == {
        'platform': 'telegram', 'chat_id': '-1000', 'thread_id': '8',
        'message_id': '123', 'user_id': 'owner', 'chat_type': 'group',
        'profile': 'default', 'transport_profile': 'default',
    }
    assert request['project'] == {
        'enabled': True, 'profile': 'default', 'delivery_type': 'report',
        'workers': 2, 'board': 'default', 'project_id': '',
    }
    assert request['part'] == '0'
    assert request['text'] == event.text
    return context


def _note(runner, adapter):
    return runner._nfos_coordinator_intake_note(adapter.handle_message.call_args.args[0])


def _request_json_from_note(note):
    """Allow prose/fences around the exact machine-readable request JSON."""
    decoder = json.JSONDecoder()
    for offset, char in enumerate(note):
        if char != '{':
            continue
        try:
            value, _ = decoder.raw_decode(note[offset:])
        except ValueError:
            continue
        if isinstance(value, dict) and {'source', 'project', 'attachments'} <= value.keys():
            return value
    raise AssertionError('Coordinator note did not preserve its request as JSON')


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['quero um relatório', 'relate a versão'])
async def test_ambiguous_new_request_gets_context_without_automatic_work(coordinator, text):
    runner, event, adapter, root = coordinator
    event.text = text
    assert await runner._nfos_receive(event) is not None
    context = await _context(runner, event, adapter, root)
    assert context['request']['attachments'] == []
    assert _counts(root/'kanban.db') == (1, 0)
    _inbox(root, event)
    adapter._send_with_retry.assert_not_called()
    note = _note(runner, adapter)
    assert _request_json_from_note(note) == context['request']
    assert 'receive' in note and '--db' in note and '--input' in note
    assert str(root/'kanban.db') in note


@pytest.mark.asyncio
async def test_status_is_persisted_for_coordinator_without_worker_dispatch(coordinator):
    runner, event, adapter, root = coordinator
    event.text = 'Qual é o status?'
    assert await runner._nfos_receive(event) is not None
    context = await _context(runner, event, adapter, root)
    assert _request_json_from_note(_note(runner, adapter)) == context['request']
    assert _counts(root/'kanban.db') == (1, 0)
    row, payload = _inbox(root, event)
    assert await runner._nfos_receive(event) is not None
    assert _inbox(root, event)[0]['id'] == row['id']
    adapter._send_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_reply_preserves_identity_and_original_attachment_without_dispatch(coordinator):
    runner, event, adapter, root = coordinator
    event.text = 'Agora considere também este arquivo'
    event.reply_to_message_id = '122'
    original = root/'original.bin'
    original.write_bytes(b'original attachment bytes, not a transcript')
    event.media_urls = [str(original)]
    event.media_types = ['application/octet-stream']
    assert await runner._nfos_receive(event) is not None
    request = (await _context(runner, event, adapter, root))['request']
    assert len(request['attachments']) == 1
    preserved = Path(request['attachments'][0]['original'])
    assert preserved.resolve() != original.resolve()
    original.unlink()
    assert preserved.read_bytes() == b'original attachment bytes, not a transcript'
    assert event.reply_to_message_id == '122'
    assert _request_json_from_note(_note(runner, adapter)) == request
    assert _counts(root/'kanban.db') == (1, 0)
    _, stored = _inbox(root, event)
    assert stored['attachments'] == request['attachments']
    adapter._send_with_retry.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('excluded', ['internal', 'other_project'])
async def test_non_intake_events_never_receive_dispatch_context(coordinator, excluded):
    runner, event, adapter, root = coordinator
    event.text = 'quero um relatório'
    if excluded == 'internal':
        event.internal = True
    else:
        runner._resolve_project_context_for_message = lambda event, source: (
            SimpleNamespace(board_slug='other', is_management=False), None)
    assert await runner._nfos_receive(event) is None
    assert not (event.metadata or {}).get('nfos_coordinator_intake')
    assert not runner._nfos_coordinator_intake_note(event)
    assert _counts(root/'kanban.db') == (0, 0)
    adapter._send_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_internal_wake_preserves_context_without_a_second_intake(coordinator):
    from gateway.platforms.base import MessageEvent
    runner, event, adapter, root = coordinator
    event.text = 'quero um relatório'
    assert await runner._nfos_receive(event) is not None
    context = await _context(runner, event, adapter, root)
    internal = MessageEvent(text='Durable coordinator input', source=event.source,
        internal=True, metadata={'nfos_coordinator_intake':context})
    assert await runner._nfos_receive(internal) is None
    assert internal.metadata['nfos_coordinator_intake'] == context
    assert _request_json_from_note(runner._nfos_coordinator_intake_note(internal)) == context['request']
    assert _counts(root/'kanban.db') == (1, 0)


@pytest.mark.asyncio
async def test_busy_clear_reply_cannot_fall_through_to_enabled_legacy_dispatch(coordinator):
    runner, event, adapter, root = coordinator
    event.text = 'Crie um relatório com a informação que faltava'
    event.reply_to_message_id = '122'
    config = runner._kanban_parallel_dispatch_config(event.source)
    config['dispatch'] = {'parallel_by_default': True}
    runner._kanban_parallel_dispatch_assignee = lambda source, config: 'default'
    runner._kanban_parallel_queue_state = lambda *args: (False, 0)

    async def legacy_create(_event):
        # If the reply escapes to the old path, create a real card so this
        # regression catches both the wrong route and its persisted effect.
        with kb.connect_closing() as conn:
            task_id = kb.create_task(conn, title='Unexpected legacy reply card',
                                     assignee='default', requires_repo=False)
        return 'Created '+task_id

    runner._handle_kanban_command = AsyncMock(side_effect=legacy_create)
    handled = await runner._kanban_parallel_dispatch_busy_message(event, 'principal')
    runner._handle_kanban_command.assert_not_called()
    assert handled is True, 'Durable coordinator inbox must own the reply before RAM steering'
    assert _counts(root/'kanban.db') == (1, 0)
    _inbox(root, event)
    adapter._send_with_retry.assert_not_called()
    context = await _context(runner, event, adapter, root)
    assert _request_json_from_note(_note(runner, adapter)) == context['request']


@pytest.mark.asyncio
async def test_receive_cli_explicit_db_shares_automatic_intake_identity(coordinator, monkeypatch):
    runner, event, adapter, root = coordinator
    assert await runner._nfos_receive(event)
    canonical = root/'kanban.db'
    with kb.connect_closing(canonical) as conn:
        row = conn.execute('SELECT id,payload FROM nfos_requests').fetchone()
        request_id, payload = row['id'], json.loads(row['payload'])
    payload['part'] = '0'
    request_file = root/'request.json'
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    decoy = root/'wrong-session-board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(decoy))
    monkeypatch.setattr('sys.argv', ['nfos_delivery', 'receive', '--db', str(canonical),
                                    '--input', str(request_file)])
    for _ in range(2):
        output = io.StringIO()
        with redirect_stdout(output):
            delivery.main()
        assert json.loads(output.getvalue())['request_id'] == request_id
    assert _counts(canonical) == (1, 0)
    assert not decoy.exists(), 'CLI ignored the event board and wrote to its session default'


@pytest.mark.asyncio
async def test_principal_receive_promotes_existing_inbox_identity_only_once(coordinator, monkeypatch):
    runner, event, adapter, root = coordinator
    event.text = 'quero um relatório'
    assert await runner._nfos_receive(event) is not None
    row, stored = _inbox(root, event)
    request_file = root/'recognized-task.json'
    context = await _context(runner, event, adapter, root)
    request_file.write_text(json.dumps(context['request']), encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['nfos_delivery', 'receive', '--db', str(root/'kanban.db'),
                                    '--input', str(request_file)])
    for _ in range(2):
        output = io.StringIO()
        with redirect_stdout(output):
            delivery.main()
        assert json.loads(output.getvalue())['request_id'] == row['id']
    with kb.connect_closing(root/'kanban.db') as conn:
        promoted = delivery.get_request(conn, row['id'])
        assert promoted['status'] == 'pending'
        assert promoted['task_id'] is None
        assert json.loads(promoted['payload'])['source'] == stored['source']
    assert _counts(root/'kanban.db') == (1, 0)


@pytest.mark.asyncio
async def test_coordinator_checkpoint_contains_input_before_sidecar_is_consumed(coordinator):
    from agent.turn_checkpoint import initialize_agent_turn_checkpoint
    from gateway.wake import current_notify_receipt, notify_wake_accepted
    runner, event, adapter, root = coordinator
    event.text = 'quero um relatório'
    event.reply_to_message_id = '122'
    assert await runner._nfos_receive(event) is not None
    context = await _context(runner, event, adapter, root)
    internal = adapter.handle_message.call_args.args[0]
    receipt = internal.metadata['kanban_wake_delivery']
    assert not notify_wake_accepted(receipt)
    agent = SimpleNamespace(session_id='principal-original',
        _session_db=SimpleNamespace(db_path=root/'state.db'))
    token = current_notify_receipt.set(receipt)
    try:
        # Same early checkpoint as turn_context, before the later RAM sidecar
        # consumption or first model call. This is the actual wake's text.
        initialize_agent_turn_checkpoint(agent, turn_id='before-sidecar',
            user_content=internal.text, messages=[])
    finally:
        current_notify_receipt.reset(token)
    assert notify_wake_accepted(receipt)
    persisted = agent._turn_checkpoint_store.load('principal-original')
    text = persisted['active_user_turn']['content']
    assert _request_json_from_note(text) == context['request']
    assert context['db_path'] in text
    assert '122' in text
    _inbox(root, event)
