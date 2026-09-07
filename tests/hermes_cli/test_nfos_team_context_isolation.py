"""Operational home/board routing separation, not an operating-system sandbox."""
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d
from hermes_state import SessionDB
from tests.hermes_cli.test_nfos_bootstrap_process_crashes import assert_child_identity, wait_path


def public_snapshot(conn):
    return {table:[dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('nfos_requests','tasks','task_runs','nfos_workflows','nfos_artifacts','nfos_decisions','task_events')}


@pytest.fixture
def teams(tmp_path):
    rows=[]
    try:
        for name in ('team-a','team-b'):
            home=tmp_path/name;home.mkdir()
            env=dict(os.environ,HERMES_HOME=str(home),HERMES_KANBAN_HOME=str(home),
                     HERMES_KANBAN_BOARD=name,PYTHONPATH=str(ROOT))
            for key in ('HERMES_KANBAN_DB','HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID',
                        'HERMES_KANBAN_WORKSPACES_ROOT','HERMES_DELEGATED_CHILD_CONTEXT'):
                env.pop(key,None)
            marker=home/'initialized.json'
            proc=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--child',name,str(marker)],
                cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            rows.append({'home':home,'env':env,'process':proc,'marker':marker})
        for row in rows:
            wait_path(row['marker'],row['process'],timeout=35)
            row.update(json.loads(row['marker'].read_text()))
            assert_child_identity(row['process'],row['pid'])
            row['env'].update(HERMES_KANBAN_TASK=row['task'],HERMES_KANBAN_RUN_ID=str(row['run']))
        yield rows
    finally:
        for row in rows:
            proc=row['process']
            if proc.poll() is None:
                descendants=psutil.Process(proc.pid).children(recursive=True)
                for child in reversed(descendants):
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
                psutil.wait_procs(descendants,timeout=10)
                proc.kill()
            proc.communicate(timeout=10)


def test_two_real_processes_route_requests_specs_decisions_and_workspaces_to_their_team(teams):
    assert teams[0]['pid'] != teams[1]['pid']
    assert teams[0]['task'] != teams[1]['task']
    # Both databases may allocate run 1. The board/home and task are part of
    # execution identity; a process-global run number is not its authority.
    assert teams[0]['run'] == teams[1]['run'] == 1
    for own,foreign in (teams,teams[::-1]):
        home=own['home'];workspace=Path(own['workspace'])
        assert Path(own['db']) == home/'kanban/boards'/own['name']/'kanban.db'
        assert workspace == home/'kanban/boards'/own['name']/'workspaces'/own['task']
        assert workspace.is_relative_to(home)
        assert not workspace.is_relative_to(foreign['home'])
        assert (workspace/'partial-work.txt').read_text() == own['canary']
        with kb.connect_closing(Path(own['db'])) as conn:
            snapshot=public_snapshot(conn)
            serialized=json.dumps(snapshot)
            assert own['canary'] in serialized and foreign['canary'] not in serialized
            assert len(snapshot['nfos_requests']) == len(snapshot['tasks']) == len(snapshot['nfos_workflows']) == 1
            assert len(snapshot['nfos_artifacts']) == len(snapshot['nfos_decisions']) == 1
            assert kb.get_task(conn,foreign['task']) is None
            assert d.get_request(conn,foreign['request']) is None
            assert d.get_decision(conn,foreign['decision']) is None
            context=kb.build_worker_context(conn,own['task'])
            assert own['canary'] in context and foreign['canary'] not in context
            sub=conn.execute('SELECT * FROM kanban_notify_subs').fetchone()
            assert sub['chat_id']==own['chat_id'] and sub['thread_id']==own['name']


def test_real_sessiondb_history_and_messages_remain_in_the_selected_home(teams):
    for own,foreign in (teams,teams[::-1]):
        assert Path(own['sessions_db']) == own['home']/'state.db'
        db=SessionDB(Path(own['sessions_db']))
        try:
            assert [row['id'] for row in db.search_sessions()] == [own['session']]
            assert db.get_session(foreign['session']) is None
            assert db.get_messages(foreign['session']) == []
            messages=db.get_messages(own['session'])
            assert len(messages)==3
            assert [row['role'] for row in messages]==['user','assistant','tool']
            encoded=json.dumps(messages)
            assert own['canary'] in encoded and foreign['canary'] not in encoded
            assert messages[-1]['tool_call_id']==own['call_id']
            session=db.get_session(own['session'])
            assert session['chat_id']==own['chat_id'] and session['thread_id']==own['name']
        finally:db.close()


def test_normal_worker_cli_cannot_mutate_other_team_card_using_same_numeric_run(teams):
    before={}
    for row in teams:
        with kb.connect_closing(Path(row['db'])) as conn:
            before[row['name']]=public_snapshot(conn)
    for own,foreign in (teams,teams[::-1]):
        result=subprocess.run([sys.executable,'-B',str(Path(d.__file__)),'progress',
            '--task',foreign['task'],'--run',str(own['run']),'--stage','analysis','--next','Cross-team write must not occur'],
            cwd=own['workspace'],env=own['env'],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert result.returncode==1,(result.stdout,result.stderr)
        assert json.loads(result.stdout)['type']=='OwnershipConflict'
        # The normal no-override read still resolves its own saved card/spec.
        show=subprocess.run([sys.executable,'-B',str(Path(d.__file__)),'show'],
            cwd=own['workspace'],env=own['env'],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert show.returncode==0,show.stderr
        payload=json.loads(show.stdout)
        assert payload['workflow']['task_id']==own['task']
        assert own['canary'] in show.stdout and foreign['canary'] not in show.stdout
    for row in teams:
        with kb.connect_closing(Path(row['db'])) as conn:
            assert public_snapshot(conn)==before[row['name']]


def _child(name,marker):
    canary=name.upper().replace('-','_')+'_ONLY_CONTENT'
    chat_id='-10041' if name=='team-a' else '-10042'
    source={'platform':'telegram','chat_id':chat_id,'thread_id':name,'message_id':'42'}
    session='fixture-'+name
    # Team routing points to a provisioned board. A nonexistent environment
    # slug deliberately falls back to default, so create it through its API.
    kb.create_board(name,name='Fixture '+name)
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid=d.receive_request(conn,source=source,text='Audit '+canary,
            project={'board':name,'profile':'default','delivery_type':'report'})
        reservation=d.reserve_request(conn,capacity=2)
        task=d.bootstrap_card(conn,rid,reservation['claim_token'],pid=os.getpid())
        workspace=kb.resolve_workspace(task,board=name,conn=conn)
        kb.set_workspace_path(conn,task.id,workspace)
        (workspace/'partial-work.txt').write_text(canary)
        d.save_spec(conn,task.id,task.current_run_id,
            {'goal':canary,'criteria':[{'id':'C1','text':'Audit '+canary}],
             'steps':['Read '+canary],'delivery_type':'report'},
            author='Codex',evidence={'session':'fixture-tl-'+name,'fallback_reason':'Synthetic contract fixture; no model was called'})
        decision=d.ask_principal(conn,task.id,task.current_run_id,kind='impediment',
            question='Review '+canary,context={'team':canary})
        dbpath=next(row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main')
    db=SessionDB()
    try:
        db.create_session(session,source='cli',model='synthetic-no-model-call',cwd=str(workspace))
        db.record_gateway_session_peer(session,source='telegram',session_key='fixture:'+name,
            chat_id=chat_id,thread_id=name,user_id='fixture-member-'+name)
        db.append_message(session,'user',content=canary)
        db.append_message(session,'assistant',content='Fixture progress '+canary,
            tool_calls=[{'id':'call-'+name,'type':'function','function':{'name':'read_file','arguments':json.dumps({'path':'partial-work.txt'})}}])
        db.append_message(session,'tool',content='Fixture output '+canary,tool_name='read_file',tool_call_id='call-'+name)
        result={'name':name,'pid':os.getpid(),'db':str(dbpath),'request':rid,'task':task.id,
            'run':task.current_run_id,'decision':decision,'workspace':str(workspace),'canary':canary,
            'sessions_db':str(db.db_path),'session':session,'chat_id':chat_id,'call_id':'call-'+name}
        temporary=marker.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(result,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,marker)
        sys.stdin.read(1)
    finally:db.close()


if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--child':
    _child(sys.argv[2],Path(sys.argv[3]))
