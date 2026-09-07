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
import stat
import time
import uuid
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

# Terminal tools intentionally sanitize inherited PYTHONPATH. An absolute
# script invocation must still use the source that owns this workflow.
if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


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
        task_id=project.get('existing_task_id')
        if task_id:
            prior=kb.get_task(conn,task_id)
            if not prior or prior.status not in {'backlog','ready','todo'} or prior.current_run_id or get_workflow(conn,task_id):
                raise OwnershipConflict('Existing card is not available for this explicit recovery request')
            if not kb._parents_satisfied(conn,task_id):
                raise WorkflowError('Existing card is waiting for its predecessors')
            profile=kb._resolve_executable_assignee(profile)
            conn.execute("UPDATE tasks SET assignee=?,status='ready',body=?,workspace_kind=?,workspace_path=?,requires_repo=?,delivery_type=?,goal_mode=1,max_runtime_seconds=?,model_override=?,provider_override=?,reasoning_effort=? WHERE id=?",
                (profile,(prior.body or '')+'\n\nNFOS delivery request:\n'+body,
                 'worktree' if kind=='code' else prior.workspace_kind,
                 prior.workspace_path or (project.get('repo_path') if kind=='code' else None),
                 int(kind=='code'),kind,project.get('max_runtime_seconds',7200),project.get('model'),
                 project.get('provider'),project.get('reasoning_effort'),task_id))
        else:
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
        if kind=='code':
            # Existing scratch/report cards did not have a worktree tracking
            # row. It must exist in this same bootstrap transaction, before
            # the child materializes and records its workspace ownership.
            kb._insert_git_delivery_obligation(conn,task_id,int(time.time()))
        conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
        conn.execute("UPDATE nfos_requests SET status='attached',task_id=?,worker_pid=?,worker_started_at=? WHERE id=?",
                     (task_id,pid,started_at,request_id))
        kb.add_notify_sub(conn,task_id=task_id,platform=source['platform'],chat_id=source['chat_id'],
            thread_id=source['thread_id'],user_id=source.get('user_id'),chat_type=source.get('chat_type'),
            notifier_profile=source.get('profile') or profile,delivery_mode='notify+wake')
        _event(conn,task_id,task.current_run_id,
            'nfos_worker_recovered_card' if project.get('existing_task_id') else 'nfos_worker_created_card',
            {'request_id':request_id,'pid':pid,'worker_started_at':started_at})
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
        wf=get_workflow(conn,task_id)
        if spec.get('delivery_type')!=task.delivery_type:
            # The project default is provisional until TL has analyzed the
            # request. An audit must not inherit a Git delivery requirement.
            if wf['spec_revision'] or wf['stage']!='analysis' or spec.get('delivery_type') not in {'report','operation'}:
                raise WorkflowError('Spec delivery type must match the card after initial classification')
            conn.execute('UPDATE tasks SET delivery_type=?,requires_repo=0 WHERE id=?',
                         (spec['delivery_type'],task_id))
            conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
            _event(conn,task_id,run_id,'nfos_delivery_classified',
                   {'previous':task.delivery_type,'delivery_type':spec['delivery_type'],'author':author})
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


def _report_results(spec, report):
    rows=report.get('criteria',[])
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
        raise WorkflowError('Report criteria must be result objects')
    results={row.get('id'):row for row in rows}
    expected={c['id'] for c in json.loads(spec['content'])['criteria']}
    if len(results)!=len(rows) or set(results)!=expected:
        raise WorkflowError('Report must contain each spec criterion exactly once')
    if any(row.get('status') not in {'PASS','FAIL','NOT_RUN'} for row in rows):
        raise WorkflowError('Report results must use PASS, FAIL or NOT_RUN')
    if not str(report.get('summary') or '').strip():
        raise WorkflowError('Report needs its actual outcome')
    if any(row['status']=='PASS' and not row.get('evidence') for row in rows):
        raise WorkflowError('Passing criteria require linked evidence')
    return results


def _evidence_refs(value):
    if value is None:
        return []
    return value if isinstance(value,list) else [value]


def _evidence_locator(reference):
    if isinstance(reference,str) and reference.strip():
        return reference.strip()
    if isinstance(reference,dict):
        for key in ('path','local_path','file','url','href','uri','ref','artifact','id'):
            if isinstance(reference.get(key),str) and reference[key].strip():
                return reference[key].strip()
    raise WorkflowError('Evidence needs a file path, URL or declared artifact id')


