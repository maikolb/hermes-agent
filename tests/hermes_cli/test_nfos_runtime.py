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
