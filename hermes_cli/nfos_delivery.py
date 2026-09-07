"""Durable delivery data in the canonical Kanban database.

Tasks/runs remain the lifecycle authority. Requests exist before their worker
creates a card. Every subsequent change and pending decision commits together
with a task event; there is no independent queue or lifecycle database.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any


class WorkflowError(ValueError):
    pass


class OwnershipConflict(WorkflowError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS nfos_requests (
    id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', task_id TEXT REFERENCES tasks(id),
    claim_token TEXT, worker_pid INTEGER, worker_started_at REAL,
    created_at INTEGER NOT NULL, claimed_at INTEGER, last_error TEXT,
    acknowledged_at INTEGER
);
CREATE INDEX IF NOT EXISTS nfos_requests_pending ON nfos_requests(status,created_at);
CREATE TABLE IF NOT EXISTS nfos_workflows (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), request_id TEXT REFERENCES nfos_requests(id),
    stage TEXT NOT NULL DEFAULT 'analysis', spec_revision INTEGER NOT NULL DEFAULT 0,
    next_action TEXT NOT NULL DEFAULT 'Analyze the original request and attachments',
    state_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nfos_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id INTEGER NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
    content TEXT NOT NULL, author TEXT NOT NULL, evidence TEXT NOT NULL,
    created_at INTEGER NOT NULL, UNIQUE(task_id,kind,revision)
);
CREATE TABLE IF NOT EXISTS nfos_decisions (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id INTEGER NOT NULL,
    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', question TEXT NOT NULL,
    context TEXT NOT NULL, answer TEXT, author TEXT, action TEXT,
    spec_revision INTEGER NOT NULL, created_at INTEGER NOT NULL, resolved_at INTEGER,
    dispatched_at INTEGER
);
CREATE INDEX IF NOT EXISTS nfos_decisions_pending ON nfos_decisions(status,created_at);
CREATE TABLE IF NOT EXISTS nfos_effects (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id INTEGER NOT NULL,
    operation TEXT NOT NULL, target TEXT NOT NULL, candidate TEXT NOT NULL,
    status TEXT NOT NULL, evidence TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(task_id,operation,target,candidate)
);
CREATE TABLE IF NOT EXISTS nfos_project_delivery (
    project TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id INTEGER NOT NULL, candidate TEXT NOT NULL, acquired_at INTEGER NOT NULL
);
"""


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def init_schema(conn):
    if conn.in_transaction:
        raise WorkflowError('Schema initialization requires its own transaction')
    conn.executescript(SCHEMA)


def _row(conn, table, key, value):
    row=conn.execute(f'SELECT * FROM {table} WHERE {key}=?',(value,)).fetchone()
    return dict(row) if row else None


def get_request(conn, request_id):
    return _row(conn,'nfos_requests','id',request_id)


def get_workflow(conn, task_id):
    return _row(conn,'nfos_workflows','task_id',task_id)


def get_decision(conn, decision_id):
    return _row(conn,'nfos_decisions','id',decision_id)


def _owned(conn, task_id, run_id):
    task=_kb().get_task(conn,task_id)
    if task is None or task.status!='running' or task.current_run_id!=run_id:
        raise OwnershipConflict('The task no longer belongs to this execution')
    return task


def _event(conn, task_id, run_id, kind, payload):
    _kb()._append_event(conn,task_id,kind,payload,run_id=run_id)


def receive_request(conn, *, source, text, project, attachments=(), part='0'):
    required=('platform','chat_id','thread_id','message_id')
    if any(not str(source.get(k) or '').strip() for k in required):
        raise WorkflowError('A request needs its original platform/chat/topic/message identity')
    if not text.strip() and not attachments:
        raise WorkflowError('A request needs text or attachments')
    source_key=_json([str(source[k]) for k in required]+[str(part)])
    request_id='req_'+hashlib.sha256(source_key.encode()).hexdigest()[:24]
    payload=_json({'source':source,'text':text,'project':project,'attachments':list(attachments)})
    with _kb().write_txn(conn,allow_nested=True):
        conn.execute('INSERT OR IGNORE INTO nfos_requests(id,source_key,payload,created_at) VALUES(?,?,?,?)',
                     (request_id,source_key,payload,int(time.time())))
    return request_id