def _evidence_location(reference, workspace):
    value=_evidence_locator(reference)
    parsed=urlsplit(value)
    if parsed.scheme and len(parsed.scheme)>1 and parsed.scheme!='file':
        return 'external',value
    if parsed.scheme=='file':
        value=unquote(parsed.path)
        if os.name=='nt' and len(value)>2 and value[0]=='/' and value[2]==':':
            value=value[1:]
    path=Path(value).expanduser()
    if not path.is_absolute():
        if not workspace:
            raise WorkflowError('Relative evidence needs the task workspace; use its absolute path')
        path=Path(workspace)/path
    return 'local',str(path.resolve(strict=False))


def _inspect_local_evidence(value):
    """Read real bytes outside the SQLite writer, detecting concurrent changes."""
    path=Path(value)
    try:
        before=path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise WorkflowError(f'Evidence is not an accessible regular file: {path}')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            opened=os.fstat(stream.fileno())
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
            after=os.fstat(stream.fileno())
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if identity(before)!=identity(opened) or identity(opened)!=identity(after) or identity(after)!=identity(path.stat()):
            raise WorkflowError(f'Evidence changed while being read: {path}')
        return {'path':str(path),'sha256':digest.hexdigest(),'size_bytes':after.st_size}
    except OSError as exc:
        raise WorkflowError(f'Local evidence is unavailable or inaccessible: {path}') from exc


def _report_artifact_checks(conn, task, spec, report):
    results=_report_results(spec,report)
    workspace=str(Path(task.workspace_path).resolve(strict=False)) if task.workspace_path else None
    originals=set()
    request=conn.execute('SELECT r.payload FROM nfos_workflows w JOIN nfos_requests r ON r.id=w.request_id WHERE w.task_id=?',(task.id,)).fetchone()
    if request:
        for attachment in json.loads(request['payload']).get('attachments',[]):
            if not isinstance(attachment,dict):
                continue
            for key in ('original','source_path','path','local_path'):
                if attachment.get(key):
                    try:
                        kind,path=_evidence_location(attachment[key],workspace)
                    except WorkflowError:
                        # An unused historical attachment may refer to another
                        # host. It cannot authorize a local cross-card path.
                        continue
                    if kind=='local':
                        originals.add(path)
    # Shared repository anchors are not exclusive task workspaces. Only a
    # concrete per-card directory proves that a path belongs to another card.
    other_workspaces=[Path(row['workspace_path']).resolve(strict=False) for row in
        conn.execute('SELECT id,workspace_path FROM tasks WHERE id!=? AND workspace_path IS NOT NULL',(task.id,))
        if Path(row['workspace_path']).name==row['id']]
    checks=[]; aliases={}
    for reference in _evidence_refs(report.get('artifacts')):
        locator=_evidence_locator(reference)
        kind,location=_evidence_location(reference,workspace)
        check={'ref':locator,'task_id':task.id,'run_id':task.current_run_id,
               'spec_revision':spec['revision'],'criteria':[],'checked_at':int(time.time())}
        if kind=='local':
            path=Path(location)
            if location not in originals and any(path.is_relative_to(root) for root in other_workspaces):
                raise WorkflowError('Evidence belongs to another task workspace and is not an input of this request')
            check.update(_inspect_local_evidence(location),status='verified_local')
        else:
            check.update(url=location,status='external_unchecked')
        index=len(checks);checks.append(check)
        keys=[locator,location]
        if isinstance(reference,dict) and reference.get('id'):
            keys.append(str(reference['id']))
        for key in keys:
            if key in aliases and aliases[key]!=index:
                prior=checks[aliases[key]]
                if prior.get('path',prior.get('url'))!=location:
                    raise WorkflowError('Artifact id links to different evidence locations')
            aliases[key]=index
    for criterion_id,row in results.items():
        for reference in _evidence_refs(row.get('evidence')):
            locator=_evidence_locator(reference)
            index=aliases.get(locator)
            if index is None:
                _,location=_evidence_location(reference,workspace)
                index=aliases.get(location)
            if index is None:
                raise WorkflowError(f'Criterion {criterion_id} evidence must link to a declared artifact')
            if criterion_id not in checks[index]['criteria']:
                checks[index]['criteria'].append(criterion_id)
    return checks


