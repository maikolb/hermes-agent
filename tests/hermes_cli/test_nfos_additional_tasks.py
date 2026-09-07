"""Real SQLite/CLI contract for media-derived work handed to the Principal."""
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d


SOURCE = {'platform': 'telegram', 'chat_id': '-1001', 'thread_id': '8', 'message_id': '42'}
TEXT = 'Audite o total; no áudio há outros dois pedidos independentes.'
MEDIA = [{'original': '/cache/original.ogg', 'mime_type': 'audio/ogg', 'file_id': 'telegram-file-1'},
         {'original': '/cache/original.jpg', 'mime_type': 'image/jpeg', 'file_id': 'telegram-file-2'}]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid = d.receive_request(conn, source=SOURCE, text=TEXT,
            project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}, attachments=MEDIA)
        request = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
    return tmp_path/'kanban.db', task, rid


def proposal():
    return {'primary_task': 'Auditar o total.', 'tasks': [
        {'key': 'audio:00:42', 'text': 'Conferir duplicidades.', 'source_ref': 'original.ogg 00:42-00:57'},
        {'key': 'audio:01:10', 'text': 'Relatar pedidos sem responsável.', 'source_ref': 'original.ogg 01:10-01:24'},
    ]}


def propose(conn, task, payload=None):
    return d.ask_principal(conn, task.id, task.current_run_id, kind='additional_tasks',
        question='Despachar as tarefas adicionais identificadas no áudio?', context=payload or proposal())


