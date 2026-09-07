"""Runtime identity and recovery must preserve exclusive ownership."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_runtime as runtime


def request(conn):
    return delivery.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'3'},
        text='Audit',project={'profile':'default','delivery_type':'report'})


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