def save_report(conn, task_id, run_id, report):
    if conn.in_transaction:
        raise WorkflowError('Report evidence must be read outside a write transaction')
    task=_owned(conn,task_id,run_id)
    spec=get_spec(conn,task_id)
    if not spec:
        raise WorkflowError('No persisted spec')
    report=json.loads(_json(report))
    encoded=_json(report)
    checks=_report_artifact_checks(conn,task,spec,report)
    evidence={'spec_revision':spec['revision'],'report_sha256':hashlib.sha256(encoded.encode()).hexdigest(),
              'artifact_checks':checks,'schema_version':1}
    with _kb().write_txn(conn):
        current=_owned(conn,task_id,run_id)
        if get_spec(conn,task_id)['id']!=spec['id'] or current.workspace_path!=task.workspace_path:
            raise WorkflowError('Spec or workspace changed while evidence was checked; save the current report')
        previous=_artifact(conn,task_id,'report');revision=(previous['revision'] if previous else 0)+1
        conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (task_id,run_id,'report',revision,encoded,'worker',_json(evidence),int(time.time())))
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


def wait_decision(conn,decision_id,*,timeout=300):
    """Wait outside a transaction, without repeatedly invoking the model."""
    if conn.in_transaction:
        raise WorkflowError('Decision wait cannot hold a write transaction')
    deadline=time.monotonic()+max(0,float(timeout))
    while True:
        row=get_decision(conn,decision_id)
        if row is None:
            raise WorkflowError('Unknown decision')
        if row['status']!='pending' or time.monotonic()>=deadline:
            return row
        time.sleep(min(1,max(0,deadline-time.monotonic())))


def resume_after_answer(conn,task_id,*,answer,source):
    if not answer.strip() or not source:
        raise WorkflowError('Record the actual human answer and its source')
    with _kb().write_txn(conn):
        if not get_workflow(conn,task_id):
            raise WorkflowError('Unknown NFOS card')
        rows=conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='human'",(task_id,)).fetchall()
        if not rows:
            raise WorkflowError('No pending human question on this card')
        for row in rows:
            context=json.loads(row['context'])
            context['human_reply']={'answer':answer,'source':source,'author':source.get('actor') or 'Human','received_at':int(time.time())}
            conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?',(_json(context),row['id']))
        _event(conn,task_id,None,'nfos_human_answer_received',{'answer':answer,'source':source,'decisions':[r['id'] for r in rows]})
    return task_id in reconcile_human_answers(conn)


def reconcile_human_answers(conn):
    from hermes_cli.nfos_runtime import run_termination_pending
    """The same runtime tick retains human blocks and applies saved replies."""
    resumed=[]
    rows=conn.execute("SELECT * FROM nfos_decisions WHERE status='human' ORDER BY created_at,id").fetchall()
    for row in rows:
        if (_run_process_alive(conn,row['task_id'],row['run_id'])
                or run_termination_pending(conn,row['task_id'],row['run_id'])):
            continue
        task=_kb().get_task(conn,row['task_id'])
        if not task or task.status in {'done','archived'}:
            continue
        # A crash before kanban_block must not turn a human question into
        # an automatic execution retry at startup.
        if task.status in {'running','ready'}:
            _kb().block_task(conn,task.id,reason=row['answer'],kind='needs_input',
                             expected_run_id=task.current_run_id if task.status=='running' else None)
        reply=json.loads(row['context']).get('human_reply')
        if not reply:
            continue
        with _kb().write_txn(conn):
            current=get_decision(conn,row['id'])
            if current['status']!='human':
                continue
            if not _kb().unblock_task(conn,row['task_id']):
                continue
            conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer=?,author=?,resolved_at=? WHERE task_id=? AND status='human'",
                         (reply['answer'],reply.get('author','Human'),int(time.time()),row['task_id']))
            _event(conn,row['task_id'],None,'nfos_human_answered',reply)
            resumed.append(row['task_id'])
    return resumed


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
    if operation not in {'homolog','pr','merge','deploy'} or not target or not candidate:
        raise WorkflowError('External effect needs operation, destination and exact candidate')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        if task.delivery_type=='code':
            wf=get_workflow(conn,task_id);state=json.loads(wf['state_json'])
            if not wf['spec_revision']:
                raise WorkflowError('Persist the spec before changing an environment')
            if operation in {'merge','deploy'} and not _approved(conn,task_id,wf['spec_revision']):
                raise WorkflowError('Principal review of this candidate is pending')
            _project_owned(conn,task_id,run_id,candidate if operation=='homolog' else state.get('homolog_sha'))
            expected=candidate if operation=='homolog' else (state.get('integrated_sha') if operation=='deploy' else state.get('homolog_sha'))
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