def reserve_request(conn, *, capacity):
    if capacity<1:
        return None
    with _kb().write_txn(conn):
        running=conn.execute("SELECT count(*) FROM tasks WHERE status='running' AND task_role='work'").fetchone()[0]
        starting=conn.execute("SELECT count(*) FROM nfos_requests WHERE status='starting'").fetchone()[0]
        if running+starting>=capacity:
            return None
        row=conn.execute("SELECT id FROM nfos_requests WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE nfos_requests SET status='starting',claim_token=?,claimed_at=? WHERE id=?",
                     (_kb()._claimer_id()+':nfos:'+uuid.uuid4().hex,int(time.time()),row['id']))
        return get_request(conn,row['id'])


def bootstrap_card(conn, request_id, token, *, pid):
    kb=_kb()
    with kb.write_txn(conn):
        request=get_request(conn,request_id)
        if not request or request['claim_token']!=token:
            raise OwnershipConflict('The request belongs to another worker')
        if request['task_id']:
            task=kb.get_task(conn,request['task_id'])
            if task.claim_lock!=token or task.worker_pid!=pid:
                raise OwnershipConflict('The existing card has a different execution')
            return task
        if request['status']!='starting':
            raise OwnershipConflict('Request was not reserved for a worker')
        payload=json.loads(request['payload']);project=payload['project'];source=payload['source']
        profile=project['profile'];kind=project.get('delivery_type','code')
        original=payload['text']
        body=original+'\n\nOriginal attachments:\n'+_json(payload['attachments'])
        task_id=kb.create_task(conn,title=(original.strip().splitlines() or ['Analyze attached request'])[0][:200],
            body=body,assignee=profile,created_by='worker:'+profile,
            workspace_kind='worktree' if kind=='code' else 'scratch',
            workspace_path=project.get('repo_path') if kind=='code' else None,
            project_id=project.get('project_id'),requires_repo=kind=='code',delivery_type=kind,
            board=project.get('board'),idempotency_key=request_id,goal_mode=True,
            max_runtime_seconds=project.get('max_runtime_seconds',7200),
            model_override=project.get('model'),provider_override=project.get('provider'),
            reasoning_effort=project.get('reasoning_effort'))
        task=kb.claim_task(conn,task_id,claimer=token)
        if task is None:
            raise OwnershipConflict('Card could not be claimed')
        started_at=kb._process_start_time(pid)
        conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',(pid,started_at,task_id))
        conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?',(pid,task.current_run_id))
        conn.execute('INSERT INTO nfos_workflows(task_id,request_id,updated_at) VALUES(?,?,?)',
                     (task_id,request_id,int(time.time())))
        # The owner's NFOS workflow owns review and verified delivery for this
        # enrolled card. Keep the legacy row, but do not require a second
        # reviewer process or an unrelated board-admin policy on this path.
        conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
        conn.execute("UPDATE nfos_requests SET status='attached',task_id=?,worker_pid=?,worker_started_at=? WHERE id=?",
                     (task_id,pid,started_at,request_id))
        kb.add_notify_sub(conn,task_id=task_id,platform=source['platform'],chat_id=source['chat_id'],
            thread_id=source['thread_id'],user_id=source.get('user_id'),chat_type=source.get('chat_type'),
            notifier_profile=source.get('profile') or profile,delivery_mode='notify+wake')
        _event(conn,task_id,task.current_run_id,'nfos_worker_created_card',{'request_id':request_id,'pid':pid})
        return kb.get_task(conn,task_id)


def _artifact(conn, task_id, kind):
    row=conn.execute('SELECT * FROM nfos_artifacts WHERE task_id=? AND kind=? ORDER BY revision DESC LIMIT 1',
                     (task_id,kind)).fetchone()
    return dict(row) if row else None


def get_spec(conn, task_id):
    return _artifact(conn,task_id,'spec')


def save_spec(conn, task_id, run_id, spec, *, author, evidence):
    if not spec.get('goal') or not spec.get('criteria') or not spec.get('steps'):
        raise WorkflowError('A spec needs a goal, verifiable criteria and direct steps')
    ids=[c.get('id') for c in spec['criteria']]
    if not all(ids) or len(set(ids))!=len(ids) or any(not c.get('text') for c in spec['criteria']):
        raise WorkflowError('Each spec criterion needs a unique id and description')
    if author not in {'Claude TL','Codex'} or not evidence:
        raise WorkflowError('Record the actual TL/Codex execution evidence')
    if author=='Codex' and not evidence.get('fallback_reason'):
        raise WorkflowError('Codex spec fallback needs the Claude unavailability reason')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        if spec.get('delivery_type')!=task.delivery_type:
            raise WorkflowError('Spec delivery type must match the card')
        wf=get_workflow(conn,task_id)
        revision=wf['spec_revision']+1
        conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (task_id,run_id,'spec',revision,_json(spec),author,_json(evidence),int(time.time())))
        conn.execute("UPDATE nfos_workflows SET stage='spec',spec_revision=?,next_action='Implement and verify the persisted spec',updated_at=? WHERE task_id=?",
                     (revision,int(time.time()),task_id))
        _event(conn,task_id,run_id,'nfos_spec_saved',{'revision':revision,'author':author,'evidence':evidence})
        return revision


