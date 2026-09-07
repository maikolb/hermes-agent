"""A saved task can change processes without losing its publication slot."""
import json
import os
import subprocess
import sys
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d
from hermes_cli import nfos_runtime as runtime
from tests.hermes_cli.test_nfos_candidate_delivery import delivery, A


def set_prior_worker(conn, task, pid):
    """Keep the synthetic fixture's row and durable process receipt consistent."""
    started = kb._process_start_time(pid)
    conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?', (pid, started, task.id))
    conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?', (pid, task.current_run_id))
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND run_id=? AND kind='nfos_worker_created_card'",
                       (task.id, task.current_run_id)).fetchone()
    payload = json.loads(row['payload'])
    payload.update(pid=pid, worker_started_at=started)
    conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    conn.commit()


def test_new_run_recovers_its_publication_slot_and_unknown_effect(delivery):
    conn, task = delivery
    assert d.acquire_project(conn, 'pilot', task.id, task.current_run_id, A)
    conn.execute("INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,created_at,updated_at) VALUES('lost-response',?,?,'deploy','hml',?,'unknown',1,1)",
                 (task.id, task.current_run_id, A))
    set_prior_worker(conn, task, 987654321)
    kb.reclaim_task(conn, task.id)
    current = kb.claim_task(conn, task.id)
    assert current is not None
    assert d.acquire_project(conn, 'pilot', task.id, current.current_run_id, A)
    assert conn.execute("SELECT status FROM nfos_effects WHERE id='lost-response'").fetchone()[0] == 'unknown'
    assert conn.execute('SELECT run_id FROM nfos_project_delivery').fetchone()[0] == current.current_run_id


def test_live_previous_worker_prevents_new_claim_and_keeps_publication_slot(delivery):
    conn, task = delivery
    assert d.acquire_project(conn, 'pilot', task.id, task.current_run_id, A)
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        set_prior_worker(conn, task, proc.pid)
        assert kb.reclaim_task(conn, task.id, signal_fn=lambda *args: None)
        assert proc.poll() is None
        assert kb.claim_task(conn, task.id) is None
        assert conn.execute('SELECT run_id FROM nfos_project_delivery').fetchone()[0] == task.current_run_id
        proc.terminate(); proc.wait(timeout=10)
        current = kb.claim_task(conn, task.id)
        assert current is not None
        assert d.acquire_project(conn, 'pilot', task.id, current.current_run_id, A)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=10)


def test_initial_analysis_can_classify_report_without_git_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid = d.receive_request(conn, source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'report'},
            text='Audit existing behavior, change no code', project={'board':'pilot','profile':'default','delivery_type':'code','repo_path':str(tmp_path)})
        request = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
        spec = {'goal':'Audit', 'criteria':[{'id':'A1','text':'Observed behavior explained'}], 'steps':['Read','Report'], 'delivery_type':'report'}
        assert d.save_spec(conn, task.id, task.current_run_id, spec, author='Claude TL', evidence={'session':'fixture'}) == 1
        saved = kb.get_task(conn, task.id)
        assert saved.delivery_type == 'report' and saved.requires_repo is False
        assert conn.execute('SELECT required FROM task_git_delivery WHERE task_id=?', (task.id,)).fetchone()[0] == 0
        d.advance(conn, task.id, task.current_run_id, 'implement', next_action='Collect evidence')
        with pytest.raises(d.WorkflowError, match='delivery type'):
            d.save_spec(conn, task.id, task.current_run_id, dict(spec, delivery_type='code'), author='Claude TL', evidence={'session':'fixture-two'})


def test_human_reply_is_persisted_until_previous_real_process_exits(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        with kb.connect_closing() as conn:
            rid = d.receive_request(conn, source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'human'},
                text='Audit the selected business rule', project={'board':'pilot','profile':'default','delivery_type':'report'})
            request = d.reserve_request(conn, capacity=2)
            task = d.bootstrap_card(conn, rid, request['claim_token'], pid=proc.pid)
            decision = d.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Choose A or B', context={'checkpoint':'saved'})
            d.resolve_decision(conn, decision, action='human', answer='Which business rule?', author='Principal')
            kb.block_task(conn, task.id, kind='needs_input', reason='Which business rule?')
            d.resume_after_answer(conn, task.id, answer='Use A', source={'platform':'telegram','message_id':'answer-one'})
            assert kb.get_task(conn, task.id).status == 'blocked'
        proc.terminate(); proc.wait(timeout=10)
        with kb.connect_closing() as conn:
            assert d.reconcile_human_answers(conn) == [task.id]
            assert kb.get_task(conn, task.id).status == 'ready'
            assert d.get_decision(conn, decision)['answer'] == 'Use A'
            assert d.reconcile_human_answers(conn) == []
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=10)


def test_startup_preserves_human_question_after_worker_died_before_block(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        rid=d.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'interrupted-human'},
            text='Audit',project={'board':'pilot','profile':'default','delivery_type':'report'})
        request=d.reserve_request(conn,capacity=2)
        task=d.bootstrap_card(conn,rid,request['claim_token'],pid=987654321)
        decision=d.ask_principal(conn,task.id,task.current_run_id,kind='impediment',question='Choose A or B',context={'checkpoint':'saved'})
        d.resolve_decision(conn,decision,action='human',answer='Which rule?',author='Principal')
        runtime.reconcile_runtime(conn)
        assert kb.get_task(conn,task.id).status=='blocked'
        assert d.get_decision(conn,decision)['status']=='human'
        assert d.reserve_request(conn,capacity=2) is None