def reconcile_effect(conn, effect_id, *, found, evidence, caller_task_id=None, caller_run_id=None):
    if not evidence or not evidence.get('readback'):
        raise WorkflowError('Reconciliation requires destination readback evidence')
    with _kb().write_txn(conn):
        effect=_row(conn,'nfos_effects','id',effect_id)
        if not effect:
            raise WorkflowError('Unknown effect')
        if caller_task_id is not None or caller_run_id is not None:
            if effect['task_id']!=caller_task_id:
                raise OwnershipConflict('This receipt belongs to another task')
            _owned(conn,caller_task_id,caller_run_id)
        if effect['status']=='confirmed' and not found:
            raise WorkflowError('Confirmed effect cannot be undone by an absent lookup')
        task=_kb().get_task(conn,effect['task_id'])
        if found and task.delivery_type=='code':
            wf=get_workflow(conn,task.id);state=json.loads(wf['state_json'])
            if evidence.get('candidate')!=effect['candidate']:
                raise WorkflowError('Destination readback must identify this exact candidate')
            if effect['operation']=='homolog':
                if not evidence.get('tree') or not evidence.get('artifact'):
                    raise WorkflowError('Read the actual homologation tree and deployed artifact')
                state['homolog_deployment']=evidence
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


def completion_evidence_check(conn, task_id):
    """Read evidence before completion's transaction; never perform network I/O."""
    if conn.in_transaction:
        raise WorkflowError('Completion evidence must be read outside the write transaction')
    report=_artifact(conn,task_id,'report')
    spec=get_spec(conn,task_id)
    if not report or not spec:
        return None
    try:
        content=json.loads(report['content']); metadata=json.loads(report['evidence'])
        results=_report_results(spec,content)
        if any(row['status']!='PASS' for row in results.values()):
            return None
        digest=hashlib.sha256(report['content'].encode()).hexdigest()
        if (metadata.get('schema_version')!=1 or metadata.get('report_sha256')!=digest
                or metadata.get('spec_revision')!=spec['revision']):
            return None
        proved=set()
        for check in metadata.get('artifact_checks',[]):
            if (check.get('task_id')!=task_id or check.get('run_id')!=report['run_id']
                    or check.get('spec_revision')!=spec['revision']):
                return None
            if check.get('status')=='verified_local':
                actual=_inspect_local_evidence(check['path'])
                if any(actual[key]!=check.get(key) for key in ('path','sha256','size_bytes')):
                    return None
                proved.update(check.get('criteria',[]))
            elif check.get('status')!='external_unchecked':
                return None
        if any(row['status']!='PASS' or criterion not in proved for criterion,row in results.items()):
            return None
        return {'report_id':report['id'],'report_revision':report['revision'],
                'report_sha256':digest,'spec_revision':spec['revision'],
                'evidence_sha256':hashlib.sha256(report['evidence'].encode()).hexdigest()}
    except (WorkflowError,KeyError,TypeError,ValueError):
        return None


def completion_ready(conn, task_id, *, evidence_check=None):
    wf=get_workflow(conn,task_id)
    if wf is None:
        return True
    report=_artifact(conn,task_id,'report')
    if not wf['spec_revision'] or not report or json.loads(report['evidence']).get('spec_revision')!=wf['spec_revision']:
        return False
    if evidence_check is None:
        if conn.in_transaction:
            return False
        evidence_check=completion_evidence_check(conn,task_id)
    if evidence_check!={'report_id':report['id'],'report_revision':report['revision'],
            'report_sha256':hashlib.sha256(report['content'].encode()).hexdigest(),
            'spec_revision':wf['spec_revision'],
            'evidence_sha256':hashlib.sha256(report['evidence'].encode()).hexdigest()}:
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


def _run_process_alive(conn, task_id, run_id):
    run=conn.execute('SELECT worker_pid FROM task_runs WHERE task_id=? AND id=?',(task_id,run_id)).fetchone()
    event=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind IN ('spawned','nfos_worker_created_card','nfos_worker_recovered_card') ORDER BY id DESC LIMIT 1",
                       (task_id,run_id)).fetchone()
    payload=json.loads(event['payload'] or '{}') if event else {}
    pid=payload.get('pid') or (run['worker_pid'] if run else None)
    expected=payload.get('worker_started_at')
    if not pid or not _kb()._pid_alive(pid):
        return False
    # An old run without a start identity remains occupied while its PID is
    # alive; modern runs distinguish PID reuse from their actual worker.
    return expected is None or _kb()._process_identity_matches(pid,expected)