def advance(conn, task_id, run_id, stage, *, next_action, state=None):
    if stage not in {'analysis','implement','homolog','review','publish','verify','report'}:
        raise WorkflowError('Unknown delivery stage')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        wf=get_workflow(conn,task_id)
        if stage!='analysis' and not wf['spec_revision']:
            raise WorkflowError('Persist the spec before implementation')
        saved=json.loads(wf['state_json']);saved.update(state or {})
        if task.delivery_type=='code' and stage in {'homolog','publish'}:
            if stage=='publish' and not _approved(conn,task_id,wf['spec_revision'],state=saved):
                raise WorkflowError('Principal review of this candidate is pending')
            _project_owned(conn,task_id,run_id,saved.get('homolog_sha'))
        conn.execute('UPDATE nfos_workflows SET stage=?,next_action=?,state_json=?,updated_at=? WHERE task_id=?',
                     (stage,next_action,_json(saved),int(time.time()),task_id))
        _event(conn,task_id,run_id,'nfos_progress',{'stage':stage,'next_action':next_action,'state':state or {}})


def save_report(conn, task_id, run_id, report):
    with _kb().write_txn(conn):
        _owned(conn,task_id,run_id)
        spec=get_spec(conn,task_id)
        if not spec:
            raise WorkflowError('No persisted spec')
        criteria=json.loads(spec['content'])['criteria']
        results={row['id']:row for row in report.get('criteria',[])}
        if any(c['id'] not in results or results[c['id']].get('status')!='PASS' or not results[c['id']].get('evidence') for c in criteria):
            raise WorkflowError('Every spec criterion requires passing evidence')
        if not report.get('summary') or not report.get('artifacts'):
            raise WorkflowError('Report needs outcome and accessible artifact references')
        previous=_artifact(conn,task_id,'report');revision=(previous['revision'] if previous else 0)+1
        conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (task_id,run_id,'report',revision,_json(report),'worker',_json({'spec_revision':spec['revision']}),int(time.time())))
        _event(conn,task_id,run_id,'nfos_report_saved',{'revision':revision,'spec_revision':spec['revision']})


def ask_principal(conn, task_id, run_id, *, kind, question, context):
    if kind not in {'review','impediment'} or not question.strip():
        raise WorkflowError('A decision needs its kind and concrete question')
    with _kb().write_txn(conn):
        _owned(conn,task_id,run_id)
        existing=conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND run_id=? AND kind=? AND question=? AND status='pending'",
                               (task_id,run_id,kind,question)).fetchone()
        if existing:
            return existing['id']
        revision=get_workflow(conn,task_id)['spec_revision']
        context=dict(context)
        if kind=='review':
            context['review_identity']=_review_identity(conn,task_id)
        decision_id='dec_'+uuid.uuid4().hex[:20]
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (decision_id,task_id,run_id,kind,question,_json(context),revision,int(time.time())))
        _event(conn,task_id,run_id,'nfos_principal_requested',{'decision_id':decision_id,'kind':kind,'question':question})
        return decision_id


def pending_decisions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE status='pending' ORDER BY created_at,id")]


def resume_after_answer(conn,task_id,*,answer,source):
    if not answer.strip() or not source:
        raise WorkflowError('Record the actual human answer and its source')
    with _kb().write_txn(conn):
        if not get_workflow(conn,task_id):
            raise WorkflowError('Unknown NFOS card')
        rows=conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='human'",(task_id,)).fetchall()
        if not rows:
            raise WorkflowError('No pending human question on this card')
        if not _kb().unblock_task(conn,task_id):
            raise WorkflowError('Wait for the previous worker to finish saving and stop')
        conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer=?,author='Human',resolved_at=? WHERE task_id=? AND status='human'",
            (answer,int(time.time()),task_id))
        _event(conn,task_id,None,'nfos_human_answered',{'answer':answer,'source':source,'decisions':[r['id'] for r in rows]})


