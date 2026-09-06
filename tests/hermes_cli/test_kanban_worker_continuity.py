"""A replacement process keeps the interrupted worker's durable session."""
import json
import os
import subprocess
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_state import SessionDB


def test_replacement_spawn_resumes_only_its_own_interrupted_session(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    monkeypatch.setattr(kb, '_retag_legacy_worker_sessions', lambda root: None)
    monkeypatch.setattr(kb, '_resolve_worker_cli_toolsets', lambda home: [])
    captured=[]
    monkeypatch.setattr('subprocess.Popen', lambda cmd, **kw: captured.append((cmd,kw)) or SimpleNamespace(pid=4321))
    with kb.connect_closing() as conn:
        tid=kb.create_task(conn,title='Finish accepted report',assignee='default',requires_repo=False)
        kb.recompute_ready(conn)
        task=kb.claim_task(conn,tid)
        assert task
        run=kb.get_task(conn,tid).current_run_id
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',(json.dumps({'worker_session_id':'worker-saved'}),run))
        conn.commit()
        kb.reclaim_task(conn,tid)
        task=kb.claim_task(conn,tid)
    db=SessionDB(db_path=tmp_path/'state.db')
    db.create_session(session_id='worker-saved',source='kanban')
    db.append_message(session_id='worker-saved',role='assistant',content='Evidence already collected: 42 items.')
    db.close()
    kb._default_spawn(task,str(tmp_path))
    cmd=captured[-1][0]
    assert cmd[cmd.index('--resume')+1]=='worker-saved'
    assert 'current card' in cmd[cmd.index('-q')+1].lower()
    with kb.connect_closing() as conn:
        conn.execute("UPDATE task_runs SET profile='another-profile' WHERE id=?",(run,))
        conn.commit()
    kb._default_spawn(task,str(tmp_path))
    assert '--resume' not in captured[-1][0]


@pytest.mark.parametrize('worker,verification_pending', [(False, False), (True, True)])
def test_gateway_delivery_and_worker_verification_keep_resume_semantics(tmp_path, worker, verification_pending):
    from agent.turn_checkpoint import TurnCheckpointStore
    store = TurnCheckpointStore(tmp_path/'checkpoints')
    messages = [{'role':'user', 'content':'Finish'}]
    routing = {'kanban_task_id':'t-saved', 'kanban_db':'board.db'} if worker else {'platform':'telegram'}
    store.start_turn('saved', 'original', 'Finish', messages, routing=routing)
    store.mark_deliverable('saved', 'Existing answer', verification_pending=verification_pending)
    restored = store.start_turn('saved', 'replacement', 'Finish', messages, routing=routing)
    assert restored['turn_id'] == 'original'
    assert restored['pending_deliverable']['content'] == 'Existing answer'


@pytest.mark.parametrize('phase', ['deliverable_composed', 'delivery_pending'])
def test_finished_worker_answer_starts_executable_turn_in_same_session(tmp_path, monkeypatch, phase):
    from agent.turn_checkpoint import initialize_agent_turn_checkpoint
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't-continuity')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.setenv('HERMES_SESSION_SOURCE', 'kanban')
    db = SessionDB(db_path=tmp_path/'state.db')
    db.create_session(session_id='saved-answer', source='kanban')
    db.append_message(session_id='saved-answer', role='user', content='Finish same card')
    first = SimpleNamespace(session_id='saved-answer', _session_db=db)
    initialize_agent_turn_checkpoint(first, turn_id='old-turn', user_content='Finish same card',
                                     messages=db.get_messages('saved-answer'))
    db.append_message(session_id='saved-answer', role='assistant', content='7 tests passed; API check pending')
    first._turn_checkpoint_store.mark_deliverable('saved-answer', '7 tests passed; API check pending',
                                                  verification_pending=False)
    first._turn_checkpoint_store.transition('saved-answer', phase=phase,
                                            next_action='finalize_delivery')
    replacement = SimpleNamespace(session_id='saved-answer', _session_db=db)
    state = initialize_agent_turn_checkpoint(replacement, turn_id='new-turn',
                user_content='Finish same card', messages=db.get_messages('saved-answer'))
    assert state['turn_id'] == 'new-turn'
    assert state['phase'] == 'turn_started'
    assert not state['recovery']['restored']
    assert replacement._restored_pending_deliverable is None
    assert db.get_messages('saved-answer')[-1]['content'] == '7 tests passed; API check pending'
    db.close()


def test_process_death_cannot_commit_card_without_its_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    monkeypatch.setenv('HERMES_SESSION_CHAT_ID', 'continuity-test')
    with kb.connect_closing():
        pass
    code = '''
import json, os
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt
kt.load_config=lambda: {}
original=kb.add_notify_sub
def interrupt(*args, **kwargs):
    os._exit(23)
if os.environ.get('INTERRUPT_SUBSCRIPTION') == '1':
    kb.add_notify_sub=interrupt
print(kt._handle_create(dict(title='Accepted once',assignee='default',requires_repo=False,
                            idempotency_key='continuity-test')))
'''
    child=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,
                         env=dict(os.environ,INTERRUPT_SUBSCRIPTION='1'),
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert child.returncode == 23, child.stderr
    with kb.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    for _ in range(2):
        child=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert child.returncode == 0, child.stderr
        result=json.loads(child.stdout)
        assert result.get('ok') and result['subscribed'],result
    with kb.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM kanban_notify_subs').fetchone()[0] == 1


def test_process_restart_restores_worker_checkpoint_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_TASK','t-continuity')
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    monkeypatch.setenv('HERMES_SESSION_SOURCE','kanban')
    child=subprocess.run([sys.executable,'-B','-c','''
import os
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.turn_checkpoint import initialize_agent_turn_checkpoint
db=SessionDB(db_path=Path(os.environ['HERMES_HOME'])/'state.db')
db.create_session(session_id='worker-crash',source='kanban')
db.append_message(session_id='worker-crash',role='assistant',content='42 items validated')
agent=SimpleNamespace(session_id='worker-crash',_session_db=db)
initialize_agent_turn_checkpoint(agent,turn_id='original',user_content='Report',
                                 messages=[{'role':'user','content':'Report'}])
agent._turn_checkpoint_store.transition('worker-crash',phase='planning',next_action='write_report_from_42_items')
os._exit(23)
'''],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert child.returncode == 23,child.stderr
    from agent.turn_checkpoint import initialize_agent_turn_checkpoint
    db=SessionDB(db_path=tmp_path/'state.db')
    assert '42 items validated' in str(db.get_messages('worker-crash'))
    replacement=SimpleNamespace(session_id='worker-crash',_session_db=db)
    state=initialize_agent_turn_checkpoint(replacement,turn_id='replacement',
            user_content='Updated card instructions',messages=db.get_messages('worker-crash'))
    assert state['turn_id']=='original'
    assert state['next_action']=='write_report_from_42_items'
    assert state['recovery']['restored'] is True
    db.close()