def acquire_project(conn, project, task_id, run_id, candidate):
    with _kb().write_txn(conn):
        _owned(conn,task_id,run_id)
        current=_row(conn,'nfos_project_delivery','project',project)
        if current:
            if (current['task_id'],current['run_id'],current['candidate'])==(task_id,run_id,candidate):
                return True
            previous=conn.execute('SELECT ended_at FROM task_runs WHERE id=? AND task_id=?',
                                  (current['run_id'],current['task_id'])).fetchone()
            if not previous or not previous['ended_at'] or _run_process_alive(conn,current['task_id'],current['run_id']):
                return False
            unknown=conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('homolog','merge','deploy')",
                                 (current['task_id'],)).fetchone()
            # The replacement run may reconcile its own exact candidate.
            # Another candidate/task cannot overwrite an ambiguous delivery.
            if unknown and (current['task_id']!=task_id or current['candidate']!=candidate):
                return False
            conn.execute('DELETE FROM nfos_project_delivery WHERE project=?',(project,))
            _event(conn,task_id,run_id,'nfos_project_delivery_recovered',{'project':project,'previous':current})
        conn.execute('INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)',
                     (project,task_id,run_id,candidate,int(time.time())))
        _event(conn,task_id,run_id,'nfos_project_delivery_acquired',{'project':project,'candidate':candidate})
        return True


def release_project(conn, project, task_id, run_id):
    with _kb().write_txn(conn):
        if conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('homolog','merge','deploy')",(task_id,)).fetchone():
            raise WorkflowError('Read the unresolved homologation/merge/deploy destination before releasing publication')
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
        'pending','effect','reconcile','acquire-project','release-project','receive','resume','wait'])
    parser.add_argument('--task',default=os.environ.get('HERMES_KANBAN_TASK'))
    parser.add_argument('--run',type=int,default=int(os.environ.get('HERMES_KANBAN_RUN_ID') or 0))
    parser.add_argument('--input',help='JSON file with spec/report/state/question/receipt/request')
    parser.add_argument('--evidence',help='JSON file with the TL session/output and fallback reason when applicable')
    parser.add_argument('--author',default='Claude TL',choices=['Claude TL','Codex'])
    parser.add_argument('--stage')
    parser.add_argument('--next',dest='next_action',default='')
    parser.add_argument('--kind',choices=['review','impediment'])
    parser.add_argument('--decision')
    parser.add_argument('--timeout',type=float,default=300,help='Maximum wait duration; pending is not failure')
    parser.add_argument('--resolution',choices=['continue','approve','changes','human'])
    parser.add_argument('--operation',choices=['homolog','pr','merge','deploy'])
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
                'runtime':{'code_root':str(Path(__file__).resolve().parents[1]),'python':sys.executable},
                'report':_artifact(conn,args.task,'report'),
                'decisions':[dict(r) for r in conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? ORDER BY created_at',(args.task,))],
                'effects':[dict(r) for r in conn.execute('SELECT * FROM nfos_effects WHERE task_id=?',(args.task,))]}
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_tool_calls'").fetchone():
                from hermes_cli.nfos_tool import read_calls
                result['native_calls']=read_calls(conn,args.task)
        elif args.action=='save-spec':
            result={'revision':save_spec(conn,args.task,args.run,payload,author=args.author,evidence=evidence)}
        elif args.action=='save-report':
            save_report(conn,args.task,args.run,payload);result={'saved':True}
        elif args.action=='progress':
            advance(conn,args.task,args.run,args.stage,next_action=args.next_action,state=payload);result={'saved':True}
        elif args.action=='ask':
            context={k:v for k,v in payload.items() if k not in {'question','context'}}
            context.update(payload.get('context') or {})
            result={'decision_id':ask_principal(conn,args.task,args.run,kind=args.kind,
                question=payload['question'],context=context)}
        elif args.action=='pending':
            result=pending_decisions(conn)
        elif args.action=='wait':
            result=wait_decision(conn,args.decision,timeout=args.timeout)
        elif args.action=='resume':
            resumed=resume_after_answer(conn,args.task,answer=payload['answer'],source=payload['source'])
            result={'resumed':resumed,'answer_persisted':True,'waiting_for_previous_exit':not resumed}
        elif args.action=='decide':
            if os.environ.get('HERMES_KANBAN_TASK'):
                raise WorkflowError('The Principal resolves reviews in its own coordinator session')
            resolve_decision(conn,args.decision,action=args.resolution,answer=payload['answer'],author='Principal');result={'saved':True}
        elif args.action=='effect':
            result=begin_effect(conn,args.task,args.run,operation=args.operation,target=args.target,candidate=args.candidate)
        elif args.action=='reconcile':
            reconcile_effect(conn,args.effect_id,found=payload['found'],evidence=payload['evidence'],
                caller_task_id=args.task,caller_run_id=args.run);result={'saved':True}
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
