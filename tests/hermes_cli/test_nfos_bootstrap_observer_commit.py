"""Observers see the whole committed card and never hold its writer open."""
import os
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import lifecycle
from hermes_cli import nfos_delivery as d


@pytest.fixture
def reserved_request(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn,
            source={'platform': 'telegram', 'chat_id': '1', 'thread_id': '2', 'message_id': 'observer'},
            text='Read existing report', project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'})
        reservation = d.reserve_request(conn, capacity=2)
        yield conn, rid, reservation['claim_token'], tmp_path / 'kanban.db'


def test_observer_sees_committed_complete_card_and_another_writer_can_run(reserved_request, monkeypatch):
    conn, rid, token, path = reserved_request
    observed = []

    def observer(event, **fields):
        if event != 'kanban_task_claimed':
            return
        item = {'transaction_open': conn.in_transaction}
        observed.append(item)
        with sqlite3.connect(path, timeout=.1, isolation_level=None) as other:
            item['attached'] = other.execute('SELECT task_id FROM nfos_requests WHERE id=?', (rid,)).fetchone()[0]
            item['workflow'] = other.execute('SELECT count(*) FROM nfos_workflows WHERE request_id=?', (rid,)).fetchone()[0]
            item['event'] = other.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_worker_created_card'", (fields['task_id'],)).fetchone()[0]
            other.execute('BEGIN IMMEDIATE')
            other.execute('UPDATE tasks SET title=title WHERE id=?', (fields['task_id'],))
            other.execute('COMMIT')
            item['other_writer_committed'] = True

    monkeypatch.setattr(lifecycle, 'invoke_hook', observer)
    task = d.bootstrap_card(conn, rid, token, pid=os.getpid())
    assert observed == [{'transaction_open': False, 'attached': task.id, 'workflow': 1,
                         'event': 1, 'other_writer_committed': True}]


def test_rolled_back_bootstrap_does_not_emit_claimed_observer(reserved_request, monkeypatch):
    conn, rid, token, _ = reserved_request
    observed = []
    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda event, **fields: observed.append(event))

    def fail(*args, **kwargs):
        raise RuntimeError('fault before outer commit')

    monkeypatch.setattr(d, '_event', fail)
    with pytest.raises(RuntimeError, match='fault before outer commit'):
        d.bootstrap_card(conn, rid, token, pid=os.getpid())
    assert 'kanban_task_claimed' not in observed
    assert d.get_request(conn, rid)['task_id'] is None
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_bootstrap_retransmission_does_not_repeat_observer(reserved_request, monkeypatch):
    conn, rid, token, _ = reserved_request
    observed = []
    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda event, **fields: observed.append(event))
    first = d.bootstrap_card(conn, rid, token, pid=os.getpid())
    second = d.bootstrap_card(conn, rid, token, pid=os.getpid())
    assert second.id == first.id
    assert observed.count('kanban_task_claimed') == 1