def requests(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM nfos_requests ORDER BY id')]


def decide(conn, decision, **kwargs):
    d.resolve_decision(conn, decision, action='continue', answer='Itens independentes conferidos.',
                       author='Principal', **kwargs)


def test_worker_proposal_is_durable_and_only_principal_enqueues_children(board):
    path, task, rid = board
    with kb.connect_closing(path) as conn:
        decision = propose(conn, task)
        assert len(requests(conn)) == 1
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    with kb.connect_closing(path) as conn:
        assert d.pending_decisions(conn)[0]['id'] == decision
        decide(conn, decision)
        assert len(requests(conn)) == 3
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        original = d.get_request(conn, rid)
        assert json.loads(original['payload'])['text'] == TEXT
        assert json.loads(original['source_key'])[-1] == '0'
        for child in requests(conn):
            if child['id'] == rid:
                continue
            payload = json.loads(child['payload'])
            assert payload['source'] == SOURCE
            assert payload['attachments'] == MEDIA
            assert payload['project']['board'] == 'pilot'
            assert payload['origin']['request_id'] == rid
            assert payload['origin']['task_id'] == task.id
            assert payload['origin']['original_text'] == TEXT
            assert payload['origin']['decision_id'] == decision
            assert json.loads(child['source_key'])[-1] != '0'


def test_dispatch_crash_rolls_back_all_children_resolution_and_events(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        decision = propose(conn, task)
        before = conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
        conn.execute("CREATE TRIGGER fail_second_child BEFORE INSERT ON nfos_requests "
                     "WHEN json_extract(NEW.payload,'$.text')='Relatar pedidos sem responsável.' "
                     "BEGIN SELECT RAISE(ABORT,'power-loss-between-items'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match='power-loss-between-items'):
            decide(conn, decision)
        assert len(requests(conn)) == 1
        assert d.get_decision(conn, decision)['status'] == 'pending'
        assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0] == before
        conn.execute('DROP TRIGGER fail_second_child'); conn.commit()
        decide(conn, decision)
        assert len(requests(conn)) == 3


def test_lost_response_and_concurrent_principal_retries_do_not_duplicate(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        decision = propose(conn, task)
    def finish(_):
        with kb.connect_closing(path) as conn:
            decide(conn, decision)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, range(2)))
    with kb.connect_closing(path) as conn:
        assert propose(conn, task) == decision
        decide(conn, decision)
        assert len(requests(conn)) == 3
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_additional_tasks_dispatched'").fetchone()[0] == 1


def test_principal_can_refine_proposal_and_keeps_submitted_history(board):
    path, task, rid = board
    with kb.connect_closing(path) as conn:
        decision = propose(conn, task)
        revised = proposal()
        revised['tasks'] = [dict(revised['tasks'][0], text='Conferir duplicidades apenas no período solicitado.')]
        decide(conn, decision, proposal=revised)
        children = [r for r in requests(conn) if r['id'] != rid]
        assert len(children) == 1
        assert json.loads(children[0]['payload'])['text'] == revised['tasks'][0]['text']
        context = json.loads(d.get_decision(conn, decision)['context'])
        assert context['tasks'] == proposal()['tasks']
        assert context['dispatch_proposal'] == revised


@pytest.mark.parametrize('bad', ['missing_key', 'duplicate_key', 'missing_text', 'original_part'])
def test_ambiguous_proposal_returns_changes_without_dropping_the_proposal(board, bad):
    path, task, _ = board
    payload = proposal()
    if bad == 'missing_key': del payload['tasks'][0]['key']
    if bad == 'duplicate_key': payload['tasks'][1]['key'] = payload['tasks'][0]['key']
    if bad == 'missing_text': payload['tasks'][0]['text'] = ''
    if bad == 'original_part': payload['tasks'][0]['key'] = '0'
    with kb.connect_closing(path) as conn:
        decision = propose(conn, task, payload)
        decide(conn, decision)
        row = d.get_decision(conn, decision)
        assert row['action'] == 'changes'
        assert json.loads(row['context'])['tasks'] == payload['tasks']
        assert json.loads(row['context'])['dispatch_errors']
        assert len(requests(conn)) == 1


def test_overlapping_batches_reuse_stable_item_and_do_not_repeat_card(board):
    path, task, rid = board
    with kb.connect_closing(path) as conn:
        decide(conn, propose(conn, task))
        more = proposal()
        more['tasks'].append({'key': 'photo:caption:2', 'text': 'Auditar o dado indicado na imagem.', 'source_ref': 'original.jpg legenda 2'})
        decide(conn, propose(conn, task, more))
        assert len(requests(conn)) == 4
        assert json.loads(d.get_request(conn, rid)['payload'])['text'] == TEXT
        request = d.reserve_request(conn, capacity=2)
        child = d.bootstrap_card(conn, request['id'], request['claim_token'], pid=os.getpid())
        assert child.id != task.id
        assert child.created_by == 'worker:default'
        assert 'Original request lineage:' in child.body
        assert rid in child.body
        assert d.bootstrap_card(conn, request['id'], request['claim_token'], pid=os.getpid()).id == child.id
        assert d.reserve_request(conn, capacity=2) is None


def test_same_stable_key_with_different_content_returns_changes_atomically(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        decide(conn, propose(conn, task))
        altered = proposal()
        altered['tasks'][0]['text'] = 'Outra tarefa que não é a extração anterior.'
        decision = propose(conn, task, altered)
        decide(conn, decision)
        assert d.get_decision(conn, decision)['action'] == 'changes'
        assert len(requests(conn)) == 3


def test_child_rereading_same_media_reuses_original_lineage_item(board):
    path, task, rid = board
    with kb.connect_closing(path) as conn:
        decide(conn, propose(conn, task))
        request = d.reserve_request(conn, capacity=2)
        child = d.bootstrap_card(conn, request['id'], request['claim_token'], pid=os.getpid())
        # The child sees all preserved originals but must not dispatch siblings
        # again, even if it sends their stable source locations to the Principal.
        current_key = json.loads(request['payload'])['origin']['item_key']
        sibling = next(item for item in proposal()['tasks'] if item['key'] != current_key)
        decision = propose(conn, child, {'primary_task': child.title, 'tasks': [sibling]})
        decide(conn, decision)
        assert len(requests(conn)) == 3
        assert d.get_decision(conn, decision)['action'] == 'continue'
        assert json.loads(d.get_request(conn, rid)['payload'])['text'] == TEXT


def test_recovery_target_is_not_inherited_by_new_worker_cards(board):
    path, task, rid = board
    with kb.connect_closing(path) as conn:
        payload = json.loads(d.get_request(conn, rid)['payload'])
        payload['project']['existing_task_id'] = task.id
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), rid)); conn.commit()
        decide(conn, propose(conn, task))
        request = d.reserve_request(conn, capacity=2)
        assert 'existing_task_id' not in json.loads(request['payload'])['project']
        child = d.bootstrap_card(conn, request['id'], request['claim_token'], pid=os.getpid())
        assert child.id != task.id


