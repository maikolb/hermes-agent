"""Runtime identity and recovery must preserve exclusive ownership."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_runtime as runtime
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')  # NO_OWNER_QUESTIONS_UNIVERSAL_20261010


def request(conn):
    return delivery.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'3'},
        text='Audit',project={'profile':'default','delivery_type':'report'})


@pytest.mark.parametrize('status', ['blocked', 'done', 'archived'])
@pytest.mark.parametrize('owner_reply', ['Yes, use QA-01.', 'No, do not use that account.'])
def test_owner_guidance_retried_until_decided_without_resuming_card(tmp_path, monkeypatch, status, owner_reply):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    clock = [1800000000]
    monkeypatch.setattr(delivery.time, 'time', lambda: clock[0])
    with kb.connect_closing() as conn:
        rid = request(conn)
        reservation = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, reservation['claim_token'], pid=2147483647)
        human = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
            question='Which test account should be used?', context={})
        delivery.resolve_decision(conn, human, action='human',
            answer='Which test account should be used?', author='Principal')
        conn.execute('UPDATE tasks SET status=?,worker_pid=NULL WHERE id=?', (status, task.id))
        conn.commit()
        source = {'platform': 'vigilia', 'actor': 'Maikol', 'message_id': 'owner-reply'}
        receipt = delivery.receive_owner_guidance(conn, task.id,
            text=owner_reply, source=source)
        decision_id = receipt['decision_id']

        def requests():
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'",
                (task.id,)) if json.loads(row[0]).get('decision_id') == decision_id]

        for attempt in range(delivery.DECISION_MAX_REMINDERS + 2):
            clock[0] += delivery.DECISION_REMINDER_GAP + 1
            runtime.reconcile_runtime(conn)
            expected = 1 if status == 'archived' else attempt + 2
            assert len(requests()) == expected
            runtime.reconcile_runtime(conn)
            assert len(requests()) == expected, 'same tick duplicated the Principal wake'
            assert delivery.get_decision(conn, decision_id)['status'] == 'pending'
            assert delivery.get_decision(conn, human)['status'] == 'human'
            assert kb.get_task(conn, task.id).status != 'ready', 'text is not an automatic approval'

        if status != 'archived':
            delivery.resolve_decision(conn, decision_id, action='continue',
                answer='Owner instruction processed.', author='Principal')
            before = len(requests())
            clock[0] += delivery.DECISION_REMINDER_GAP + 1
            runtime.reconcile_runtime(conn)
            assert len(requests()) == before
            assert json.loads(delivery.get_decision(conn, decision_id)['context'])['source'] == source


def test_portal_reply_retries_principal_wake_without_automatic_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    monkeypatch.setattr(delivery, '_run_process_alive', lambda *args:False)
    monkeypatch.setattr(runtime, 'run_termination_pending', lambda *args:False)
    clock = [1800000000]
    monkeypatch.setattr(delivery.time, 'time', lambda:clock[0])
    with kb.connect_closing() as conn:
        rid = request(conn)
        reserved = delivery.reserve_request(conn, capacity=1)
        task = delivery.bootstrap_card(conn, rid, reserved['claim_token'], pid=os.getpid())
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
            question='Continue with the next step?', context={})
        delivery.resolve_decision(conn, decision, action='human', answer='Ask requester', author='Principal',
            public_message={'kind':'question','to':'solicitante','text':'Qual registro?'})
        delivery.resume_after_answer(conn, task.id, answer='Registro dois',
            source={'platform':'portal','actor':'Lucas','message_id':'reply-1'})
        for _ in range(delivery.DECISION_MAX_REMINDERS + 2):
            clock[0] += delivery.DECISION_REMINDER_GAP + 1
            runtime.reconcile_runtime(conn)
            saved = delivery.get_decision(conn, decision)
            assert saved['status'] == 'pending' and saved['author'] is None
            assert kb.get_task(conn, task.id).status == 'blocked'
        assert len(json.loads(saved['context'])['reminders']) > delivery.DECISION_MAX_REMINDERS


def test_worker_cannot_switch_to_another_path_release(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=request(conn);reservation=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,reservation['claim_token'],pid=os.getpid())
    monkeypatch.setattr(kb,'_IS_WINDOWS',False)
    monkeypatch.setattr(kb,'_resolve_hermes_argv',lambda:['/old-release/bin/hermes'])
    monkeypatch.setattr(kb,'_retag_legacy_worker_sessions',lambda path:None)
    monkeypatch.setattr(kb,'_resolve_worker_cli_toolsets',lambda path:[])
    captured=[]
    monkeypatch.setattr('subprocess.Popen',lambda cmd,**kw:captured.append((cmd,kw)) or SimpleNamespace(pid=123))
    kb._default_spawn(task,str(tmp_path))
    assert captured[0][0][:3]==kb._module_hermes_argv()
    assert captured[0][1]['env']['PYTHONPATH'].split(os.pathsep)[0]==str(Path(kb.__file__).resolve().parents[1])


def test_startup_keeps_live_bootstrap_and_recovers_dead_reservation(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=request(conn);reservation=delivery.reserve_request(conn,capacity=2)
        conn.execute('UPDATE nfos_requests SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (os.getpid(),kb._process_start_time(os.getpid()),rid));conn.commit()
        assert runtime.reconcile_starting(conn,now=reservation['claimed_at']+100)==[]
        conn.execute('UPDATE nfos_requests SET worker_started_at=-1 WHERE id=?',(rid,));conn.commit()
        assert runtime.reconcile_starting(conn,now=reservation['claimed_at']+100)==[rid]
        assert delivery.get_request(conn,rid)['status']=='pending'


def test_two_full_slots_preserve_pending_request(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        for name in ['one','two']:
            tid=kb.create_task(conn,title=name,assignee='default',delivery_type='report',requires_repo=False)
            assert kb.claim_task(conn,tid)
        rid=request(conn)
        assert delivery.reserve_request(conn,capacity=2) is None
        assert delivery.get_request(conn,rid)['status']=='pending'


def test_workflow_cli_survives_terminal_pythonpath_sanitization(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=request(conn);reservation=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,reservation['claim_token'],pid=os.getpid())
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,'-B',str(Path(delivery.__file__)),'show','--task',task.id],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout)
    assert payload['workflow']['task_id']==task.id
    assert payload['runtime']['code_root']==str(Path(kb.__file__).resolve().parents[1])


# ---------------------------------------------------------------------------
# NFOS_CAMINHO_NO_PONTO_DE_USO_20261016: the runtime hands the worker the paths it already knows
# ---------------------------------------------------------------------------

def test_wait_decision_command_is_runnable():
    command = runtime.wait_decision_command('dec_1')
    assert command.startswith(runtime.workflow_command())
    assert command.endswith('--timeout 300')
    assert ' wait --decision dec_1 ' in command
    assert Path(delivery.__file__).resolve().is_file() and str(Path(delivery.__file__).resolve()) in command
    # An id with shell metacharacters stays one argument.
    assert "'dec 1; rm'" in runtime.wait_decision_command('dec 1; rm')


@pytest.fixture
def locations_env(tmp_path, monkeypatch):
    home = tmp_path / 'hermes_root'
    (home / 'cache' / 'terminal-output').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'pilot')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    engine = tmp_path / 'pw_src'
    (engine / 'project_workflow').mkdir(parents=True)
    monkeypatch.setenv('PROJECT_WORKFLOW_SRC', str(engine))
    workspace = tmp_path / 'worktree'
    workspace.mkdir()
    return SimpleNamespace(home=home, engine=engine, workspace=workspace, task=SimpleNamespace(id='t_loc'))


def test_worker_locations_lists_existing_paths(locations_env, monkeypatch):
    env = locations_env
    reports = env.home / 'reports' / 'pilot' / 't_loc'
    reports.mkdir(parents=True)
    logs = env.home / 'kanban' / 'boards' / 'pilot' / 'logs'
    logs.mkdir(parents=True)

    block = runtime.worker_locations(env.task, str(env.workspace), 'pilot', hermes_home=str(env.home))

    assert runtime.workflow_command() in block
    assert str(Path(delivery.__file__).resolve().parents[1]) in block
    assert str(env.engine / 'project_workflow') in block
    assert str(env.workspace) in block
    assert str(reports) in block
    assert str(env.home / 'cache' / 'terminal-output') in block
    assert str(logs / 't_loc.log') in block
    assert block.splitlines()[0].startswith('Where things are')

    # A line whose path does not exist is left out; the block never says what is not true.
    reports.rmdir()
    missing_engine = env.engine / 'project_workflow'
    missing_engine.rmdir()
    block = runtime.worker_locations(env.task, str(env.workspace / 'gone'), 'pilot', hermes_home=str(env.home))
    assert str(reports) not in block
    assert 'project-workflow engine source' not in block
    assert 'Your workspace and cwd' not in block
    assert str(env.workspace / 'gone') not in block
    assert runtime.workflow_command() in block


def test_worker_locations_names_browser_cwd(locations_env):
    env = locations_env
    block = runtime.worker_locations(env.task, str(env.workspace), 'pilot', hermes_home=str(env.home))
    line = next(l for l in block.splitlines() if 'Your workspace and cwd' in l)
    assert str(env.workspace) in line
    assert '.playwright-mcp' in line and './file.png' in line and 'relative to this directory' in line


def test_worker_prompt_starts_with_locations(tmp_path, monkeypatch):
    # The block reaches the prompt the dispatcher hands the worker, before the workflow instructions.
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    monkeypatch.setattr(kb, '_resolve_hermes_argv', lambda: ['hermes'])
    monkeypatch.setattr(kb, '_module_hermes_argv', lambda: ['hermes'])
    with kb.connect_closing() as conn:
        rid = request(conn)
        reservation = delivery.reserve_request(conn, capacity=2)
        task = delivery.bootstrap_card(conn, rid, reservation['claim_token'], pid=os.getpid())
        task = kb.get_task(conn, task.id)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured['cmd'] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)
    kb._default_spawn(task, str(workspace))

    prompt = captured['cmd'][captured['cmd'].index('-q') + 1]
    assert 'Where things are (absolute paths, this run):' in prompt
    assert prompt.index('Where things are') < prompt.index('Exact workflow CLI prefix')
    assert 'Your workspace and cwd: ' + str(workspace) in prompt
    assert runtime.workflow_command() in prompt.split('Exact workflow CLI prefix')[0]