def resolve_decision(conn, decision_id, *, action, answer, author):
    if action not in {'continue','approve','changes','human'} or not answer.strip() or author!='Principal':
        raise WorkflowError('Principal decision requires its concrete answer and action')
    with _kb().write_txn(conn):
        row=get_decision(conn,decision_id)
        if not row:
            raise WorkflowError('Unknown decision')
        if row['status']!='pending':
            if row['action']==action and row['answer']==answer:
                return
            raise WorkflowError('Decision was already resolved')
        if action=='approve' and row['kind']!='review':
            raise WorkflowError('Only a delivery review can authorize publication')
        if row['spec_revision']!=get_workflow(conn,row['task_id'])['spec_revision']:
            raise WorkflowError('Spec changed during review; review the current revision')
        if action=='approve':
            identity=_review_identity(conn,row['task_id'])
            if json.loads(row['context']).get('review_identity')!=identity:
                raise WorkflowError('Candidate or report changed during review')
            task=_kb().get_task(conn,row['task_id'])
            if task.delivery_type=='code':
                if not all(identity.get(k) for k in ('homolog_sha','candidate_tree','homolog_evidence')):
                    raise WorkflowError('Review needs the actual homologated candidate and evidence')
                if not _confirmed(conn,task.id,'pr',identity['homolog_sha']):
                    raise WorkflowError('Review needs the confirmed PR for this candidate')
            elif not identity.get('report_revision'):
                raise WorkflowError('Review needs the saved report')
        conn.execute('UPDATE nfos_decisions SET status=?,answer=?,author=?,action=?,resolved_at=? WHERE id=?',
                     ('human' if action=='human' else 'resolved',answer,author,action,int(time.time()),decision_id))
        _event(conn,row['task_id'],row['run_id'],'nfos_principal_resolved',{'decision_id':decision_id,'action':action,'answer':answer})


def _review_identity(conn,task_id,*,state=None):
    wf=get_workflow(conn,task_id)
    task=_kb().get_task(conn,task_id)
    if task.delivery_type=='code':
        state=json.loads(wf['state_json']) if state is None else state
        return {k:state.get(k) for k in ('homolog_sha','candidate_tree','homolog_evidence')}
    report=_artifact(conn,task_id,'report')
    return {'report_revision':report['revision'] if report else None}


def _approved(conn, task_id, revision, *, state=None):
    row=conn.execute("SELECT action,context FROM nfos_decisions WHERE task_id=? AND kind='review' AND status='resolved' AND spec_revision=? ORDER BY resolved_at DESC,rowid DESC LIMIT 1",
                     (task_id,revision)).fetchone()
    return bool(row and row['action']=='approve' and
        json.loads(row['context']).get('review_identity')==_review_identity(conn,task_id,state=state))


def _confirmed(conn,task_id,operation,candidate):
    return conn.execute("SELECT * FROM nfos_effects WHERE task_id=? AND operation=? AND candidate=? AND status='confirmed' ORDER BY updated_at DESC LIMIT 1",
        (task_id,operation,candidate)).fetchone()


def _project_owned(conn,task_id,run_id,candidate):
    wf=get_workflow(conn,task_id)
    request=get_request(conn,wf['request_id'])
    project=json.loads(request['payload'])['project']
    key=project.get('board') or project.get('project_id')
    row=_row(conn,'nfos_project_delivery','project',key)
    if not candidate or not row or (row['task_id'],row['run_id'],row['candidate'])!=(task_id,run_id,candidate):
        raise WorkflowError('Acquire the project publication slot for this candidate first')