def test_proposal_requires_current_worker_but_analysis_and_simple_specs_still_work(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        with pytest.raises(d.OwnershipConflict):
            d.ask_principal(conn, task.id, task.current_run_id + 1, kind='additional_tasks', question='Split?', context=proposal())
        decision = propose(conn, task)
        d.advance(conn, task.id, task.current_run_id, 'analysis', next_action='Read the original audio')
        d.save_spec(conn, task.id, task.current_run_id,
            {'goal': 'Auditar o total', 'criteria': [{'id': 'AC1', 'text': 'Total conferido'}],
             'steps': ['Conferir total'], 'delivery_type': 'report'},
            author='Claude TL', evidence={'session': 'actual-test-TL-transcript'})
        with pytest.raises(d.WorkflowError, match='boundaries'):
            d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Implement primary task')
        d.resolve_decision(conn, decision, action='changes', answer='First item is part of the same task; revise.', author='Principal')
        assert len(requests(conn)) == 1
        revised = {'primary_task': 'Auditar o total e suas fontes relacionadas.', 'tasks': []}
        decide(conn, propose(conn, task, revised))
        d.save_spec(conn, task.id, task.current_run_id,
            {'goal': revised['primary_task'], 'criteria': [{'id': 'AC1', 'text': 'Total e fontes conferidos'}],
             'steps': ['Conferir total e fontes'], 'delivery_type': 'report'},
            author='Claude TL', evidence={'session': 'revised-test-TL-transcript'})
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Implement primary task')
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id


def test_cli_worker_cannot_skip_principal_with_receive(board, tmp_path, monkeypatch):
    path, task, _ = board
    payload = tmp_path/'request.json'
    payload.write_text(json.dumps({'source': SOURCE, 'text': 'Hidden worker dispatch', 'part': 'hidden',
        'project': {'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}}), encoding='utf-8')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'receive', '--input', str(payload)])
    with pytest.raises(d.WorkflowError, match='Principal'):
        d.main()
    with kb.connect_closing(path) as conn:
        assert len(requests(conn)) == 1


def test_human_answer_returns_split_to_principal_instead_of_faking_dispatch(board):
    path, _, _ = board
    child = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    child.communicate(timeout=15)
    assert child.returncode == 0
    with kb.connect_closing(path) as conn:
        rid = d.receive_request(conn, source={**SOURCE, 'message_id': '43'}, text=TEXT,
            project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}, attachments=MEDIA)
        request = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, request['claim_token'], pid=child.pid)
        decision = propose(conn, task)
        d.resolve_decision(conn, decision, action='human', answer='Qual período se aplica ao segundo pedido?', author='Principal')
        kb.block_task(conn, task.id, reason='Qual período?', kind='needs_input', expected_run_id=task.current_run_id)
        assert d.resume_after_answer(conn, task.id, answer='O período descrito no áudio.',
            source={**SOURCE, 'message_id': '44'})
        pending = d.get_decision(conn, decision)
        assert pending['status'] == 'pending'
        assert pending['action'] is None
        assert json.loads(pending['context'])['human_reply']['source']['message_id'] == '44'
        assert len(requests(conn)) == 2
        assert kb.get_task(conn, task.id).status == 'ready'
        decide(conn, decision)
        assert len(requests(conn)) == 4


def test_changed_primary_requires_new_spec_even_after_implementation_started(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        decide(conn, propose(conn, task))
        spec = {'goal': proposal()['primary_task'], 'criteria': [{'id': 'AC1', 'text': 'Total conferido'}],
                'steps': ['Conferir total'], 'delivery_type': 'report'}
        d.save_spec(conn, task.id, task.current_run_id, spec, author='Claude TL', evidence={'session': 'tl-1'})
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Conferir o total')
        evidence = path.parent/'scope-evidence.txt'
        evidence.write_text('Verified total under the original scope.', encoding='utf-8')
        d.save_report(conn, task.id, task.current_run_id,
            {'summary': 'Original scope verified', 'artifacts': [str(evidence)],
             'criteria': [{'id': 'AC1', 'status': 'PASS', 'evidence': [str(evidence)]}]})
        review = d.ask_principal(conn, task.id, task.current_run_id, kind='review', question='Review original report?', context={})
        d.resolve_decision(conn, review, action='approve', answer='Original report verified.', author='Principal')
        assert d.completion_ready(conn, task.id)
        changed = {'primary_task': 'Conferir o total somente do período solicitado.', 'tasks': []}
        decide(conn, propose(conn, task, changed))
        assert not d.completion_ready(conn, task.id)
        with pytest.raises(d.WorkflowError, match='newer spec'):
            d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Continue with old spec')
        with pytest.raises(d.WorkflowError, match='newer spec'):
            d.begin_effect(conn, task.id, task.current_run_id, operation='pr', target='existing destination', candidate='old-candidate')
        d.advance(conn, task.id, task.current_run_id, 'analysis', next_action='Read additional context',
                  state={'task_partition': {}})
        with pytest.raises(d.WorkflowError, match='newer spec'):
            d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Try stale saved state')
        # A later split with the same primary cannot erase the stale-spec mark.
        later = dict(changed, tasks=[{'key': 'message:3', 'text': 'Relatar outra contagem.', 'source_ref': 'message item 3'}])
        decide(conn, propose(conn, task, later))
        with pytest.raises(d.WorkflowError, match='newer spec'):
            d.advance(conn, task.id, task.current_run_id, 'report', next_action='Report against old criteria')
        d.save_spec(conn, task.id, task.current_run_id, dict(spec, goal=changed['primary_task']),
                    author='Claude TL', evidence={'session': 'tl-2'})
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Use the revised scope')
        assert d.get_spec(conn, task.id)['revision'] == 2


def test_more_items_preserving_primary_do_not_require_another_spec(board):
    path, task, _ = board
    with kb.connect_closing(path) as conn:
        decide(conn, propose(conn, task))
        d.save_spec(conn, task.id, task.current_run_id,
            {'goal': proposal()['primary_task'], 'criteria': [{'id': 'AC1', 'text': 'Total conferido'}],
             'steps': ['Conferir total'], 'delivery_type': 'report'}, author='Claude TL', evidence={'session': 'tl-1'})
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Conferir total')
        later = proposal()
        later['tasks'].append({'key': 'message:3', 'text': 'Relatar outra contagem.', 'source_ref': 'message item 3'})
        decide(conn, propose(conn, task, later))
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Continue same scope')
        assert d.get_spec(conn, task.id)['revision'] == 1


def test_real_cli_split_decision_dispatch_and_show_preserve_request(board, tmp_path, monkeypatch, capsys):
    path, task, rid = board
    payload = tmp_path/'proposal.json'
    payload.write_text(json.dumps({'question': 'Desdobrar os pedidos?', **proposal()}), encoding='utf-8')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'ask', '--kind', 'additional_tasks', '--input', str(payload)])
    d.main()
    decision = json.loads(capsys.readouterr().out)['decision_id']
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    payload.write_text(json.dumps({'answer': 'Os itens estão dentro do pedido.'}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'decide', '--decision', decision,
        '--resolution', 'continue', '--input', str(payload)])
    d.main(); capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', ['nfos_delivery', 'show', '--task', task.id])
    d.main()
    shown = json.loads(capsys.readouterr().out)
    assert shown['request']['id'] == rid
    assert json.loads(shown['request']['payload'])['attachments'] == MEDIA
    with kb.connect_closing(path) as conn:
        assert len(requests(conn)) == 3