def begin_effect(conn, task_id, run_id, *, operation, target, candidate):
    if operation not in {'pr','merge','deploy'} or not target or not candidate:
        raise WorkflowError('External effect needs operation, destination and exact candidate')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        if task.delivery_type=='code':
            wf=get_workflow(conn,task_id);state=json.loads(wf['state_json'])
            if operation in {'merge','deploy'} and not _approved(conn,task_id,wf['spec_revision']):
                raise WorkflowError('Principal review of this candidate is pending')
            _project_owned(conn,task_id,run_id,state.get('homolog_sha'))
            expected=state.get('integrated_sha') if operation=='deploy' else state.get('homolog_sha')
            if candidate!=expected:
                raise WorkflowError('Use the confirmed integrated candidate' if operation=='deploy' else 'Use the homologated candidate')
            if operation=='deploy' and not _confirmed(conn,task_id,'merge',state.get('homolog_sha')):
                raise WorkflowError('Confirm the integrated merge before deploy')
        key=hashlib.sha256(_json([task_id,operation,target,candidate]).encode()).hexdigest()
        existing=_row(conn,'nfos_effects','id',key)
        if existing:
            if existing['status']=='absent':
                conn.execute("UPDATE nfos_effects SET status='unknown',run_id=?,updated_at=? WHERE id=?",
                             (run_id,int(time.time()),key))
                _event(conn,task_id,run_id,'nfos_effect_retry',{'effect_id':key,'readback':json.loads(existing['evidence'])})
                return {**existing,'status':'unknown','execute':True,'reconcile':False}
            return {**existing,'execute':False,'reconcile':existing['status']=='unknown'}
        now=int(time.time())
        conn.execute('INSERT INTO nfos_effects(id,task_id,run_id,operation,target,candidate,status,created_at,updated_at) VALUES(?,?,?,?,?,?,\'unknown\',?,?)',
                     (key,task_id,run_id,operation,target,candidate,now,now))
        _event(conn,task_id,run_id,'nfos_effect_requested',{'effect_id':key,'operation':operation,'target':target,'candidate':candidate})
        return {'id':key,'execute':True,'reconcile':False,'status':'unknown'}


def reconcile_effect(conn, effect_id, *, found, evidence):
    if not evidence or not evidence.get('readback'):
        raise WorkflowError('Reconciliation requires destination readback evidence')
    with _kb().write_txn(conn):
        effect=_row(conn,'nfos_effects','id',effect_id)
        if not effect:
            raise WorkflowError('Unknown effect')
        if effect['status']=='confirmed' and not found:
            raise WorkflowError('Confirmed effect cannot be undone by an absent lookup')
        task=_kb().get_task(conn,effect['task_id'])
        if found and task.delivery_type=='code':
            wf=get_workflow(conn,task.id);state=json.loads(wf['state_json'])
            if evidence.get('candidate')!=effect['candidate']:
                raise WorkflowError('Destination readback must identify this exact candidate')
            if effect['operation'] in {'merge','deploy'}:
                if evidence.get('tree')!=state.get('candidate_tree'):
                    raise WorkflowError('Integrated tree differs from homologation; verify the new tree in homolog first')
            if effect['operation']=='merge':
                if not evidence.get('integrated_sha'):
                    raise WorkflowError('Read the exact integrated SHA')
                state['integrated_sha']=evidence['integrated_sha']
            if effect['operation']=='deploy':
                if not evidence.get('artifact') or not evidence.get('behavior_evidence'):
                    raise WorkflowError('Read the deployed artifact and verify actual behavior')
                state.update(artifact=evidence['artifact'],production_readback=evidence)
            conn.execute('UPDATE nfos_workflows SET state_json=?,updated_at=? WHERE task_id=?',
                (_json(state),int(time.time()),task.id))
        status='confirmed' if found else 'absent'
        conn.execute('UPDATE nfos_effects SET status=?,evidence=?,updated_at=? WHERE id=?',
                     (status,_json(evidence),int(time.time()),effect_id))
        _event(conn,effect['task_id'],effect['run_id'],'nfos_effect_reconciled',{'effect_id':effect_id,'status':status,'evidence':evidence})


def completion_ready(conn, task_id):
    wf=get_workflow(conn,task_id)
    if wf is None:
        return True
    report=_artifact(conn,task_id,'report')
    if not wf['spec_revision'] or not report or json.loads(report['evidence']).get('spec_revision')!=wf['spec_revision']:
        return False
    if not _approved(conn,task_id,wf['spec_revision']):
        return False
    task=_kb().get_task(conn,task_id)
    if task.delivery_type=='code':
        state=json.loads(wf['state_json'])
        required=('homolog_sha','integrated_sha','artifact','production_readback')
        if any(not state.get(k) for k in required):
            return False
        if not all(_confirmed(conn,task_id,operation,candidate) for operation,candidate in
                [('pr',state['homolog_sha']),('merge',state['homolog_sha']),('deploy',state['integrated_sha'])]):
            return False
    return True


def acquire_project(conn, project, task_id, run_id, candidate):
    with _kb().write_txn(conn):
        _owned(conn,task_id,run_id)
        current=_row(conn,'nfos_project_delivery','project',project)
        if current:
            return current['task_id']==task_id and current['run_id']==run_id and current['candidate']==candidate
        conn.execute('INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)',
                     (project,task_id,run_id,candidate,int(time.time())))
        _event(conn,task_id,run_id,'nfos_project_delivery_acquired',{'project':project,'candidate':candidate})
        return True


def release_project(conn, project, task_id, run_id):
    with _kb().write_txn(conn):
        if conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('merge','deploy')",(task_id,)).fetchone():
            raise WorkflowError('Read the unresolved merge/deploy destination before releasing publication')
        changed=conn.execute('DELETE FROM nfos_project_delivery WHERE project=? AND task_id=? AND run_id=?',
                             (project,task_id,run_id)).rowcount
        if changed:
            _event(conn,task_id,run_id,'nfos_project_delivery_released',{'project':project})
        return bool(changed)


def main():
    """JSON CLI used by full Hermes workers and their Principal."""
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['show','save-spec','save-report','progress','ask','decide',
        'pending','effect','reconcile','acquire-project','release-project','receive','resume'])
    parser.add_argument('--task',default=os.environ.get('HERMES_KANBAN_TASK'))
    parser.add_argument('--run',type=int,default=int(os.environ.get('HERMES_KANBAN_RUN_ID') or 0))
    parser.add_argument('--input',help='JSON file with spec/report/state/question/receipt/request')
    parser.add_argument('--evidence',help='JSON file with the TL session/output and fallback reason when applicable')
    parser.add_argument('--author',default='Claude TL',choices=['Claude TL','Codex'])
    parser.add_argument('--stage')
    parser.add_argument('--next',dest='next_action',default='')
    parser.add_argument('--kind',choices=['review','impediment'])
    parser.add_argument('--decision')
    parser.add_argument('--resolution',choices=['continue','approve','changes','human'])
    parser.add_argument('--operation',choices=['pr','merge','deploy'])
    parser.add_argument('--target')
    parser.add_argument('--candidate')
    parser.add_argument('--effect-id')
    parser.add_argument('--project',default=os.environ.get('HERMES_KANBAN_BOARD'))
    args=parser.parse_args()
    payload=json.loads(Path(args.input).read_text(encoding='utf-8-sig')) if args.input else {}
    evidence=json.loads(Path(args.evidence).read_text(encoding='utf-8-sig')) if args.evidence else {}
    with _kb().connect_closing() as conn:
        if args.action=='show':
            result={'workflow':get_workflow(conn,args.task),'spec':get_spec(conn,args.task),
                'report':_artifact(conn,args.task,'report'),
                'decisions':[dict(r) for r in conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? ORDER BY created_at',(args.task,))],
                'effects':[dict(r) for r in conn.execute('SELECT * FROM nfos_effects WHERE task_id=?',(args.task,))]}
        elif args.action=='save-spec':
            result={'revision':save_spec(conn,args.task,args.run,payload,author=args.author,evidence=evidence)}
        elif args.action=='save-report':
            save_report(conn,args.task,args.run,payload);result={'saved':True}
        elif args.action=='progress':
            advance(conn,args.task,args.run,args.stage,next_action=args.next_action,state=payload);result={'saved':True}
        elif args.action=='ask':
            result={'decision_id':ask_principal(conn,args.task,args.run,kind=args.kind,
                question=payload['question'],context=payload.get('context',{}))}
        elif args.action=='pending':
            result=pending_decisions(conn)
        elif args.action=='resume':
            resume_after_answer(conn,args.task,answer=payload['answer'],source=payload['source']);result={'resumed':True}
        elif args.action=='decide':
            if os.environ.get('HERMES_KANBAN_TASK'):
                raise WorkflowError('The Principal resolves reviews in its own coordinator session')
            resolve_decision(conn,args.decision,action=args.resolution,answer=payload['answer'],author='Principal');result={'saved':True}
        elif args.action=='effect':
            result=begin_effect(conn,args.task,args.run,operation=args.operation,target=args.target,candidate=args.candidate)
        elif args.action=='reconcile':
            reconcile_effect(conn,args.effect_id,found=payload['found'],evidence=payload['evidence']);result={'saved':True}
        elif args.action=='acquire-project':
            result={'acquired':acquire_project(conn,args.project,args.task,args.run,args.candidate)}
        elif args.action=='release-project':
            result={'released':release_project(conn,args.project,args.task,args.run)}
        elif args.action=='receive':
            result={'request_id':receive_request(conn,source=payload['source'],text=payload['text'],
                project=payload['project'],attachments=payload.get('attachments',[]),part=payload.get('part','0'))}
        print(_json(result))


if __name__=='__main__':
    try:
        main()
    except WorkflowError as exc:
        print(_json({'error':str(exc),'type':type(exc).__name__}))
        raise SystemExit(1)
