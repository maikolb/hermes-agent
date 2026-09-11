"""Durable delivery data in the canonical Kanban database.

Tasks/runs remain the lifecycle authority. Requests exist before their worker
creates a card. Every subsequent change and pending decision commits together
with a task event; there is no independent queue or lifecycle database.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def active_suspension(conn, task_id):
    """A Principal's human decision fences work until its supported resolution."""
    return conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND status='human' "
                        "ORDER BY resolved_at DESC,rowid DESC LIMIT 1", (task_id,)).fetchone()


def _owned(conn, task_id, run_id):
    if active_suspension(conn, task_id):
        raise OwnershipConflict('The Principal suspended this task; await its supported resumption')
    task=_kb().get_task(conn,task_id)
    if task is None or task.status!='running' or task.current_run_id!=run_id:
        raise OwnershipConflict('The task no longer belongs to this execution')
    return task


def _event(conn, task_id, run_id, kind, payload):
    _kb()._append_event(conn,task_id,kind,payload,run_id=run_id)


URGENT_PRIORITY = 100  # URGENT_20260910
HUMAN_REQUEST_PRIORITY = 10  # HUMAN_PRIORITY_20260910
_HUMAN_PLATFORMS = {'telegram', 'whatsapp', 'discord', 'slack', 'signal', 'email'}


def _intake_priority(payload):
    """HUMAN_PRIORITY_20260910 (ordem do Maikol): pedido nascido de mensagem humana entra acima de backlog promovido
    (priority 0); card retido/adotado de sessão antiga fica em 0; urgente continua 100."""
    if payload.get('urgent'):
        return URGENT_PRIORITY
    src = payload.get('source') or {}
    if str(src.get('message_identity_kind') or '') in ('retained-session-row', 'retained-card'):
        return 0
    if str(src.get('platform') or '').lower() in _HUMAN_PLATFORMS:
        return HUMAN_REQUEST_PRIORITY
    return 0


_URGENT_RE = re.compile(r"prioridade\s+m[áa]xima|\burgent[ei]\b|urg[êe]ncia|\basap\b|\bp0\b", re.I)
_REF_RE = re.compile(r"https?://[^\s<>\"')\]]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _references(text):
    return [t.rstrip('.,;:)') for t in _REF_RE.findall(text or '')]


def _open_task_for_references(conn, refs):
    for ref in refs:
        if len(ref) < 12:
            continue
        row = conn.execute(
            "SELECT id FROM tasks WHERE status NOT IN ('done','archived') AND task_role = 'work' "
            "AND (body LIKE ? OR title LIKE ?) ORDER BY created_at DESC LIMIT 1", ('%' + ref + '%', '%' + ref + '%')
        ).fetchone()
        if row:
            return row[0]
    return None


def _same_thread_request(conn, source, *, message_id=None, within=900, exclude=None):
    """Request anterior do mesmo platform/chat/thread: pelo message_id (reply) ou o mais recente em `within` s."""
    rows = conn.execute("SELECT id, task_id, payload, created_at FROM nfos_requests ORDER BY created_at DESC, id DESC LIMIT 200").fetchall()
    now = time.time()
    for r in rows:
        if exclude and r['id'] == exclude:
            continue
        try:
            p = json.loads(r['payload'] or '{}')
        except Exception:
            continue
        s = p.get('source') or {}
        if (s.get('platform'), str(s.get('chat_id')), str(s.get('thread_id'))) != (source.get('platform'), str(source.get('chat_id')), str(source.get('thread_id'))):
            continue
        if message_id is not None:
            if str(s.get('message_id')) == str(message_id):
                return r, p
            continue
        if str(s.get('message_id')) != str(source.get('message_id')) and now - float(r['created_at'] or 0) <= within:
            return r, p
    return None, None


def _urgent_intake(conn, request_id, source, text, reply_to_message_id, *, author):
    """URGENT_20260910 (ordem do Maikol): pedido urgente ou repetição de referência de card aberto anexa ao card
    existente (sem card novo) e, se urgente, sobe a prioridade e libera o card; devolve o que fez."""
    urgent = bool(_URGENT_RE.search(text or ''))
    refs = _references(text)
    target = None; how = None
    if reply_to_message_id:
        r, p = _same_thread_request(conn, source, message_id=reply_to_message_id)
        if r is not None:
            target = r['task_id'] or _open_task_for_references(conn, _references(p.get('text') or '')); how = 'reply'
    if target is None and refs:
        target = _open_task_for_references(conn, refs); how = 'same_reference'
    if target is None and urgent and not refs:
        r, p = _same_thread_request(conn, source, exclude=request_id)
        if r is not None:
            target = r['task_id'] or _open_task_for_references(conn, _references(p.get('text') or '')); how = 'previous_message'
    if target is None:
        return {'urgent': urgent}
    trow = conn.execute("SELECT status, priority, block_kind FROM tasks WHERE id = ?", (target,)).fetchone()
    if trow is None or trow['status'] in ('done', 'archived'):
        return {'urgent': urgent}
    conn.execute("UPDATE nfos_requests SET status='attached', task_id=? WHERE id=? AND status IN ('pending','coordinating')", (target, request_id))
    _kb().add_comment(conn, target, author, ('[prioridade máxima] ' if urgent else '[reenvio] ') + (text or '').strip()[:1500])
    _event(conn, target, None, 'request_attached', {'request_id': request_id, 'how': how, 'urgent': urgent})
    if urgent:
        conn.execute("UPDATE tasks SET priority = MAX(COALESCE(priority,0), ?) WHERE id = ?", (URGENT_PRIORITY, target))
        if trow['status'] == 'backlog' or (trow['status'] == 'blocked' and (trow['block_kind'] or '') in ('dependency', 'capability', '')):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=? AND status IN ('backlog','blocked')", (target,))
        _event(conn, target, None, 'priority_escalated', {'request_id': request_id, 'priority': URGENT_PRIORITY, 'how': how, 'previous_status': trow['status']})
    return {'urgent': urgent, 'attached_to': target, 'how': how}


def receive_request(conn, *, source, text, project, attachments=(), part='0', origin=None,
                    defer_to_principal=False, reply_to_message_id=None):
    required=('platform','chat_id','thread_id','message_id')
    if any(not str(source.get(k) or '').strip() for k in required):
        raise WorkflowError('A request needs its original platform/chat/topic/message identity')
    if not text.strip() and not attachments:
        raise WorkflowError('A request needs text or attachments')
    source_key=_json([str(source[k]) for k in required]+[str(part)])
    request_id='req_'+hashlib.sha256(source_key.encode()).hexdigest()[:24]
    payload={'source':source,'text':text,'project':project,'attachments':list(attachments)}
    if _URGENT_RE.search(text or ''):  # URGENT_20260910
        payload['urgent']=True
    if origin is not None:
        payload['origin']=origin
    if defer_to_principal:
        payload['coordination']={'reply_to_message_id':reply_to_message_id}
    payload=_json(payload)
    with _kb().write_txn(conn,allow_nested=True):
        conn.execute('INSERT OR IGNORE INTO nfos_requests(id,source_key,payload,status,created_at) VALUES(?,?,?,?,?)',
                     (request_id,source_key,payload,'coordinating' if defer_to_principal else 'pending',int(time.time())))
        try:  # URGENT_20260910: anexa a card aberto e/ou escala prioridade; nunca derruba o intake
            _author=(re.match(r'\s*\[([^|\]]+)\|', text or '') or [None, None])[1] or 'nfos-intake'
            _urgent_intake(conn, request_id, source, text, reply_to_message_id, author=_author)
        except Exception:
            pass
        if not defer_to_principal:
            # The Principal classifies the preserved message through the same
            # intake. Its original bytes/identity remain authoritative.
            conn.execute("UPDATE nfos_requests SET status='pending' WHERE id=? AND status='coordinating'",
                         (request_id,))
    return request_id


def _coordinator_receipt(conn, row):
    path=next(item[2] for item in conn.execute('PRAGMA database_list') if item[1]=='main')
    if not path:
        raise WorkflowError('Principal input requires a persistent board database')
    coordination=json.loads(row['payload']).get('coordination') or {}
    return {'kind':'nfos_coordinator_input','db_path':str(Path(path).resolve()),
            'request_id':row['id'],'delivery_id':'nfos-input:'+row['id'],
            'claim_token':coordination.get('claim_token')}


def record_coordinator_progress(conn, receipt, **progress):
    with _kb().write_txn(conn,allow_nested=True):
        row=get_request(conn,receipt['request_id'])
        if row is None:
            return
        payload=json.loads(row['payload']);coordination=payload.setdefault('coordination',{})
        if coordination.get('claim_token')!=receipt.get('claim_token'):
            raise OwnershipConflict('Principal input was claimed by another delivery attempt')
        for key in ('checkpoint_root','session_id','wake_accepted'):
            if key in progress:
                coordination[key]=progress[key]
        if coordination.get('wake_accepted'):
            coordination['claim_until']=0
            coordination['last_error']=None
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?',(_json(payload),row['id']))


def coordinator_wake_accepted(conn, receipt):
    """Same lost-ACK recovery as ordinary Kanban wakes, without a fake card."""
    row=get_request(conn,receipt['request_id'])
    if row is None or row['status']!='coordinating':
        return True
    coordination=json.loads(row['payload']).get('coordination') or {}
    if coordination.get('wake_accepted'):
        return True
    if coordination.get('checkpoint_root') and coordination.get('session_id'):
        from agent.turn_checkpoint import TurnCheckpointStore
        try:
            state=TurnCheckpointStore(coordination['checkpoint_root']).load(coordination['session_id'])
        except FileNotFoundError:
            return False
        saved=(state.get('routing') or {}).get('kanban_wake_delivery') or {}
        if (saved.get('delivery_id')==receipt['delivery_id']
                and saved.get('claim_token')==coordination.get('claim_token')
                and saved.get('db_path')==receipt['db_path']):
            record_coordinator_progress(conn,_coordinator_receipt(conn,row),wake_accepted=True)
            return True
    return False


def claim_coordinator_input(conn, request_id):
    row=get_request(conn,request_id)
    if row is None or coordinator_wake_accepted(conn,_coordinator_receipt(conn,row)):
        return None
    with _kb().write_txn(conn,allow_nested=True):
        row=get_request(conn,request_id)
        if row is None or row['status']!='coordinating':
            return None
        payload=json.loads(row['payload']);coordination=payload.setdefault('coordination',{})
        now=time.time()
        if coordination.get('wake_accepted') or max(coordination.get('claim_until',0),coordination.get('next_attempt_at',0))>now:
            return None
        coordination.update(claim_token=uuid.uuid4().hex,claim_until=now+60,
                            attempts=int(coordination.get('attempts',0))+1)
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?',(_json(payload),row['id']))
        return _coordinator_receipt(conn,get_request(conn,request_id))


def retry_coordinator_input(conn, receipt, *, error=None):
    with _kb().write_txn(conn,allow_nested=True):
        row=get_request(conn,receipt['request_id'])
        if row is None or row['status']!='coordinating':
            return
        payload=json.loads(row['payload']);coordination=payload.get('coordination') or {}
        if coordination.get('claim_token')!=receipt.get('claim_token') or coordination.get('wake_accepted'):
            return
        from agent.redact import redact_sensitive_text
        attempts=int(coordination.get('attempts',1))
        delay=60 if error is None else min(300,5*2**min(attempts-1,6))
        if attempts>=3:
            delay=max(delay,3600)  # patch local 10/09/2026 (Maikol): sem teto, a mesma mensagem era reinjetada a cada 5 s ate 16x por dia
        coordination.update(claim_until=0,next_attempt_at=time.time()+delay,
                            last_error=redact_sensitive_text(str(error),force=True,redact_url_credentials=True)[:500] if error else None)
        conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?',(_json(payload),row['id']))


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


# Telegram group intake hands over the adapter's attribution envelope, one
# ``[Sender|user_id]`` line followed by the message. The card title must show
# the request, never that envelope: on 09/09 the dovcrm and concursa-ai boards
# filled with cards titled ``[Maikol|996979567]`` and no request was readable.
_ATTRIBUTION_LINE=re.compile(r'^\[[^\]\n]*\|[^\]\n]*\]$')
_LEADING_MENTIONS=re.compile(r'^(?:@\w+\s*)+')
_URL=re.compile(r'https?://([^\s/]+)(/\S*)?')
REQUEST_TITLE_MAX=160


def request_card_title(text, attachments=()):
    """Title a request card with the request itself.

    Drops the attribution line(s) and the bot mention that triggered the
    intake, folds what remains into one line with links reduced to their host,
    and cuts at a word boundary. Attachment-only requests name the attachment
    kinds so the card still says what arrived.
    """
    lines=[line.strip() for line in str(text or '').splitlines()]
    while lines and (not lines[0] or _ATTRIBUTION_LINE.match(lines[0])):
        lines.pop(0)
    excerpt=_LEADING_MENTIONS.sub('',' '.join(line for line in lines if line))
    excerpt=_URL.sub(lambda m:m.group(1)+('/…' if (m.group(2) or '').strip('/') else ''),excerpt)
    excerpt=' '.join(excerpt.split())
    if not excerpt:
        kinds=sorted({str(item.get('mime_type') or '').split('/')[0] for item in attachments if isinstance(item,dict)}-{''})
        return 'Analyze attached request'+(' ('+', '.join(kinds)+')' if kinds else '')
    if len(excerpt)<=REQUEST_TITLE_MAX:
        return excerpt
    cut=excerpt.rfind(' ',0,REQUEST_TITLE_MAX)
    return excerpt[:cut if cut>REQUEST_TITLE_MAX//2 else REQUEST_TITLE_MAX-1].rstrip(' ,;:-')+'…'


def bootstrap_card(conn, request_id, token, *, pid):
    kb=_kb()
    # claim_task's observers must see the complete request/card/run mapping.
    # Flush them after the outer commit, including when bootstrap returns early.
    with kb.defer_kanban_lifecycle_hooks(), kb.write_txn(conn):
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
        if payload.get('origin'):
            body+='\n\nOriginal request lineage:\n'+_json(payload['origin'])
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
            conn.execute('UPDATE tasks SET priority=MAX(COALESCE(priority,0),?) WHERE id=?',(_intake_priority(payload),task_id))  # HUMAN_PRIORITY_20260910
        else:
            task_id=kb.create_task(conn,title=request_card_title(original,payload.get('attachments') or ()),
                body=body,assignee=profile,created_by='worker:'+profile,
                priority=_intake_priority(payload),  # HUMAN_PRIORITY_20260910
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


def _spec_matches_instruction(conn, task_id):
    task=_kb().get_task(conn,task_id)
    spec=get_spec(conn,task_id)
    if not task or not spec:
        return False
    wf=get_workflow(conn,task_id)
    if wf and wf['spec_revision'] < json.loads(wf['state_json']).get('classification_revision_floor',0):
        return False
    evidence=json.loads(spec['evidence'])
    if 'instruction_revision' in evidence or task.instruction_revision==0:
        return evidence.get('instruction_revision',0)==task.instruction_revision
    # Historical specs remain immutable. A Principal's explicit reconciliation
    # binds only the exact instruction and spec it inspected, never later edits.
    identity=_legacy_spec_identity(task,spec)
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_legacy_spec_bound' ORDER BY id DESC",(task_id,)):
        if json.loads(row['payload']).get('identity')==identity:
            return True
    return False


def _legacy_spec_identity(task,spec):
    return {'spec_id':spec['id'],'spec_sha256':hashlib.sha256(spec['content'].encode()).hexdigest(),
            'instruction_revision':task.instruction_revision,
            'instruction_sha256':hashlib.sha256(_json({'title':task.title,'body':task.body}).encode()).hexdigest()}


def reconcile_legacy_spec(conn,task_id,*,spec_id,spec_sha256,instruction_revision,
                          instruction_sha256,reason,evidence,author):
    """Record a reviewed migration binding; do not alter spec, review or effects."""
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise WorkflowError('The Principal reconciles legacy metadata outside the worker session')
    if not reason or not author or not isinstance(evidence,list) or not evidence:
        raise WorkflowError('Record the reconciliation reason, author and inspected local evidence')
    if conn.in_transaction:
        raise WorkflowError('Reconciliation evidence must be read outside a write transaction')
    checks=[_inspect_local_evidence(str(Path(ref).resolve())) for ref in evidence]
    expected={'spec_id':spec_id,'spec_sha256':spec_sha256,'instruction_revision':instruction_revision,
              'instruction_sha256':instruction_sha256}
    with _kb().write_txn(conn):
        task=_kb().get_task(conn,task_id);spec=get_spec(conn,task_id)
        if not task or not spec or _legacy_spec_identity(task,spec)!=expected:
            raise WorkflowError('Spec or instruction changed; read and review the current identity')
        if 'instruction_revision' in json.loads(spec['evidence']):
            raise WorkflowError('Only a legacy spec missing its instruction binding can be reconciled')
        for row in conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='nfos_legacy_spec_bound' ORDER BY id DESC",(task_id,)):
            if json.loads(row['payload']).get('identity')==expected:
                return {'event_id':row['id'],'identity':expected}
        _event(conn,task_id,None,'nfos_legacy_spec_bound',
               {'identity':expected,'reason':reason,'author':author,'evidence':checks})
        event_id=conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='nfos_legacy_spec_bound'",(task_id,)).fetchone()[0]
        return {'event_id':event_id,'identity':expected}


def _require_current_instruction_spec(conn, task_id):
    if not _spec_matches_instruction(conn,task_id):
        raise WorkflowError('Card instructions changed; read them and persist the updated spec before continuing delivery')


def record_precheck(conn, task_id, run_id, payload):
    """BLOCK_LESS7_20260910: registro do que foi lido em produção/HML/PRs antes de qualquer spec ou implementação."""
    checked=payload.get('checked') if isinstance(payload,dict) else None
    verdict=str((payload or {}).get('verdict') or '').strip()
    if (not isinstance(checked,list) or not checked or any(not isinstance(c,dict) or not str(c.get('target') or '').strip()
            or not str(c.get('result') or '').strip() for c in checked)):
        raise WorkflowError('precheck needs checked=[{target,method,result}] with what was actually read in production, HML or PRs')
    if verdict not in {'already_delivered','partial','not_delivered'}:
        raise WorkflowError('precheck verdict must be already_delivered, partial or not_delivered')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        wf=get_workflow(conn,task_id)
        if wf is None:
            raise WorkflowError('Unknown NFOS card')
        state=json.loads(wf['state_json'] or '{}')
        record={'checked':checked,'verdict':verdict,'delta':str(payload.get('delta') or ''),
                'instruction_revision':task.instruction_revision,'run_id':run_id,'recorded_at':int(time.time())}
        state['production_precheck']=record
        nxt={'already_delivered':'Already delivered: save a short report (criteria PASS with the readback as evidence) and call kanban_complete; do not re-implement',
             'partial':'Partially delivered: write the spec for the delta only, then implement',
             'not_delivered':'Not delivered: write the spec and implement'}[verdict]
        conn.execute('UPDATE nfos_workflows SET state_json=?,next_action=?,updated_at=? WHERE task_id=?',(_json(state),nxt,int(time.time()),task_id))
        _event(conn,task_id,run_id,'nfos_precheck',record)
        return record


SPEC_SIZE_BUDGET = {'P': 2700, 'M': 7200, 'G': 14400}  # BLOCK_LESS9_20260910: P 45 min, M 2 h, G 4 h (SOUL v3 seção 8)


# ---------------------------------------------------------------------------------------------------------------------
# RESULT_PROBE_20260911: critério obrigatório de resultado, medido no destino pela mesma sonda que reproduziu a reclamação.
# PASS|FAIL|INDETERMINADO vêm da medição, não do relatório. FAIL devolve a nota de debug ao mesmo card.
PROBE_KINDS = {'sql', 'http', 'header'}
_PROBE_EXPECT_KEYS = {'row', 'scalar', 'op', 'value', 'set_equals', 'contains_all', 'not_matches', 'count_between', 'equals', 'status'}
_RELEVANT_MUTATIONS = ('merge', 'deploy', 'homolog', 'repair')


def _norm_text(value):
    import unicodedata
    text = unicodedata.normalize('NFKD', str(value if value is not None else ''))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', text).strip().casefold()


def _probe_project(conn, task_id):
    """(slug, config) do projeto do card; probe_env vem de kanban.delivery.projects.<slug>.probe_env."""
    try:
        wf = get_workflow(conn, task_id); request = get_request(conn, wf['request_id'])
        project = json.loads(request['payload'])['project']
        key = project.get('board') or project.get('project_id')
        from hermes_cli.nfos_runtime import project_config
        return key, (project_config(key) or {})
    except Exception:
        return None, {}


def _probe_env_file(conn, task_id):
    key, cfg = _probe_project(conn, task_id)
    name = str(cfg.get('probe_env') or '').strip()
    if not name or name.startswith('/') or '..' in name:
        return None
    home = os.environ.get('HERMES_HOME') or str(Path.home() / '.hermes')
    path = Path(home) / 'secrets' / name
    return path if path.is_file() else None


def _env_values(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[7:]
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _validate_probe(cid, probe):
    if not isinstance(probe, dict) or probe.get('kind') not in PROBE_KINDS:
        raise WorkflowError(f'Criterion {cid}: a mandatory criterion needs probe {{kind: sql|http|header, ..., expect}}')
    expect = probe.get('expect')
    if not isinstance(expect, dict) or not (set(expect) & _PROBE_EXPECT_KEYS):
        raise WorkflowError(f'Criterion {cid}: probe.expect must state the requested result (row, scalar, op+value, set_equals, '
                            'contains_all, not_matches, count_between, equals or status); a count alone rarely proves content')
    kind = probe['kind']
    if kind == 'sql':
        query = str(probe.get('query') or '').strip()
        if not re.match(r'(?is)^(select|with)\b', query) or ';' in query.rstrip(';'):
            raise WorkflowError(f'Criterion {cid}: sql probe must be a single SELECT/WITH statement (read-only)')
    else:
        if not re.match(r'^https?://', str(probe.get('url') or '')):
            raise WorkflowError(f'Criterion {cid}: {kind} probe needs an http(s) url')
        if kind == 'header' and not str(probe.get('header') or '').strip():
            raise WorkflowError(f'Criterion {cid}: header probe needs the header name')


def _probe_expect_ok(expect, observed):
    rows = observed.get('rows')
    col = [r[0] if isinstance(r, (list, tuple)) and r else None for r in (rows or [])]
    if 'status' in expect and observed.get('status') != expect['status']:
        return False
    if 'row' in expect:
        return bool(rows) and list(rows[0]) == list(expect['row'])
    if 'scalar' in expect:
        return bool(rows) and rows[0][0] == expect['scalar']
    if 'op' in expect:
        if not rows:
            return False
        left, right = rows[0][0], expect.get('value')
        try:
            return {'==': left == right, '!=': left != right, '>=': left >= right, '<=': left <= right,
                    '>': left > right, '<': left < right}.get(str(expect['op']), False)
        except TypeError:
            return False
    forbidden = expect.get('not_matches')
    if forbidden and any(re.search(forbidden, str(x or ''), re.I) for x in col):
        return False
    if 'set_equals' in expect:
        return {_norm_text(x) for x in col} == {_norm_text(x) for x in expect['set_equals']}
    if 'contains_all' in expect:
        have = {_norm_text(x) for x in col}
        return all(_norm_text(x) in have for x in expect['contains_all'])
    if 'count_between' in expect:
        low, high = expect['count_between']
        return low <= len(rows or []) <= high
    if 'equals' in expect:
        return _norm_text(observed.get('value')) == _norm_text(expect['equals'])
    return 'status' in expect or forbidden is not None


def _json_safe(value):
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _run_sql_probe(dsn, query):
    if dsn.startswith('sqlite://'):
        path = dsn[len('sqlite://'):]
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            rows = conn.execute(query).fetchmany(200)
        finally:
            conn.close()
        return [list(r) for r in rows]
    try:
        import psycopg2
    except ImportError as exc:
        raise WorkflowError('psycopg2 is not installed in this runtime (pip install psycopg2-binary in the release venv)') from exc
    conn = psycopg2.connect(dsn, connect_timeout=15, options='-c default_transaction_read_only=on -c statement_timeout=15000')
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor(); cur.execute(query); rows = cur.fetchmany(200)
    finally:
        conn.close()
    return [list(r) for r in rows]


def _run_http_probe(probe, env):
    import urllib.request, urllib.error
    headers = {}
    for key, value in (probe.get('headers') or {}).items():
        value = str(value)
        headers[str(key)] = env.get(value[5:], '') if value.startswith('$env:') else value
    method = 'HEAD' if probe['kind'] == 'header' else 'GET'
    req = urllib.request.Request(probe['url'], headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, hdrs = resp.status, dict(resp.headers)
            body = resp.read(200000) if method == 'GET' else b''
    except urllib.error.HTTPError as exc:
        status, hdrs = exc.code, dict(exc.headers or {})
        body = exc.read(200000) if method == 'GET' else b''
    if probe['kind'] == 'header':
        name = str(probe['header']).lower()
        return {'status': status, 'value': next((v for k, v in hdrs.items() if k.lower() == name), None)}
    observed = {'status': status}
    text = body.decode('utf-8', 'replace')
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if probe.get('json_path') and data is not None:
        node = data
        for part in [p for p in str(probe['json_path']).split('.') if p]:
            if isinstance(node, list) and part.isdigit():
                node = node[int(part)] if int(part) < len(node) else None
            elif isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        observed['value'] = _json_safe(node)
        if isinstance(node, list):
            observed['rows'] = [[x.get(probe.get('name_key') or 'name') if isinstance(x, dict) else x] for x in node][:200]
    else:
        observed['value'] = text[:2000]
    return observed


def _last_candidate(conn, task_id):
    row = conn.execute("SELECT candidate FROM nfos_effects WHERE task_id=? AND status='confirmed' AND operation IN ('homolog','deploy') "
                       "ORDER BY updated_at DESC LIMIT 1", (task_id,)).fetchone()
    return row[0] if row else None


def _relevant_mutation(conn, task_id):
    row = conn.execute("SELECT id,operation,target,updated_at FROM nfos_effects WHERE task_id=? AND status='confirmed' AND operation IN "
                       "('merge','deploy','homolog','repair') ORDER BY updated_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
    return dict(row) if row else None


def _same_mutation(recorded, current):
    """A medição vale enquanto a última mutação relevante for a mesma vista na hora de medir (identidade, não relógio)."""
    recorded = recorded or {}; current = current or {}
    return recorded.get('id') == current.get('id') and recorded.get('updated_at') == current.get('updated_at')


def _execute_probe(conn, task_id, probe):
    """Fora de transação. Devolve (state, observed, error)."""
    kind = probe['kind']
    env_file = _probe_env_file(conn, task_id)
    env = _env_values(env_file) if env_file else {}
    try:
        if kind == 'sql':
            dsn = env.get('PROBE_DATABASE_URL') or env.get('DATABASE_URL')
            if not dsn:
                return 'INDETERMINADO', None, 'probe_env of the project has no PROBE_DATABASE_URL/DATABASE_URL (config kanban.delivery.projects.<slug>.probe_env)'
            observed = {'rows': _json_safe(_run_sql_probe(dsn, str(probe['query']).strip().rstrip(';')))}
        else:
            observed = _run_http_probe(probe, env)
            if observed.get('status') in (401, 403) and 'status' not in (probe.get('expect') or {}):
                return 'INDETERMINADO', observed, f"HTTP {observed['status']}: access to the consumer surface was refused"
    except WorkflowError as exc:
        return 'INDETERMINADO', None, str(exc)
    except Exception as exc:  # timeout, rede, sintaxe: medição indisponível, não defeito
        return 'INDETERMINADO', None, f'{type(exc).__name__}: {str(exc).splitlines()[0][:160]}'
    expect = dict(probe.get('expect') or {})
    if expect.get('equals') == '$candidate':
        expect['equals'] = _last_candidate(conn, task_id) or ''
    return ('PASS' if _probe_expect_ok(expect, observed) else 'FAIL'), observed, None


def run_probes(conn, task_id, run_id, *, criterion=None, all_=False):
    if conn.in_transaction:
        raise WorkflowError('probe runs outside a write transaction')
    task = _owned(conn, task_id, run_id) if run_id else _kb().get_task(conn, task_id)
    if task is None:
        raise WorkflowError('Unknown task')
    spec = get_spec(conn, task_id)
    if not spec:
        raise WorkflowError('Persist the spec before measuring')
    criteria = json.loads(spec['content']).get('criteria') or []
    targets = [c for c in criteria if isinstance(c.get('probe'), dict) and (all_ or c.get('id') == criterion)]
    if not targets:
        raise WorkflowError('No criterion with a probe matches; pass --criterion <id> or --all')
    results = []
    for crit in targets:
        cid = crit['id']; probe = crit['probe']
        state, observed, error = _execute_probe(conn, task_id, probe)
        shown = {k: v for k, v in probe.items() if k != 'headers'}
        record = {'criterion': cid, 'text': crit.get('text'), 'probe': shown, 'expected': probe.get('expect'),
                  'observed': _json_safe(observed), 'state': state, 'error': error, 'ran_at': int(time.time()),
                  'spec_revision': spec['revision'], 'run_id': run_id, 'mutation': _relevant_mutation(conn, task_id)}
        with _kb().write_txn(conn):
            prev = _artifact(conn, task_id, f'probe:{cid}'); revision = (prev['revision'] if prev else 0) + 1
            conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         (task_id, run_id, f'probe:{cid}', revision, _json(record), 'runtime', _json({'kind': probe['kind']}), record['ran_at']))
            if state == 'PASS' and prev:  # LEARNING_20260911: FAIL -> PASS vira hipótese com a mudança registrada
                _capture_hypothesis(conn, task_id, run_id, spec, crit, prev, record, revision)
            wf = get_workflow(conn, task_id)
            if wf is not None:
                st = json.loads(wf['state_json'] or '{}') or {}
                st.setdefault('measurements', {})[cid] = {'state': state, 'ran_at': record['ran_at'], 'revision': revision,
                                                          'spec_revision': spec['revision'], 'error': error}
                if state == 'FAIL':
                    attempts = list(st.get('attempts') or [])
                    attempts.append({'at': record['ran_at'], 'run_id': run_id, 'criterion': cid, 'observed': _json_safe(observed)})
                    st['attempts'] = attempts[-30:]
                conn.execute('UPDATE nfos_workflows SET state_json=?,updated_at=? WHERE task_id=?', (_json(st), int(time.time()), task_id))
            _event(conn, task_id, run_id, 'nfos_probe', {'criterion': cid, 'state': state, 'revision': revision, 'error': error})
        results.append({'criterion': cid, 'state': state, 'observed': record['observed'], 'expected': record['expected'],
                        'error': error, 'revision': revision})
    return results


def _mandatory_effective(conn, task_id, spec):
    """Status efetivo dos critérios obrigatórios pela última medição válida."""
    criteria = json.loads(spec['content']).get('criteria') or []
    mutation = _relevant_mutation(conn, task_id)
    out = {}
    for crit in criteria:
        if not crit.get('mandatory'):
            continue
        cid = crit['id']
        art = _artifact(conn, task_id, f'probe:{cid}')
        info = {'criterion': cid, 'text': crit.get('text'), 'expected': (crit.get('probe') or {}).get('expect')}
        if not art:
            out[cid] = dict(info, status='NOT_RUN', why='measurement not executed (run `probe --criterion %s`)' % cid); continue
        m = json.loads(art['content'])
        info.update(observed=m.get('observed'), state=m.get('state'), ran_at=m.get('ran_at'), revision=art['revision'], error=m.get('error'))
        if m.get('spec_revision') != spec['revision']:
            out[cid] = dict(info, status='NOT_RUN', why='measurement belongs to an earlier spec revision; run the probe again')
        elif not _same_mutation(m.get('mutation'), mutation):
            out[cid] = dict(info, status='NOT_RUN', why="measurement predates the last relevant mutation (%s %s); run the probe again"
                            % ((mutation or {}).get('operation'), (mutation or {}).get('target')))
        elif m.get('state') == 'PASS':
            out[cid] = dict(info, status='PASS', why='')
        elif m.get('state') == 'FAIL':
            out[cid] = dict(info, status='FAIL', why='measured result differs from the requested result')
        else:
            out[cid] = dict(info, status='NOT_RUN', why='measurement indeterminate: ' + str(m.get('error') or ''))
    return out


def mandatory_pending(conn, task_id, spec=None):
    spec = spec or get_spec(conn, task_id)
    if not spec:
        return []
    return [e for e in _mandatory_effective(conn, task_id, spec).values() if e['status'] != 'PASS']


def _apply_measurements_to_report(conn, task_id, spec, report):
    """Owner mode: status de critério obrigatório vem da medição; reclassificação não é aceita."""
    effective = _mandatory_effective(conn, task_id, spec)
    if not effective:
        return
    reclassified = {r.get('id') for r in (report.get('reclassified') or []) if isinstance(r, dict)}
    artifacts = report.setdefault('artifacts', [])
    measurements = {}
    for row in report.get('criteria') or []:
        cid = row.get('id')
        if cid not in effective:
            continue
        if cid in reclassified:
            raise WorkflowError(f'Criterion {cid} is mandatory: its status comes from the measurement, not from reclassification')
        eff = effective[cid]
        if row.get('status') != eff['status']:
            row['declared_status'] = row.get('status')
        row['status'] = eff['status']
        row['measurement'] = {k: eff.get(k) for k in ('state', 'ran_at', 'revision', 'why', 'error')}
        if eff['status'] == 'PASS':
            aid = f'probe:{cid}'
            if not any(isinstance(a, dict) and a.get('id') == aid for a in artifacts):
                artifacts.append({'id': aid, 'path': f"kanban://nfos_artifacts/{aid}/r{eff.get('revision')}"})
            refs = [r for r in _evidence_refs(row.get('evidence')) if r]
            if aid not in refs:
                refs.append(aid)
            row['evidence'] = refs
        measurements[cid] = {k: eff.get(k) for k in ('status', 'state', 'ran_at', 'revision', 'why', 'observed', 'expected')}
    report['measurements'] = measurements


def _check_probe_corrections(previous, spec):
    """Sonda de critério obrigatório muda só com probe_corrections [{id, reason, evidence}] (histórico na revisão da spec)."""
    if not previous:
        return
    try:
        old = {c.get('id'): c for c in (json.loads(previous['content']).get('criteria') or [])}
    except Exception:
        return
    corrections = {c.get('id'): c for c in (spec.get('probe_corrections') or []) if isinstance(c, dict)}
    for crit in spec.get('criteria') or []:
        cid = crit.get('id'); before = old.get(cid) or {}
        if not before.get('mandatory') or not before.get('probe'):
            continue
        if _json(before.get('probe')) == _json(crit.get('probe')) and bool(crit.get('mandatory')) == bool(before.get('mandatory')):
            continue
        corr = corrections.get(cid)
        if not corr or not str(corr.get('reason') or '').strip() or not corr.get('evidence'):
            raise WorkflowError(f'Criterion {cid}: changing or dropping the probe of a mandatory criterion needs probe_corrections '
                                '[{id, reason, evidence:[...]}] in the new spec revision; the earlier measurement stays in history')


def _spec_result_criteria_checks(conn, task_id, spec):
    """Owner mode: valida sondas e exige critério obrigatório quando a reprodução foi medida."""
    for crit in spec.get('criteria') or []:
        if crit.get('probe') is not None or crit.get('mandatory'):
            _validate_probe(crit.get('id'), crit.get('probe'))
    _check_probe_corrections(get_spec(conn, task_id), spec)
    wf = get_workflow(conn, task_id)
    precheck = (json.loads(wf['state_json'] or '{}') or {}).get('production_precheck') if wf else None
    precheck = precheck or {}
    measured = any(str(c.get('method') or '').strip().lower() in {'sql', 'http', 'header', 'api', 'query'} for c in (precheck.get('checked') or []) if isinstance(c, dict))
    if (spec.get('delivery_type') in {'code', 'operation'} and precheck.get('verdict') in {'partial', 'not_delivered'} and measured
            and not any(c.get('mandatory') for c in spec.get('criteria') or [])):
        raise WorkflowError('The precheck measured the complaint (method sql/http): declare at least one criterion with mandatory=true and a '
                            'probe that measures the requested result on the surface the user consumes')


def completion_refusal_note(conn, task_id):
    """Nota de debug devolvida pelo kanban_complete quando um critério obrigatório não mediu PASS (owner mode)."""
    if not _owner_mode():
        return None
    spec = get_spec(conn, task_id)
    if not spec or get_workflow(conn, task_id) is None:
        return None
    pending = mandatory_pending(conn, task_id, spec)
    if not pending:
        return None
    wf = get_workflow(conn, task_id); st = json.loads(wf['state_json'] or '{}') or {}
    mutation = _relevant_mutation(conn, task_id)
    lines = ['Fechamento recusado: critério obrigatório sem medição PASS; o pedido não está resolvido no destino.']
    for e in pending:
        ran = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(e['ran_at'])) if e.get('ran_at') else 'não medido'
        lines.append(f"- {e['criterion']}: {e.get('text')}\n  esperado: {_json(e.get('expected'))[:400]}\n  observado: {_json(e.get('observed'))[:600]} ({e.get('state') or 'sem medição'}, {ran})\n  motivo: {e['why']}")
    if mutation:
        lines.append(f"Última mutação relevante: {mutation['operation']} {mutation['target']} em {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(int(mutation['updated_at'])))}.")
    else:
        lines.append('Nenhuma mutação relevante registrada ainda (deploy, homolog, merge ou effect --operation repair).')
    attempts = [a for a in (st.get('attempts') or []) if a.get('hypothesis') or a.get('change')]
    if attempts:
        last = attempts[-1]
        lines.append(f"Tentativas registradas: {len(attempts)}; última: hipótese '{str(last.get('hypothesis') or '')[:200]}', mudança '{str(last.get('change') or '')[:200]}'.")
    lines.append('Continue neste card, mesma prioridade: (1) registre hipótese e mudança delimitada: `progress --stage implement --next "..." '
                 '--hypothesis "..." --change "..."`; (2) aplique uma mudança e registre a mutação (`effect --operation repair|deploy|homolog ...` e '
                 '`reconcile`); (3) `probe --criterion <id>`; (4) `save-report` e kanban_complete. Não reclassifique, não crie card, não repita efeito '
                 'confirmado. Sonda errada: nova revisão da spec com probe_corrections [{id, reason, evidence}].')
    return '\n'.join(lines)


def record_completion_refusal(conn, task_id, note):
    pending = [e['criterion'] for e in mandatory_pending(conn, task_id)]
    with _kb().write_txn(conn):
        task = _kb().get_task(conn, task_id)
        _event(conn, task_id, task.current_run_id if task else None, 'nfos_completion_refused', {'pending': pending, 'note': note[:1500]})
        conn.execute('UPDATE nfos_workflows SET next_action=?,updated_at=? WHERE task_id=?',
                     ('Completion refused: mandatory criteria without PASS measurement (' + ', '.join(pending) + '). Continue in this card: hypothesis, one change, effect, probe, report.', int(time.time()), task_id))


# ---------------------------------------------------------------------------------------------------------------------
# LEARNING_20260911: tentativas capturadas, aprendizado aplicável recuperado na montagem do contexto, promoção criteriosa.
def _lessons_table(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_lessons'").fetchone())


def _ensure_lessons(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS nfos_lessons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, criterion TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('hypothesis','proven','retired')),
        component TEXT, operation TEXT, condition TEXT, text TEXT NOT NULL, evidence TEXT, "limit" TEXT, author TEXT,
        created_at INTEGER NOT NULL, promoted_at INTEGER, promoted_by TEXT, promotion_reason TEXT, superseded_by INTEGER)""")


def _probe_component(probe):
    """Tabelas (sql) ou caminho (http/header) tocados pela sonda: chave de aplicabilidade das lições."""
    if not isinstance(probe, dict):
        return None
    if probe.get('kind') == 'sql':
        found = re.findall(r'\b(?:from|join)\s+([A-Za-z_][\w.]*)', str(probe.get('query') or ''), re.I)
        return ','.join(sorted({x.lower() for x in found})) or None
    match = re.match(r'https?://[^/]+(/[^?#]*)', str(probe.get('url') or ''))
    return (match.group(1) if match else str(probe.get('url') or ''))[:120] or None


def record_lesson(conn, *, kind, text, task_id=None, criterion=None, component=None, operation=None, condition=None,
                  evidence=None, limit=None, author='runtime'):
    if kind not in {'hypothesis', 'proven'}:
        raise WorkflowError('lesson kind must be hypothesis or proven')
    if not str(text or '').strip():
        raise WorkflowError('lesson needs text')
    if kind == 'proven' and (not evidence or not str(condition or '').strip()):
        raise WorkflowError('a proven lesson needs the condition where it applies and evidence')
    with _kb().write_txn(conn):
        _ensure_lessons(conn)
        cur = conn.execute('INSERT INTO nfos_lessons(task_id,criterion,kind,component,operation,condition,text,evidence,"limit",author,created_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (task_id, criterion, kind, component, operation, condition, str(text).strip(), _json(list(evidence or [])), limit, author, int(time.time())))
        lesson_id = cur.lastrowid
        if task_id:
            _event(conn, task_id, None, 'nfos_lesson_recorded', {'lesson_id': lesson_id, 'kind': kind, 'component': component})
        return lesson_id


def promote_lesson(conn, lesson_id, *, reason, evidence, author):
    if not str(reason or '').strip() or not evidence:
        raise WorkflowError('promotion needs the reason and the evidence that sustains the conclusion')
    with _kb().write_txn(conn):
        _ensure_lessons(conn)
        row = conn.execute('SELECT * FROM nfos_lessons WHERE id=?', (int(lesson_id),)).fetchone()
        if not row:
            raise WorkflowError('Unknown lesson')
        merged = list(json.loads(row['evidence'] or '[]')) + [e for e in evidence if e not in json.loads(row['evidence'] or '[]')]
        conn.execute("UPDATE nfos_lessons SET kind='proven',promoted_at=?,promoted_by=?,promotion_reason=?,evidence=? WHERE id=?",
                     (int(time.time()), author, str(reason).strip(), _json(merged), int(lesson_id)))


def retire_lesson(conn, lesson_id, *, reason, author):
    if not str(reason or '').strip():
        raise WorkflowError('retiring a lesson needs the reason')
    with _kb().write_txn(conn):
        _ensure_lessons(conn)
        if not conn.execute('SELECT 1 FROM nfos_lessons WHERE id=?', (int(lesson_id),)).fetchone():
            raise WorkflowError('Unknown lesson')
        conn.execute("UPDATE nfos_lessons SET kind='retired',promoted_at=?,promoted_by=?,promotion_reason=? WHERE id=?",
                     (int(time.time()), author, str(reason).strip(), int(lesson_id)))


def list_lessons(conn, *, include_retired=False):
    if not _lessons_table(conn):
        return []
    rows = conn.execute('SELECT * FROM nfos_lessons ORDER BY id DESC LIMIT 200').fetchall()
    return [dict(r) for r in rows if include_retired or r['kind'] != 'retired']


def _capture_hypothesis(conn, task_id, run_id, spec, crit, previous, record, revision):
    """Dentro da transação da medição: FAIL -> PASS na mesma revisão vira hipótese com o que mudou no meio."""
    try:
        before = json.loads(previous['content'])
    except Exception:
        return
    if before.get('state') != 'FAIL' or before.get('spec_revision') != spec['revision']:
        return
    wf = get_workflow(conn, task_id); st = json.loads(wf['state_json'] or '{}') if wf else {}
    tried = [a for a in (st.get('attempts') or []) if (a.get('hypothesis') or a.get('change')) and int(a.get('at') or 0) >= int(before.get('ran_at') or 0)]
    last = tried[-1] if tried else {}
    mutation = record.get('mutation') or {}
    text = (f"{crit['id']} ({str(crit.get('text') or '')[:120]}): antes {_json(before.get('observed'))[:200]}; "
            f"depois de '{str(last.get('change') or 'mudança não registrada')[:200]}' "
            f"(hipótese: '{str(last.get('hypothesis') or 'não registrada')[:200]}')"
            + (f" via {mutation.get('operation')} em {str(mutation.get('target') or '')[:80]}" if mutation else '')
            + ", a medição passou.")
    _ensure_lessons(conn)
    goal = ''
    try:
        goal = str(json.loads(spec['content']).get('goal') or '')[:200]
    except Exception:
        pass
    cur = conn.execute('INSERT INTO nfos_lessons(task_id,criterion,kind,component,operation,condition,text,evidence,"limit",author,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, crit['id'], 'hypothesis', _probe_component(crit.get('probe')), mutation.get('operation'), goal, text,
                        _json([f"probe:{crit['id']} r{previous['revision']}", f"probe:{crit['id']} r{revision}"] + ([mutation.get('id')] if mutation.get('id') else [])),
                        'Uma passagem FAIL para PASS não prova a causa; confirmar em outro caso antes de promover.', 'runtime', int(time.time())))
    _event(conn, task_id, run_id, 'nfos_lesson_recorded', {'lesson_id': cur.lastrowid, 'kind': 'hypothesis', 'criterion': crit['id']})


def case_context(conn, task_id):
    """Mesmo pedido: medições, tentativas, próxima ação e nota de debug, para toda retomada."""
    wf = get_workflow(conn, task_id)
    if not wf:
        return ''
    st = json.loads(wf['state_json'] or '{}') or {}
    parts = []
    measurements = st.get('measurements') or {}
    if measurements:
        parts.append('Measurements so far: ' + '; '.join(
            f"{cid}={m.get('state')} ({time.strftime('%d/%m %H:%MZ', time.gmtime(int(m.get('ran_at') or 0)))})" for cid, m in measurements.items()))
    attempts = st.get('attempts') or []
    if attempts:
        lines = []
        for a in attempts[-8:]:
            when = time.strftime('%d/%m %H:%MZ', time.gmtime(int(a.get('at') or 0)))
            if a.get('hypothesis') or a.get('change'):
                lines.append(f"- {when}: hypothesis '{str(a.get('hypothesis') or '')[:160]}', change '{str(a.get('change') or '')[:160]}'")
            else:
                lines.append(f"- {when}: {a.get('criterion')} FAIL, observed {_json(a.get('observed'))[:160]}")
        parts.append('Attempts already made on this request (do not repeat what failed):\n' + '\n'.join(lines))
    if wf.get('next_action'):
        parts.append('Recorded next action: ' + str(wf['next_action'])[:300])
    note = completion_refusal_note(conn, task_id)
    if note:
        parts.append(note)
    return ('\n\nCase context (this request):\n' + '\n'.join(parts)) if parts else ''


def lessons_context(conn, task_id, limit=8):
    """Entre pedidos do projeto: comprovadas como orientação sob a condição; hipóteses aplicáveis como hipóteses."""
    if not _lessons_table(conn):
        return ''
    task = _kb().get_task(conn, task_id)
    haystack = ' '.join(filter(None, [task.title if task else '', task.body if task else '']))
    components = set()
    spec = get_spec(conn, task_id)
    if spec:
        for crit in json.loads(spec['content']).get('criteria') or []:
            comp = _probe_component(crit.get('probe'))
            if comp:
                components.update(comp.split(','))
    wf = get_workflow(conn, task_id)
    precheck = ((json.loads(wf['state_json'] or '{}') or {}).get('production_precheck') if wf else None) or {}
    for checked in precheck.get('checked') or []:
        if isinstance(checked, dict):
            haystack += ' ' + str(checked.get('target') or '')
    hay = _norm_text(haystack); comps = {_norm_text(c) for c in components}

    def applicable(row):
        comp = _norm_text(row.get('component') or '')
        if not comp:
            return True
        return any(part and (part in hay or part in comps) for part in comp.split(','))

    rows = [dict(r) for r in conn.execute("SELECT * FROM nfos_lessons WHERE kind IN ('proven','hypothesis') AND superseded_by IS NULL ORDER BY id DESC LIMIT 200")]
    proven = [r for r in rows if r['kind'] == 'proven' and applicable(r)]
    proven += [r for r in rows if r['kind'] == 'proven' and r not in proven]
    proven = proven[:limit]
    hypotheses = [r for r in rows if r['kind'] == 'hypothesis' and applicable(r)][:limit]
    if not proven and not hypotheses:
        return ''

    def fmt(r):
        ev = ', '.join(str(e) for e in json.loads(r.get('evidence') or '[]'))[:200]
        cond = f"when: {str(r.get('condition') or '')[:160]}; " if r.get('condition') else ''
        lim = f"; limit: {str(r.get('limit') or '')[:160]}" if r.get('limit') else ''
        return f"- [L{r['id']}] {cond}{str(r.get('text') or '')[:400]} (evidence: {ev or 'none'}{lim})"

    out = ['\n\nProject lessons:']
    if proven:
        out.append('Proven (guidance when the condition holds):'); out += [fmt(r) for r in proven]
    if hypotheses:
        out.append('Hypotheses from earlier attempts (not proven: test, do not assume):'); out += [fmt(r) for r in hypotheses]
    out.append('Record what you learn with `progress --hypothesis ... --change ...`; a FAIL followed by PASS after your change is captured '
               'automatically as a hypothesis; promotion to proven needs reason and evidence (`lesson`).')
    return '\n'.join(out)


def worker_context(conn, task_id):
    """Texto anexado ao prompt do worker no spawn e exposto no show."""
    try:
        return (case_context(conn, task_id) + lessons_context(conn, task_id))[:12000]
    except Exception:
        return ''


def lesson_command(conn, payload, *, author):
    op = str((payload or {}).get('op') or 'list')
    if op == 'list':
        return {'lessons': list_lessons(conn, include_retired=bool(payload.get('include_retired')))}
    if op == 'add':
        return {'lesson_id': record_lesson(conn, kind=payload.get('kind') or 'hypothesis', text=payload.get('text'), task_id=payload.get('task_id'),
                                          criterion=payload.get('criterion'), component=payload.get('component'), operation=payload.get('operation'),
                                          condition=payload.get('condition'), evidence=payload.get('evidence'), limit=payload.get('limit'), author=author)}
    if op == 'promote':
        promote_lesson(conn, payload.get('id'), reason=payload.get('reason'), evidence=payload.get('evidence') or [], author=author)
        return {'promoted': payload.get('id')}
    if op == 'retire':
        retire_lesson(conn, payload.get('id'), reason=payload.get('reason'), author=author)
        return {'retired': payload.get('id')}
    raise WorkflowError('lesson op must be list, add, promote or retire')


def save_spec(conn, task_id, run_id, spec, *, author, evidence):
    if 'delivery_destination' in spec:
        from hermes_cli.nfos_destination import validate
        validate(spec['delivery_destination'])
    if not spec.get('goal') or not spec.get('criteria') or not spec.get('steps'):
        raise WorkflowError('A spec needs a goal, verifiable criteria and direct steps')
    if spec.get('delivery_type')=='operation':  # OPERATION_FAST_20260910: guarda contra classificar como operation para pular o caminho de código
        _op=spec.get('operation') if isinstance(spec.get('operation'),dict) else {}
        if not str(_op.get('target') or '').strip() or not str(_op.get('mutation') or '').strip() or not str(spec.get('no_code_reason') or '').strip():
            raise WorkflowError('An operation spec needs operation.target (the production system or object), operation.mutation (what changes) and no_code_reason (why no repository change is needed). If any step edits the repository, builds, deploys or opens a PR, the card is code.')
    ids=[c.get('id') for c in spec['criteria']]
    if not all(ids) or len(set(ids))!=len(ids) or any(not c.get('text') for c in spec['criteria']):
        raise WorkflowError('Each spec criterion needs a unique id and description')
    if _owner_mode():  # RESULT_PROBE_20260911: sondas válidas; obrigatório quando a reprodução foi medida
        _spec_result_criteria_checks(conn,task_id,spec)
    if author not in {'Claude TL','Codex','worker'} or not evidence:  # BLOCK_LESS_20260910: o worker pode autorar a própria spec
        raise WorkflowError('Record the actual TL/Codex/worker execution evidence')
    if author=='Codex' and not evidence.get('fallback_reason'):
        raise WorkflowError('Codex spec fallback needs the Claude unavailability reason')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        wf=get_workflow(conn,task_id)
        from hermes_cli.nfos_principal_review import settings as _settings
        if _settings().get('principal_validation') is False and not wf['spec_revision']:  # BLOCK_LESS7_20260910: modo das premissas
            _pc=(json.loads(wf['state_json'] or '{}') or {}).get('production_precheck') or {}
            if _pc.get('instruction_revision')!=task.instruction_revision:
                raise WorkflowError('Production precheck missing: run `precheck --input precheck.json` (checked=[{target,method,result}]: for code read production/HML/PRs, for operation or report read only the production target; verdict=already_delivered|partial|not_delivered) before saving a spec')  # OPERATION_FAST2_20260910
            if str(spec.get('size') or '').strip().upper() not in SPEC_SIZE_BUDGET:  # BLOCK_LESS9_20260910
                raise WorkflowError('Spec needs size P, M or G (P: small fix up to 45 min; M: up to 2 h; G: up to 4 h); it sets the run budget and the board class')
        _size=str(spec.get('size') or '').strip().upper()  # BLOCK_LESS9_20260910: quem escreve a spec define o orçamento
        if _size in SPEC_SIZE_BUDGET:
            conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?',(SPEC_SIZE_BUDGET[_size],task_id))
            _event(conn,task_id,run_id,'nfos_spec_size',{'size':_size,'max_runtime_seconds':SPEC_SIZE_BUDGET[_size]})
        if spec.get('delivery_type')!=task.delivery_type:
            # The project default is provisional until TL has analyzed the
            # request. An audit must not inherit a Git delivery requirement.
            _new=spec.get('delivery_type')
            if _new=='code':  # OPERATION_FAST_20260910: corrigir classificação errada só com worktree
                _wk=conn.execute('SELECT workspace_kind FROM tasks WHERE id=?',(task_id,)).fetchone()
                if not _wk or (_wk[0] or '')!='worktree':
                    raise WorkflowError('Reclassifying to code needs a worktree workspace; this card has none: create a code card for the repository change')
                conn.execute('UPDATE tasks SET delivery_type=?,requires_repo=1 WHERE id=?',('code',task_id))
                conn.execute('UPDATE task_git_delivery SET required=1 WHERE task_id=?',(task_id,))
            elif _new not in {'report','operation'}:  # BLOCK_LESS_20260910: reclassificar para report/operation em qualquer estágio
                raise WorkflowError('Spec delivery type can only change to code, report or operation')
            else:
                conn.execute('UPDATE tasks SET delivery_type=?,requires_repo=0 WHERE id=?',
                             (_new,task_id))
                conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
            _event(conn,task_id,run_id,'nfos_delivery_classified',
                   {'previous':task.delivery_type,'delivery_type':_new,'author':author})
        revision=wf['spec_revision']+1
        saved_evidence=dict(evidence, instruction_revision=task.instruction_revision)
        conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (task_id,run_id,'spec',revision,_json(spec),author,_json(saved_evidence),int(time.time())))
        conn.execute("UPDATE nfos_workflows SET stage='spec',spec_revision=?,next_action='Implement and verify the persisted spec',updated_at=? WHERE task_id=?",
                     (revision,int(time.time()),task_id))
        _event(conn,task_id,run_id,'nfos_spec_saved',{'revision':revision,'author':author,'evidence':saved_evidence})
        from hermes_cli.nfos_principal_review import required
        if required(conn,task_id):
            ask_principal(conn,task_id,run_id,kind='spec_review',
                          question='Validate the saved spec against the original request before implementation',context={})
            conn.execute('UPDATE nfos_workflows SET next_action=? WHERE task_id=?',
                         ('Wait for Principal spec acceptance; revise the spec if changes are requested',task_id))
    return revision


def _scope_needs_new_spec(workflow):
    partition=json.loads(workflow['state_json']).get('task_partition') or {}
    after=partition.get('spec_revision_after')
    return after is not None and workflow['spec_revision']<=after


def advance(conn, task_id, run_id, stage, *, next_action, state=None):
    if stage not in {'analysis','implement','homolog','review','publish','verify','report'}:
        raise WorkflowError('Unknown delivery stage')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        wf=get_workflow(conn,task_id)
        if stage!='analysis' and not wf['spec_revision']:
            raise WorkflowError('Persist the spec before implementation')
        if stage!='analysis' and _scope_needs_new_spec(wf):
            raise WorkflowError('Principal changed the primary task; persist a newer spec before continuing delivery')
        if stage!='analysis':
            _require_current_instruction_spec(conn,task_id)
            from hermes_cli.nfos_principal_review import require_spec
            require_spec(conn,task_id)
        updates=dict(state or {})
        updates.pop('task_partition',None)  # Only Principal decisions own this receipt.
        saved=json.loads(wf['state_json']);saved.update(updates)
        if stage=='implement' and wf['stage'] in {'analysis','spec'} and not json.loads(wf['state_json']).get('task_partition'):
            partition=conn.execute("SELECT status,action FROM nfos_decisions WHERE task_id=? AND kind='additional_tasks' ORDER BY rowid DESC LIMIT 1",
                                   (task_id,)).fetchone()
            if partition and (partition['status']!='resolved' or partition['action']!='continue'):
                raise WorkflowError('Principal must resolve the additional task boundaries before implementation; analysis can continue')
        if task.delivery_type=='code' and stage in {'homolog','publish'}:
            if stage=='publish' and not _approved(conn,task_id,wf['spec_revision'],state=saved):
                raise WorkflowError('Principal review of this candidate is pending')
            _project_owned(conn,task_id,run_id,_delivery_candidate(conn,task_id,saved))
        conn.execute('UPDATE nfos_workflows SET stage=?,next_action=?,state_json=?,updated_at=? WHERE task_id=?',
                     (stage,next_action,_json(saved),int(time.time()),task_id))
        _event(conn,task_id,run_id,'nfos_progress',{'stage':stage,'next_action':next_action,'state':updates})


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


def _ensure_continuations(conn):  # RECORD_CONTINUATION_20260911
    conn.execute('CREATE TABLE IF NOT EXISTS nfos_continuations(child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, created_at INTEGER NOT NULL)')


def continuation_links(conn, task_id):
    """RECORD_CONTINUATION_20260911: {'of': pai ou None, 'children': [filhos]}."""
    try:
        _ensure_continuations(conn)
        of = conn.execute('SELECT parent_id FROM nfos_continuations WHERE child_id=?', (task_id,)).fetchone()
        children = [r[0] for r in conn.execute('SELECT child_id FROM nfos_continuations WHERE parent_id=? ORDER BY created_at', (task_id,))]
        return {'of': of[0] if of else None, 'children': children}
    except Exception:
        return {'of': None, 'children': []}


def continuation_chain(conn, task_id):
    """RECORD_CONTINUATION_20260911: ids da cadeia (raiz até este card) para somar consumo."""
    chain = [task_id]; seen = {task_id}
    try:
        _ensure_continuations(conn)
        cur = task_id
        for _ in range(50):
            row = conn.execute('SELECT parent_id FROM nfos_continuations WHERE child_id=?', (cur,)).fetchone()
            if not row or row[0] in seen:
                break
            chain.insert(0, row[0]); seen.add(row[0]); cur = row[0]
    except Exception:
        pass
    return chain


def chain_consumption(conn, task_id):
    """RECORD_CONTINUATION_20260911: runs e minutos somados na cadeia de continuação."""
    ids = continuation_chain(conn, task_id)
    q = ','.join('?' * len(ids))
    row = conn.execute(f"SELECT count(*), coalesce(sum(coalesce(ended_at, strftime('%s','now'))-started_at),0) FROM task_runs WHERE task_id IN ({q})", ids).fetchone()
    return {'chain': ids, 'runs': int(row[0] or 0), 'minutes': int((row[1] or 0) // 60)}


def _unmet_criteria_text(conn, task_id):
    report = _artifact(conn, task_id, 'report')
    if not report:
        return 'Sem relatório salvo no card de origem.'
    try:
        content = json.loads(report['content'])
    except Exception:
        return 'Relatório de origem ilegível.'
    lines = []
    for c in content.get('criteria') or []:
        if c.get('status') != 'PASS':
            lines.append(f"- {c.get('id')}: {c.get('status')} (evidência: {', '.join(c.get('evidence') or []) or 'nenhuma'})")
    arts = [f"{a.get('id')}: {a.get('path')}" for a in (content.get('artifacts') or [])]
    blockers = content.get('blockers') or []
    text = 'Critérios ainda não atendidos no card de origem (relatório r%s):\n' % report['revision'] + ('\n'.join(lines) or '- nenhum')
    if blockers:
        text += '\nBloqueios registrados: ' + _json(blockers)[:1500]
    if arts:
        text += '\nEvidências já existentes: ' + '; '.join(arts)[:2000]
    return text


def create_continuation(conn, parent_id, *, title=None, body=None, requester='worker'):
    """RECORD_CONTINUATION_20260911: continuação executável do mesmo pedido. Sem `parents` (sem dependência circular); herda prioridade,
    executor, tipo, projeto, tenant e workspace scratch; recebe os critérios não atendidos e as evidências do pai; idempotente
    (um filho aberto por pai); pai bloqueado com pergunta a humano gera filho bloqueado com a mesma pergunta."""
    kb = _kb()
    parent = kb.get_task(conn, parent_id)
    if not parent:
        raise WorkflowError('Unknown parent card for continuation')
    if parent.status in {'done', 'archived'} and not continuation_links(conn, parent_id)['children']:
        raise WorkflowError('A continuation is created before the parent closes')
    _ensure_continuations(conn)
    unmet = _unmet_criteria_text(conn, parent_id)
    child_title = (title or f"Continuação: {parent.title}")[:200]
    child_body = ((body or '').strip() + '\n\n' if body else '') + f"Continuação do card {parent_id} (mesmo pedido). Não repita entregas já confirmadas; leia o card de origem, seus efeitos e artefatos.\n" + unmet
    if parent.workspace_path:
        child_body += f"\nWorkspace de origem: {parent.workspace_path}" + (f" (branch {parent.branch_name})" if parent.branch_name else '')
    with kb.write_txn(conn):  # RECORD_CONTINUATION_FIX_20260911: checagem e criação na mesma transação de escrita (BEGIN IMMEDIATE serializa)
        existing = conn.execute("SELECT c.child_id FROM nfos_continuations c JOIN tasks t ON t.id=c.child_id WHERE c.parent_id=? AND t.status NOT IN ('done','archived') ORDER BY c.created_at DESC LIMIT 1", (parent_id,)).fetchone()
        if existing:
            child = kb.get_task(conn, existing[0])
            return {'task_id': child.id, 'continuation_of': parent_id, 'priority': child.priority, 'status': child.status, 'existing': True}
        child_id = kb.create_task(conn, title=child_title, body=child_body, assignee=parent.assignee,
                                  created_by=f"continuation:{requester}",
                                  workspace_kind=parent.workspace_kind if parent.workspace_kind == 'scratch' else 'worktree',
                                  workspace_path=parent.workspace_path if parent.workspace_kind == 'scratch' else None,
                                  tenant=parent.tenant, priority=max(int(parent.priority or 0), 0), parents=(),
                                  project_id=parent.project_id, delivery_type=parent.delivery_type,
                                  max_runtime_seconds=parent.max_runtime_seconds)
        conn.execute('INSERT INTO nfos_continuations(child_id,parent_id,created_at) VALUES(?,?,?)', (child_id, parent_id, int(time.time())))
        kb._append_event(conn, parent_id, 'nfos_continuation_created', {'child': child_id, 'by': requester, 'priority': max(int(parent.priority or 0), 0)})
        kb._append_event(conn, child_id, 'nfos_continuation_of', {'parent': parent_id, 'by': requester})
    if parent.status == 'blocked' and (parent.block_kind or 'needs_input') in ('needs_input', 'capability'):
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (parent_id,)).fetchone()
        reason = ''
        try:
            reason = (json.loads(ev[0]).get('reason') or '') if ev else ''
        except Exception:
            reason = ''
        if reason:
            try:
                kb.block_task(conn, child_id, reason=reason, kind='needs_input')
            except Exception:
                pass
    child = kb.get_task(conn, child_id)
    return {'task_id': child_id, 'continuation_of': parent_id, 'priority': child.priority, 'status': child.status, 'existing': False}


def _continuation_allows_partial(conn, task_id, content):
    """RECORD_CONTINUATION_20260911: FAIL só fecha (owner mode) com partial_delivery=true e continuation = filho aberto deste card."""
    try:
        if content.get('partial_delivery') is not True:
            return False
        child = str(content.get('continuation') or '')
        if not child:
            return False
        _ensure_continuations(conn)
        row = conn.execute("SELECT t.status FROM nfos_continuations c JOIN tasks t ON t.id=c.child_id WHERE c.child_id=? AND c.parent_id=?", (child, task_id)).fetchone()
        return bool(row) and row[0] not in ('done', 'archived')
    except Exception:
        return False


def _check_reclassification(conn, task_id, report):
    """RECORD_CONTINUATION_20260911: critério FAIL na revisão anterior só muda com reclassified {id, previous:'FAIL', reason, evidence}."""
    previous = _artifact(conn, task_id, 'report')
    if not previous:
        return
    try:
        prev = json.loads(previous['content'])
    except Exception:
        return
    prev_fail = {c.get('id') for c in (prev.get('criteria') or []) if c.get('status') == 'FAIL'}
    if not prev_fail:
        return
    artifacts = {a.get('id') for a in (report.get('artifacts') or [])} | {a.get('path') for a in (report.get('artifacts') or [])}
    recl = {r.get('id'): r for r in (report.get('reclassified') or []) if isinstance(r, dict)}
    for c in report.get('criteria') or []:
        cid = c.get('id')
        if cid in prev_fail and c.get('status') != 'FAIL':
            r = recl.get(cid)
            if not r or r.get('previous') != 'FAIL' or not str(r.get('reason') or '').strip() or not r.get('evidence') \
                    or any(e not in artifacts for e in r.get('evidence')):
                raise WorkflowError(f"Criterion {cid} was FAIL in report r{previous['revision']}: changing it needs reclassified "
                                    "{id, previous:'FAIL', reason, evidence:[declared artifact]}; the earlier revision stays")


def save_report(conn, task_id, run_id, report):
    if conn.in_transaction:
        raise WorkflowError('Report evidence must be read outside a write transaction')
    task=_owned(conn,task_id,run_id)
    spec=get_spec(conn,task_id)
    if not spec:
        raise WorkflowError('No persisted spec')
    _require_current_instruction_spec(conn,task_id)
    report=json.loads(_json(report))
    _check_reclassification(conn,task_id,report)  # RECORD_CONTINUATION_20260911
    if _owner_mode():  # RESULT_PROBE_20260911: critério obrigatório recebe o status da última medição válida
        _apply_measurements_to_report(conn,task_id,spec,report)
    encoded=_json(report)
    checks=_report_artifact_checks(conn,task,spec,report)
    evidence={'spec_revision':spec['revision'],'report_sha256':hashlib.sha256(encoded.encode()).hexdigest(),
              'artifact_checks':checks,'schema_version':1}
    with _kb().write_txn(conn):
        current=_owned(conn,task_id,run_id)
        if get_spec(conn,task_id)['id']!=spec['id'] or current.workspace_path!=task.workspace_path:
            raise WorkflowError('Spec or workspace changed while evidence was checked; save the current report')
        _require_current_instruction_spec(conn,task_id)
        previous=_artifact(conn,task_id,'report');revision=(previous['revision'] if previous else 0)+1
        conn.execute('INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (task_id,run_id,'report',revision,encoded,'worker',_json(evidence),int(time.time())))
        _event(conn,task_id,run_id,'nfos_report_saved',{'revision':revision,'spec_revision':spec['revision']})


_AUTO_CONTINUE = [  # BLOCK_LESS_20260910: classes de impedimento que o principal respondeu 'continue' em 223 de 302 casos (7 dias)
    (r'pre-?push|pre-?commit|lefthook|husky|no-verify|build:local|DATABASE_URL|\bhooks?\b.{0,80}(fail|block|barr|falh|imped)|(fail|block|barr|falh|imped).{0,80}\bhooks?\b',  # HOOK_AUTO_20260911
     'CONTINUE (automático, premissa do owner 10/09): hook local de pre-push/pre-commit não é gate. Faça o push agora com --no-verify; o CI do PR é o gate. Não instale dependências nem configure ambiente só para satisfazer o hook; não reconcilie ambiente de build; não pergunte de novo.'),
    (r'slot|acquire-project|acquired.{0,4}false|hml (window|remains|slot)|janela (de )?hml|occupied|ocupad',
     'CONTINUE (automático, premissa do owner 10/09): em modo registro não há slot de publicação; `acquire-project` só registra quem publica. Siga a rota do projeto mostrada no show; não pergunte de novo.'),  # RELEASE_FLOW_20260911, RECORD_MODE_TEXT_20260911
    (r'readback.{0,40}inconsisten|inconsisten.{0,40}readback|release receipt',
     'CONTINUE (automático): faça a releitura (`reconcile`) do alvo e siga com o que a releitura mostrar; não bloqueie.'),
    (r'stale lease|retained lease|partial hml delivery',
     'CONTINUE (automático): trate o lease retido com `repair-workspace`/`reconcile` e siga no mesmo card.'),
    (r'next authorized action|next step|pr[oó]xim[oa] (passo|a[cç][aã]o)|what should .{0,30} do',
     'CONTINUE (automático): siga a próxima etapa da spec salva. O principal não decide passo a passo.'),
    (r'reavaliar o impedimento registrado|retomar o mesmo card se resolv|impediment registered in the history',  # BLOCK_LESS2_20260910
     'CONTINUE (automático): o histórico de impedimentos foi tratado na triagem de 10/09. Retome o mesmo card do estado salvo e entregue; se algo só um humano pode fornecer, bloqueie com a pergunta e o destinatário no motivo.'),
    (r'rate.?limit|\b429\b|usage limit|quota|\bcota\b',
     'CONTINUE (automático): rate limit é transitório. Aguarde com backoff (60 s, 120 s, 300 s) e repita; não bloqueie o card.'),
]


def _owner_mode():
    """BLOCK_LESS8_20260910: modo das premissas do owner = principal_validation explicitamente false no perfil."""
    try:
        from hermes_cli.nfos_principal_review import settings
        return settings().get('principal_validation') is False
    except Exception:
        return False


def _auto_continue_answer(kind, question):
    """BLOCK_LESS_20260910: resposta automática do principal para perguntas que não mudam o resultado."""
    if kind=='homologation':
        return 'CONTINUE (automático, premissa do owner 10/09): candidato exato aceito para publicação em HML; publique, valide e siga.'
    if kind!='impediment':
        return None
    q=(question or '').lower()
    for rx,ans in _AUTO_CONTINUE:
        if re.search(rx,q):
            return ans
    return None


_CODE_ROUTE_PREPARATION = ('CONTINUE (automático, premissa do owner 10/09): preparação registrada, sem revisão do principal. '
                           'Siga a rota do projeto mostrada no show (delivery_environment): staging quando o projeto tem esteira, HML, dev ou test; CI é o gate.')  # CODE_FAST_ROUTE_20260910, RECORD_MODE_TEXT_20260911


def _pr_green(conn, task_id, candidate):
    """CODE_FAST_ROUTE_20260910: efeito pr confirmado com ci_status success para o candidato, ou None."""
    row = _confirmed(conn, task_id, 'pr', candidate) if candidate else None
    if not row:
        return None
    try:
        ev = json.loads(row['evidence'] or '{}')
    except Exception:
        return None
    return ev if isinstance(ev, dict) and ev.get('ci_status') == 'success' else None


def _code_route_review(conn, task_id):
    """CODE_FAST_ROUTE_20260910 (premissa do owner 10/09): revisão de card de código é mecânica: PR reconciliado com CI verde aprova."""
    state = json.loads(get_workflow(conn, task_id)['state_json'] or '{}')
    candidate = state.get('candidate_sha')
    ev = _pr_green(conn, task_id, candidate)
    if ev:
        return 'approve', ('APPROVE (automático, premissa do owner 10/09): PR ' + str(ev.get('pull_request') or '') + ' com CI verde para o candidato '
                           + str(candidate)[:12] + '. Faça o merge (gh pr merge --merge) registrando o efeito merge e reconciliando o SHA integrado, '
                           'aguarde o deploy automático (efeito deploy + readback de produção), salve o relatório e chame kanban_complete.')
    return 'continue', ('CONTINUE (automático, premissa do owner 10/09): a revisão é mecânica e exige o efeito pr reconciliado com ci_status success '
                        'para o candidato atual (' + str(candidate)[:12] + '). Abra ou atualize o PR da rota do projeto (show: delivery_environment), registre `effect --operation pr` e `reconcile` '
                        'com o readback do CI; se o CI falhar, corrija e repita; depois peça review de novo.')  # RECORD_MODE_TEXT_20260911


def ask_principal(conn, task_id, run_id, *, kind, question, context):
    if kind not in {'review','spec_review','final_review','impediment','additional_tasks','homologation','preparation'} or not question.strip():
        raise WorkflowError('A decision needs its kind and concrete question')
    with _kb().write_txn(conn,allow_nested=True):
        _owned(conn,task_id,run_id)
        if kind in {'spec_review','final_review'}:  # BLOCK_LESS2_20260910: validação desligada no perfil = pergunta sem efeito
            if _owner_mode():  # BLOCK_LESS8_20260910
                decision_id='dec_'+uuid.uuid4().hex[:20]; now=int(time.time())
                revision=get_workflow(conn,task_id)['spec_revision']
                auto='CONTINUE (automático, premissa do owner 10/09): validação do principal desligada no perfil; não peça '+kind+'. Siga: implemente, salve o relatório e chame kanban_complete.'
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (decision_id,task_id,run_id,kind,question,_json(dict(context)),revision,now,'resolved','continue',auto,'Principal',now))
                _event(conn,task_id,run_id,'nfos_principal_auto_continue',{'decision_id':decision_id,'kind':kind,'question':question,'answer':auto})
                return decision_id
        if kind=='review':  # BLOCK_LESS3_20260910: relatório/operação não tem publicação a aprovar
            from hermes_cli.nfos_principal_review import required
            _t=_kb().get_task(conn,task_id)
            if _t and _t.delivery_type in {'report','operation'} and _owner_mode():  # BLOCK_LESS8_20260910
                decision_id='dec_'+uuid.uuid4().hex[:20]; now=int(time.time())
                revision=get_workflow(conn,task_id)['spec_revision']
                auto='APPROVE (automático, premissa do owner 10/09): entrega de relatório/operação não passa por revisão de publicação. Chame kanban_complete.'
                _ctx=dict(context); _ctx['review_identity']=_review_identity(conn,task_id)  # BLOCK_LESS4_20260910
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (decision_id,task_id,run_id,kind,question,_json(_ctx),revision,now,'resolved','approve',auto,'Principal',now))
                _event(conn,task_id,run_id,'nfos_principal_auto_continue',{'decision_id':decision_id,'kind':kind,'question':question,'answer':auto})
                return decision_id
            _require_current_instruction_spec(conn,task_id)
            if _t and _t.delivery_type=='code' and _owner_mode():  # CODE_FAST_ROUTE_20260910: revisão mecânica (PR com CI verde)
                _action,_auto=_code_route_review(conn,task_id)
                decision_id='dec_'+uuid.uuid4().hex[:20]; now=int(time.time())
                revision=get_workflow(conn,task_id)['spec_revision']
                _ctx=dict(context)
                if _action=='approve':
                    _ctx['review_identity']=_review_identity(conn,task_id)
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (decision_id,task_id,run_id,kind,question,_json(_ctx),revision,now,'resolved',_action,_auto,'Principal',now))
                _event(conn,task_id,run_id,'nfos_principal_auto_continue',{'decision_id':decision_id,'kind':kind,'question':question,'answer':_auto})
                return decision_id
        context=dict(context)
        if kind in {'spec_review','final_review'}:
            from hermes_cli.nfos_principal_review import identity
            _require_current_instruction_spec(conn,task_id)
            context.pop('assessment',None)
            context['acceptance_identity']=identity(conn,task_id,kind)
            latest=conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? AND kind=? ORDER BY rowid DESC LIMIT 1',
                                (task_id,kind)).fetchone()
            if (latest and latest['status']=='pending'
                    and json.loads(latest['context']).get('acceptance_identity')==context['acceptance_identity']):
                return latest['id']
        if kind=='preparation' and _owner_mode():  # CODE_FAST_ROUTE_20260910: preparação sem decisão do principal
            _t=_kb().get_task(conn,task_id)
            if _t and _t.delivery_type=='code':
                from hermes_cli import nfos_preparation as _prep
                try:
                    context['preparation_identity']=_prep.identity(conn,task_id,context.get('preparation'))
                except Exception:
                    context['preparation_identity']=None
                decision_id='dec_'+uuid.uuid4().hex[:20]; now=int(time.time())
                revision=get_workflow(conn,task_id)['spec_revision']
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (decision_id,task_id,run_id,kind,question,_json(context),revision,now,'resolved','continue',_CODE_ROUTE_PREPARATION,'Principal',now))
                _event(conn,task_id,run_id,'nfos_principal_auto_continue',{'decision_id':decision_id,'kind':kind,'question':question,'answer':_CODE_ROUTE_PREPARATION})
                return decision_id
        if kind=='preparation':
            from hermes_cli import nfos_preparation
            context['preparation_identity']=nfos_preparation.identity(conn,task_id,context.get('preparation'))
        if kind=='homologation':
            _require_current_instruction_spec(conn,task_id)
            context=_homologation_context(conn,task_id,context)
        if kind=='additional_tasks':
            # Identity follows the saved proposal, not the execution/question
            # wording. A restarted worker can recover the same answered item.
            request_id=get_workflow(conn,task_id)['request_id']
            if not get_request(conn,request_id):
                raise WorkflowError('Additional tasks need their original persisted request')
            context['request_id']=request_id
            decision_id='dec_'+hashlib.sha256(_json([task_id,context]).encode()).hexdigest()[:24]
            if get_decision(conn,decision_id):
                return decision_id
        else:
            decision_id='dec_'+uuid.uuid4().hex[:20]
        existing=conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND run_id=? AND kind=? AND question=? AND status='pending'",
                               (task_id,run_id,kind,question)).fetchone()
        if existing and kind not in {'additional_tasks','homologation','spec_review','final_review'}:
            return existing['id']
        revision=get_workflow(conn,task_id)['spec_revision']
        auto=_auto_continue_answer(kind,question)  # BLOCK_LESS_20260910
        if auto:
            now=int(time.time())
            conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at,status,action,answer,author,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (decision_id,task_id,run_id,kind,question,_json(context),revision,now,'resolved','continue',auto,'Principal',now))
            try:
                if kind=='homologation':
                    _accept_homologation(conn,get_decision(conn,decision_id))
            except WorkflowError:
                conn.execute('DELETE FROM nfos_decisions WHERE id=?',(decision_id,))
            else:
                _event(conn,task_id,run_id,'nfos_principal_auto_continue',{'decision_id':decision_id,'kind':kind,'question':question,'answer':auto})
                return decision_id
        if kind=='review':
            context['review_identity']=_review_identity(conn,task_id)
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (decision_id,task_id,run_id,kind,question,_json(context),revision,int(time.time())))
        _event(conn,task_id,run_id,'nfos_principal_requested',{'decision_id':decision_id,'kind':kind,'question':question})
        return decision_id


def _additional_requests(conn, decision, proposal):
    """Plan the whole batch before writing any child. No external I/O."""
    if not isinstance(proposal,dict) or not isinstance(proposal.get('primary_task'),str) or not proposal['primary_task'].strip():
        raise WorkflowError('Identify the first task retained on this card in primary_task')
    items=proposal.get('tasks')
    if not isinstance(items,list):
        raise WorkflowError('tasks must list the additional items; an explicit empty list keeps only the first task')
    parent=get_request(conn,json.loads(decision['context'])['request_id'])
    parent_payload=json.loads(parent['payload'])
    root_id=(parent_payload.get('origin') or {}).get('request_id') or parent['id']
    root=get_request(conn,root_id)
    if not root:
        raise WorkflowError('The original request in this task lineage is unavailable')
    original=json.loads(root['payload'])
    project={key:value for key,value in original['project'].items() if key!='existing_task_id'}
    seen=set();planned=[]
    for item in items:
        if not isinstance(item,dict) or any(not isinstance(item.get(key),str) or not item[key].strip()
                                          for key in ('key','text','source_ref')):
            raise WorkflowError('Every additional item needs a stable key, text and source_ref in the original message/media')
        key=item['key'].strip()
        if key=='0' or key in seen:
            raise WorkflowError('Additional keys must be unique; key 0 belongs to the first task')
        seen.add(key)
        part='additional:'+hashlib.sha256(_json([root['id'],key]).encode()).hexdigest()
        source_key=_json([str(original['source'][k]) for k in ('platform','chat_id','thread_id','message_id')]+[part])
        request_id='req_'+hashlib.sha256(source_key.encode()).hexdigest()[:24]
        if request_id==parent['id']:
            raise WorkflowError('This item is the current task, not an additional task')
        origin={'request_id':root['id'],'task_id':root['task_id'],'decision_id':decision['id'],
                'item_key':key,'source_ref':item['source_ref'],'original_text':original['text'],
                'discovered_by_task_id':decision['task_id'],'proposal_request_id':parent['id']}
        payload={'source':original['source'],'text':item['text'],'project':project,
                 'attachments':original['attachments'],'origin':origin}
        existing=get_request(conn,request_id)
        if existing:
            prior=json.loads(existing['payload'])
            # Receipt state belongs to delivery, and another accepted batch may
            # reference the same item. Neither changes the original request.
            comparable={k:prior.get(k) for k in ('source','text','project','attachments','origin')}
            prior_origin=dict(comparable.get('origin') or {})
            for metadata in ('decision_id','discovered_by_task_id','proposal_request_id'):
                prior_origin[metadata]=origin[metadata]
            comparable['origin']=prior_origin
            if comparable!=payload:
                raise WorkflowError(f'Additional key {key!r} already identifies different work; review the existing request {request_id}')
        planned.append({'id':request_id,'part':part,'payload':payload})
    return planned


def _dispatch_additional_tasks(conn, decision, proposal):
    planned=_additional_requests(conn,decision,proposal)
    for item in planned:
        payload=item['payload']
        receive_request(conn,source=payload['source'],text=payload['text'],project=payload['project'],
                        attachments=payload['attachments'],part=item['part'],origin=payload['origin'])
    wf=get_workflow(conn,decision['task_id'])
    state=json.loads(wf['state_json'])
    previous=state.get('task_partition') or {}
    prior_primary=previous.get('primary_task')
    if prior_primary is None and wf['spec_revision']:
        prior_primary=json.loads(get_spec(conn,decision['task_id'])['content'])['goal']
    spec_after=previous.get('spec_revision_after')
    if wf['spec_revision'] and prior_primary.strip()!=proposal['primary_task'].strip():
        spec_after=wf['spec_revision']
    state['task_partition']={'decision_id':decision['id'],'primary_task':proposal['primary_task'],
                             'request_ids':[item['id'] for item in planned]}
    if spec_after is not None:
        state['task_partition']['spec_revision_after']=spec_after
    conn.execute('UPDATE nfos_workflows SET state_json=?,updated_at=? WHERE task_id=?',
                 (_json(state),int(time.time()),decision['task_id']))
    _event(conn,decision['task_id'],decision['run_id'],'nfos_additional_tasks_dispatched',state['task_partition'])
    return [item['id'] for item in planned]


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
        suspension = active_suspension(conn, row['task_id'])
        if suspension:
            return dict(suspension)
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
        task=_kb().get_task(conn,row['task_id'])
        if not task or task.status in {'done','archived'}:
            continue
        # Apply the Principal's persisted decision even when the model ignores
        # it or waits on a newer question. Closing the run enables the existing
        # process-tree reconciler; waiting for the live worker first deadlocks.
        # HUMAN_BLOCK_NOW_20260910: a pergunta a humano é do card; um respawn (run novo) não a apaga.
        if task.status in {'running','ready'}:
            _kb().block_task(conn,task.id,reason=row['answer'],kind='needs_input',
                             expected_run_id=task.current_run_id if task.status=='running' else None)
        if (_run_process_alive(conn,row['task_id'],row['run_id'])
                or run_termination_pending(conn,row['task_id'],row['run_id'])):
            continue
        reply=json.loads(row['context']).get('human_reply')
        if not reply:
            continue
        with _kb().write_txn(conn):
            current=get_decision(conn,row['id'])
            if current['status']!='human':
                continue
            if not _kb().unblock_task(conn,row['task_id']):
                continue
            splits=conn.execute("SELECT id,question FROM nfos_decisions WHERE task_id=? AND status='human' AND kind='additional_tasks'",
                                (row['task_id'],)).fetchall()
            conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer=?,author=?,resolved_at=? WHERE task_id=? AND status='human' AND kind!='additional_tasks'",
                         (reply['answer'],reply.get('author','Human'),int(time.time()),row['task_id']))
            # A human reply supplies information, not an implicit batch dispatch.
            # The Principal uses that saved reply through the same atomic path.
            conn.execute("UPDATE nfos_decisions SET status='pending',action=NULL,answer=NULL,author=NULL,resolved_at=NULL,dispatched_at=NULL WHERE task_id=? AND status='human' AND kind='additional_tasks'",
                         (row['task_id'],))
            _event(conn,row['task_id'],None,'nfos_human_answered',reply)
            for split in splits:
                _event(conn,row['task_id'],None,'nfos_principal_requested',
                       {'decision_id':split['id'],'kind':'additional_tasks','question':split['question'],
                        'human_reply_available':True})
            resumed.append(row['task_id'])
    return resumed


def _human_question_valid(question, to):
    """BLOCK_LESS_20260910: human só com pergunta concreta (termina em ?) e destinatário nomeado."""
    q=str(question or '').strip(); t=str(to or '').strip()
    return bool(q) and '?' in q and bool(t)


def resolve_decision(conn, decision_id, *, action, answer, author, proposal=None, assessment=None):
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise WorkflowError('The Principal resolves reviews in its own coordinator session')
    if action not in {'continue','approve','changes','human'} or not answer.strip() or author!='Principal':
        raise WorkflowError('Principal decision requires its concrete answer and action')
    initial=get_decision(conn,decision_id)
    assessed=None
    if initial and initial['status']=='pending' and initial['kind'] in {'spec_review','final_review'} and action=='continue':
        from hermes_cli.nfos_principal_review import assess
        if conn.in_transaction:
            raise WorkflowError('Acceptance evidence must be inspected outside a write transaction; ask for a fresh review')
        assessed=assess(conn,initial,assessment)
    # Reconsideration composes this same validation with the unblock atomically.
    with _kb().write_txn(conn, allow_nested=True):
        row=get_decision(conn,decision_id)
        if not row:
            raise WorkflowError('Unknown decision')
        if action=='approve':
            _require_current_instruction_spec(conn,row['task_id'])
            reviewed_revision=(json.loads(row['context']).get('review_identity') or {}).get('instruction_revision',0)
            if reviewed_revision!=_kb().get_task(conn,row['task_id']).instruction_revision:
                raise WorkflowError('Card instructions changed during review; review the updated spec')
        if row['status']!='pending':
            if row['action']==action and row['answer']==answer:
                if proposal is not None and json.loads(row['context']).get('dispatch_proposal')!=proposal:
                    raise WorkflowError('Decision already dispatched a different proposal')
                return
            saved=json.loads(row['context'])
            if saved.get('dispatch_errors') and action=='continue' and saved.get('requested_answer')==answer:
                if proposal is not None and saved.get('dispatch_proposal')!=proposal:
                    raise WorkflowError('Decision already returned changes for a different proposal')
                return
            raise WorkflowError('Decision was already resolved')
        if action=='approve' and row['kind']!='review':
            raise WorkflowError('Only a delivery review can authorize publication')
        if row['kind']=='preparation' and action=='continue':
            from hermes_cli import nfos_preparation
            nfos_preparation.check_current(conn,row)
        if row['kind']=='homologation' and action=='continue':
            _accept_homologation(conn,row)
        if row['kind'] in {'spec_review','final_review'} and action=='continue':
            from hermes_cli.nfos_principal_review import identity
            context=json.loads(row['context'])
            if context.get('acceptance_identity')!=identity(conn,row['task_id'],row['kind']):
                raise WorkflowError('Spec, instruction, candidate or report changed while reviewing')
            context['assessment']=assessed
            conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?',(_json(context),decision_id))
        current_spec_revision=get_workflow(conn,row['task_id'])['spec_revision']
        # Operational questions can outlive spec preparation. Their answers
        # resolve the original question, never approve a different delivery.
        if row['kind']=='review' and row['spec_revision']!=current_spec_revision:
            raise WorkflowError('Spec changed during review; review the current revision')
        if proposal is not None and row['kind']!='additional_tasks':
            raise WorkflowError('A revised task proposal applies only to additional_tasks')
        if row['kind']=='additional_tasks' and action=='continue':
            context=json.loads(row['context'])
            selected=proposal if proposal is not None else {key:context.get(key) for key in ('primary_task','tasks')}
            context['dispatch_proposal']=selected
            # Validate everything first. An ambiguous extraction remains visible
            # and goes back to its worker; it is never partially dispatched.
            try:
                _additional_requests(conn,row,selected)
            except WorkflowError as exc:
                context['dispatch_errors']=[str(exc)]
                context['requested_answer']=answer
                action='changes';answer+='\nRevise the saved proposal: '+str(exc)
            else:
                context['request_ids']=_dispatch_additional_tasks(conn,row,selected)
            conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?',(_json(context),decision_id))
        if action=='approve':
            identity=_review_identity(conn,row['task_id'])
            if json.loads(row['context']).get('review_identity')!=identity:
                raise WorkflowError('Candidate or report changed during review')
            task=_kb().get_task(conn,row['task_id'])
            if task.delivery_type=='code':
                from hermes_cli.nfos_destination import destination, review_only, verified
                if review_only(destination(conn,task.id)):
                    # The review PR with green CI is the verified destination itself.
                    if not verified(conn,task.id,json.loads(get_workflow(conn,task.id)['state_json'])):
                        raise WorkflowError('Review needs the confirmed review PR with CI for the accepted candidate')
                else:
                    if not all(identity.get(k) for k in ('homolog_sha','candidate_tree','homolog_evidence')):
                        raise WorkflowError('Review needs the actual homologated candidate and evidence')
                    if not _confirmed(conn,task.id,'pr',_delivery_candidate(conn,task.id)):
                        raise WorkflowError('Review needs the confirmed PR for this candidate')
            elif not identity.get('report_revision'):
                raise WorkflowError('Review needs the saved report')
        conn.execute('UPDATE nfos_decisions SET status=?,answer=?,author=?,action=?,resolved_at=? WHERE id=?',
                     ('human' if action=='human' else 'resolved',answer,author,action,int(time.time()),decision_id))
        _event(conn,row['task_id'],row['run_id'],'nfos_principal_resolved',
               {'decision_id':decision_id,'action':action,'answer':answer,
                'asked_spec_revision':row['spec_revision'],'resolved_spec_revision':current_spec_revision})
        context=json.loads(row['context'])
        if (context.get('legacy_adoption') and not context.get('reconsideration_identity')
                and action in {'continue','changes'}):
            from hermes_cli.nfos_runtime import previous_runs_termination_pending
            if previous_runs_termination_pending(conn,row['task_id']):
                raise OwnershipConflict('Previous execution must exit before resuming retained work')
            _kb().unblock_task(conn,row['task_id'])
            if _kb().get_task(conn,row['task_id']).status=='review':
                _kb().reopen_review_task(conn,row['task_id'])
    if action=='human':  # HUMAN_BLOCK_NOW_20260910 / HUMAN_BLOCK_NOW2_20260910: bloqueia na hora só se não há worker vivo neste run
        _t=_kb().get_task(conn,row['task_id'])
        if _t and (_t.status=='ready' or (_t.status=='running' and not _run_process_alive(conn,_t.id,_t.current_run_id))):
            _kb().block_task(conn,_t.id,reason=answer,kind='needs_input',
                             expected_run_id=_t.current_run_id if _t.status=='running' else None)


def reconsider_decision(conn, decision_id, *, action, reason, answer, author='Principal'):
    """Correct a Principal escalation without replacing it with a human reply."""
    if author!='Principal' or os.environ.get('HERMES_KANBAN_TASK'):
        raise WorkflowError('The Principal reconsiders decisions in its own coordinator session')
    if action not in {'continue','approve','changes'}:
        raise WorkflowError('Principal reconsideration requires continue, approve or changes')
    if not isinstance(reason,str) or not reason.strip():
        raise WorkflowError('Principal reconsideration requires a concrete reason')
    if not isinstance(answer,str) or not answer.strip():
        raise WorkflowError('Principal reconsideration requires a concrete answer')
    from hermes_cli.nfos_runtime import previous_runs_termination_pending
    new_id='nd_'+hashlib.sha256(_json([decision_id,action,reason,answer]).encode()).hexdigest()[:24]
    with _kb().write_txn(conn):
        old=get_decision(conn,decision_id)
        if not old:
            raise WorkflowError('Unknown decision')
        wf=get_workflow(conn,old['task_id'])
        if not wf:
            raise WorkflowError('Unknown NFOS card')
        if old['kind']=='impediment':
            # Recovery may be needed precisely because candidate/HML binding is
            # incomplete. Snapshot its identity without approving that binding.
            # Publication reviews still use the strict verifier below.
            state=json.loads(wf['state_json'])
            review_identity={k:state.get(k) for k in (
                'candidate_sha','candidate_tree','homolog_sha','homologation_decision','homolog_evidence')}
            review_identity['instruction_revision']=_kb().get_task(conn,old['task_id']).instruction_revision
        else:
            review_identity=_review_identity(conn,old['task_id'])
        identity={'spec_revision':wf['spec_revision'],'review_identity':review_identity}
        old_context=json.loads(old['context'])
        if old['status']=='superseded':
            current=get_decision(conn,new_id)
            if (old_context.get('superseded_by')!=new_id or not current
                    or current['status']!='resolved'):
                raise WorkflowError('Decision was already reconsidered with a different resolution')
            if json.loads(current['context']).get('reconsideration_identity')!=identity:
                raise WorkflowError('Spec or candidate changed since reconsideration; review the current identity')
            resolve_decision(conn,new_id,action=action,answer=answer,author=author)
            return new_id
        task=_kb().get_task(conn,old['task_id'])
        if old['status']!='human' or not task or task.status!='blocked':
            raise WorkflowError('Reconsideration requires a human decision on a blocked card')
        last=conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',(task.id,)).fetchone()
        if (not last or last['ended_at'] is None
                or _run_process_alive(conn,task.id,last['id'])
                or previous_runs_termination_pending(conn,task.id)):
            raise OwnershipConflict('Confirm the previous run and its children finished termination before reconsidering')
        now=int(time.time())
        context={k:v for k,v in old_context.items() if k not in {'human_reply','superseded_by'}}
        context.update(supersedes=decision_id,reconsideration_reason=reason,
            reconsideration_identity=identity,
            workflow_at_reconsideration={'stage':wf['stage'],'next_action':wf['next_action'],
                'state':json.loads(wf['state_json'])})
        if old['kind']=='review':
            context['review_identity']=identity['review_identity']
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (new_id,task.id,last['id'],old['kind'],old['question'],_json(context),wf['spec_revision'],now))
        resolve_decision(conn,new_id,action=action,answer=answer,author=author)
        old_context['superseded_by']=new_id
        conn.execute("UPDATE nfos_decisions SET status='superseded',context=? WHERE id=?",(_json(old_context),decision_id))
        resolved=get_decision(conn,new_id)
        next_action=(f"Principal reconsidered {decision_id} through {new_id} ({resolved['action']}) "
                     f"under spec r{wf['spec_revision']}. This supersedes the former human escalation. "
                     f"Resume from the preserved work: {resolved['answer']}")
        conn.execute('UPDATE nfos_workflows SET next_action=?,updated_at=? WHERE task_id=?',(next_action,now,task.id))
        another_human=conn.execute("SELECT 1 FROM nfos_decisions WHERE task_id=? AND status='human' LIMIT 1",(task.id,)).fetchone()
        unblocked=False
        if not another_human:
            unblocked=_kb().unblock_task(conn,task.id)
            if not unblocked:
                raise OwnershipConflict('The blocked card changed before reconsideration could resume it')
            if old_context.get('legacy_adoption') and _kb().get_task(conn,task.id).status=='review':
                _kb().reopen_review_task(conn,task.id)
        _event(conn,task.id,last['id'],'nfos_principal_reconsidered',
            {'decision_id':new_id,'supersedes':decision_id,'reason':reason,'action':resolved['action'],
             'answer':resolved['answer'],'identity':identity,'unblocked':unblocked})
        return new_id


def _homologation_context(conn, task_id, context):
    """Snapshot the evidence submitted for a bounded Principal judgment."""
    if _kb().get_task(conn,task_id).delivery_type!='code':
        raise WorkflowError('Homologation binding applies only to code delivery')
    identity=dict(context.get('homologation') or {})
    for key in ('candidate_sha','candidate_tree','homolog_sha','homolog_tree','baseline_sha'):
        value=identity.get(key)
        if not isinstance(value,str) or len(value) not in (40,64) or any(c not in '0123456789abcdef' for c in value):
            raise WorkflowError('Homologation needs exact candidate, tested tree and baseline identities')
    if identity.get('scope') not in {'same_tree','limited_delta'}:
        raise WorkflowError('State whether homologation covers the same tree or a limited delta')
    if identity['scope']=='same_tree' and identity['candidate_tree']!=identity['homolog_tree']:
        raise WorkflowError('Different trees cannot claim same-tree homologation')
    spec=get_spec(conn,task_id)
    criteria=identity.get('criteria')
    if not spec or not isinstance(criteria,list) or not criteria or not all(isinstance(x,str) for x in criteria) or not set(criteria)<={c['id'] for c in json.loads(spec['content'])['criteria']}:
        raise WorkflowError('Homologation must identify the current spec criteria it covers')
    refs=identity.get('evidence')
    if not isinstance(refs,list) or not refs or not all(isinstance(x,str) for x in refs):
        raise WorkflowError('Homologation requires readable local evidence of comparison, baseline and validation')
    checks=[_inspect_local_evidence(path) for path in refs]
    state=json.loads(get_workflow(conn,task_id)['state_json'])
    receipt=_confirmed(conn,task_id,'homolog',identity['homolog_sha'])
    actual=json.loads(receipt['evidence']) if receipt else {}
    if state.get('homolog_sha')!=identity['homolog_sha'] or actual.get('tree')!=identity['homolog_tree']:
        raise WorkflowError('Confirm the real homologation deployment before requesting equivalence')
    return {'homologation':identity,'evidence_checks':checks,
            'instruction_revision':_kb().get_task(conn,task_id).instruction_revision}


def _accept_homologation(conn, decision):
    wf=get_workflow(conn,decision['task_id'])
    context=json.loads(decision['context'])
    if decision['spec_revision']!=wf['spec_revision'] or context!=_homologation_context(conn,decision['task_id'],context):
        raise WorkflowError('Homologation evidence or spec changed; request a new decision')
    state=json.loads(wf['state_json']); identity=context['homologation']
    state.update(candidate_sha=identity['candidate_sha'],candidate_tree=identity['candidate_tree'],
                 homologation_decision=decision['id'])
    conn.execute('UPDATE nfos_workflows SET state_json=?,updated_at=? WHERE task_id=?',
                 (_json(state),int(time.time()),decision['task_id']))
    _event(conn,decision['task_id'],decision['run_id'],'nfos_homologation_bound',
           {'decision_id':decision['id'],'identity':identity,'evidence_checks':context['evidence_checks']})


_PRODUCTION_ORDER_RX = re.compile(  # DELIVERY_ENV_20260911
    r"(subir|sobe|suba|publicar|publique|liberar|libere|promover|promova|deploy|mesclar|mescle|merge|mergear|integrar|integre)"
    r"\W{0,24}(em|para|pra|na|no|to|in|on)?\W{0,12}(produ[cç][aã]o|\bprd\b|production|\bmain\b)", re.I)


def _express_production_order(text):
    """DELIVERY_ENV_20260911: ordem expressa do owner para produção no corpo do card (imperativo + produção), não menção descritiva."""
    return bool(_PRODUCTION_ORDER_RX.search(str(text or '')))


def _project_delivery_environment(conn, task_id):
    """DELIVERY_ENV_20260911: delivery_environment do projeto do card (config), padrão production."""
    try:
        wf = get_workflow(conn, task_id); request = get_request(conn, wf['request_id'])
        project = json.loads(request['payload'])['project']
        key = project.get('board') or project.get('project_id')
        from hermes_cli.nfos_runtime import project_config
        cfg = project_config(key) or {}
        return str(cfg.get('delivery_environment') or 'production').strip().lower()
    except Exception:
        return 'production'


def _delivery_environment_for_db(db_path):
    """DELIVERY_ENV_20260911: ambiente e rota do projeto do board (para o show)."""
    try:
        from hermes_cli.nfos_runtime import project_config, delivery_route
        slug = Path(str(db_path)).resolve().parent.name if db_path else None
        cfg = project_config(slug) if slug else None
        if not cfg:
            from hermes_cli.kanban_db import get_current_board
            cfg = project_config(get_current_board())
        return delivery_route(cfg or {})
    except Exception:
        return None


def _delivery_candidate(conn, task_id, state=None):
    wf=get_workflow(conn,task_id)
    state=json.loads(wf['state_json']) if state is None else state
    candidate=state.get('candidate_sha') or state.get('homolog_sha')
    if candidate==state.get('homolog_sha'):
        return candidate
    if _owner_mode() and state.get('candidate_sha') and _pr_green(conn,task_id,state['candidate_sha']):  # DELIVERY_ENV_20260911: CI verde identifica o candidato
        return state['candidate_sha']
    decision=get_decision(conn,state.get('homologation_decision'))
    context=json.loads(decision['context']) if decision else {}
    identity=context.get('homologation') or {}
    task=_kb().get_task(conn,task_id)
    if (not decision or decision['task_id']!=task_id or decision['kind']!='homologation'
            or decision['status']!='resolved' or decision['action']!='continue' or decision['author']!='Principal'
            or decision['spec_revision']!=wf['spec_revision'] or context.get('instruction_revision')!=task.instruction_revision
            or any(identity.get(k)!=state.get(k) for k in ('candidate_sha','candidate_tree','homolog_sha'))):
        raise WorkflowError('Distinct candidate requires current Principal homologation acceptance')
    if any(_inspect_local_evidence(check['path'])!=check for check in context.get('evidence_checks',[])):
        raise WorkflowError('Accepted homologation evidence changed; request a new decision')
    return candidate


def _review_identity(conn,task_id,*,state=None):
    wf=get_workflow(conn,task_id)
    task=_kb().get_task(conn,task_id)
    if task.delivery_type=='code':
        state=json.loads(wf['state_json']) if state is None else state
        identity={k:state.get(k) for k in ('homolog_sha','candidate_tree','homolog_evidence')}
        if state.get('candidate_sha'):
            from hermes_cli.nfos_destination import destination, review_only
            candidate=state['candidate_sha'] if review_only(destination(conn,task_id)) else _delivery_candidate(conn,task_id,state)
            identity.update(candidate_sha=candidate,homologation_decision=state.get('homologation_decision'))
    else:
        report=_artifact(conn,task_id,'report')
        identity={'report_revision':report['revision'] if report else None}
    # Preserve the identity of existing reviews for unchanged initial cards.
    if task.instruction_revision:
        identity['instruction_revision']=task.instruction_revision
    return identity


def _approved(conn, task_id, revision, *, state=None):
    if not _spec_matches_instruction(conn,task_id):
        return False
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


def _conducted_effect_checks(conn, task, task_id, run_id, operation, target, candidate, scope, staging):
    """Checagens de condução (identidade, revisão, ordem, preparação, slot). Só fora do owner mode; RECORD_MODE_20260911."""
    from hermes_cli.nfos_destination import review_only
    preparation=None
    _fast=bool(review_only(scope) and _owner_mode())  # CODE_FAST_ROUTE_20260910: rota de produção sem homologação
    if review_only(scope) and operation not in ({'pr','merge','deploy'} if _fast else {'pr'}):
        raise WorkflowError('Production route: only pr, merge and deploy effects; no homolog or staging' if _fast
                            else 'The approved delivery destination ends at the review PR; homolog, merge and deploy are outside it')
    if scope and operation=='deploy' and not _fast and (scope['verification_operation']!='deploy' or target!=scope['target']):
        raise WorkflowError('Deploy is outside the approved delivery destination; do not promote beyond the requested environment')
    if scope and operation==scope['verification_operation'] and target!=scope['target']:
        raise WorkflowError('Use the exact delivery destination from the approved spec')
    wf=get_workflow(conn,task_id);state=json.loads(wf['state_json'])
    if not wf['spec_revision']:
        raise WorkflowError('Persist the spec before changing an environment')
    _require_current_instruction_spec(conn,task_id)
    if operation in {'merge','deploy'} and not _approved(conn,task_id,wf['spec_revision']):
        raise WorkflowError('Principal review of this candidate is pending')
    if _fast and operation=='merge' and not _pr_green(conn,task_id,state.get('candidate_sha')):  # CODE_FAST_ROUTE_20260910
        raise WorkflowError('Merge needs the review PR reconciled with ci_status success for the accepted candidate')
    if staging:
        from hermes_cli import nfos_preparation
        preparation=nfos_preparation.authorized(conn,task_id,target,candidate)
        if operation=='staging_merge':
            nfos_preparation.confirmed_pr(conn,task_id,candidate,target)
    if review_only(scope):
        # A review-PR phase has no homologated candidate by definition:
        # the accepted local candidate is what the PR and CI verify.
        delivery_candidate=state.get('candidate_sha')
        if not delivery_candidate:
            raise WorkflowError('Record the accepted local candidate before opening its review PR')
    else:
        delivery_candidate=candidate if staging or operation=='homolog' or (operation=='pr' and _owner_mode()) else _delivery_candidate(conn,task_id,state)  # RELEASE_FLOW_20260911
    if staging or operation=='homolog' or not _owner_mode():  # SLOT_HML_ONLY_20260911: slot só para staging/HML; produção não serializa
        _project_owned(conn,task_id,run_id,delivery_candidate)
    expected=candidate if operation=='homolog' else (state.get('integrated_sha') if operation=='deploy' else delivery_candidate)
    if candidate!=expected:
        raise WorkflowError('Use the confirmed integrated candidate' if operation=='deploy'
                            else 'Use the accepted local candidate' if review_only(scope) else 'Use the homologated candidate')
    if operation=='deploy' and not _confirmed(conn,task_id,'merge',delivery_candidate):
        raise WorkflowError('Confirm the integrated merge before deploy')
    return preparation


def _record_mode_effect_checks(conn, task_id, candidate):
    """RECORD_MODE_20260911: registro, não condução. Spec vigente e SHA exato; o recibo vem no reconcile."""
    wf=get_workflow(conn,task_id)
    if not wf['spec_revision']:
        raise WorkflowError('Persist the spec before changing an environment')
    _require_current_instruction_spec(conn,task_id)
    if not re.fullmatch(r'[0-9a-f]{7,64}', str(candidate or '')):
        raise WorkflowError('candidate must be the exact commit SHA being published (hex)')
    return None

def begin_effect(conn, task_id, run_id, *, operation, target, candidate):
    if operation not in {'homolog','pr','merge','deploy','staging_pr','staging_merge','repair'} or not target or not candidate:  # RESULT_PROBE_20260911: repair = mutação de dado
        raise WorkflowError('External effect needs operation, destination and exact candidate')
    with _kb().write_txn(conn):
        task=_owned(conn,task_id,run_id)
        if _scope_needs_new_spec(get_workflow(conn,task_id)):
            raise WorkflowError('Principal changed the primary task; persist a newer spec before external effects')
        from hermes_cli.nfos_principal_review import require_spec
        require_spec(conn,task_id)
        if operation=='repair':  # RESULT_PROBE_20260911: registro da mutação de dado (preimage como candidato); a sonda mede depois
            if not get_workflow(conn,task_id)['spec_revision']:
                raise WorkflowError('Persist the spec before changing production data')
            _require_current_instruction_spec(conn,task_id)
        if task.delivery_type in {'report','operation'} and operation!='repair':  # OPERATION_FAST_20260910: card sem código não publica código
            raise WorkflowError('A report/operation card publishes no code: homolog, pr, merge, deploy and staging effects are refused. If the request needs a repository change, save a spec with delivery_type code (allowed from a worktree workspace) and do the repository/HML/PR reconciliation first.')
        staging=operation in {'staging_pr','staging_merge'}
        preparation=None
        if staging and task.delivery_type!='code':
            raise WorkflowError('Staging preparation applies only to code delivery')
        if task.delivery_type=='code' and operation!='repair':
            from hermes_cli.nfos_destination import destination, review_only
            scope=destination(conn,task_id)
            if operation in {'merge','deploy'} and _project_delivery_environment(conn,task_id)=='hml' and not _express_production_order(task.body):  # DELIVERY_ENV_20260911
                raise WorkflowError('This project delivers in HML: staging PR, HML deploy and HML readback close the card. Production (merge or deploy on main) only with the owner express order in the card body')
            if _owner_mode():  # RECORD_MODE_20260911: o runtime registra a publicação (task, run, operação, alvo, SHA) e não conduz
                preparation=_record_mode_effect_checks(conn,task_id,candidate)
            else:
                preparation=_conducted_effect_checks(conn,task,task_id,run_id,operation,target,candidate,scope,staging)
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
        if preparation:
            conn.execute('UPDATE nfos_effects SET evidence=? WHERE id=?',(_json(preparation),key))
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
        staging=effect['operation'] in {'staging_pr','staging_merge'}
        if staging:
            from hermes_cli import nfos_preparation
            wf=get_workflow(conn,task.id);state=json.loads(wf['state_json'])
            evidence=nfos_preparation.reconcile(conn,effect,found,evidence,state)
            conn.execute('UPDATE nfos_workflows SET state_json=?,updated_at=? WHERE task_id=?',
                         (_json(state),int(time.time()),task.id))
        if found and task.delivery_type=='code' and not staging:
            wf=get_workflow(conn,task.id);state=json.loads(wf['state_json'])
            if evidence.get('candidate')!=effect['candidate']:
                raise WorkflowError('Destination readback must identify this exact candidate')
            if effect['operation']=='homolog':
                if not evidence.get('tree') or not evidence.get('artifact'):
                    raise WorkflowError('Read the actual homologation tree and deployed artifact')
                state['homolog_deployment']=evidence
            if effect['operation'] in {'merge','deploy'} and not _owner_mode():  # RELEASE_FLOW_20260911: em owner mode a verificação é o readback de produção
                if evidence.get('tree')!=state.get('candidate_tree'):
                    raise WorkflowError('Integrated tree differs from homologation; verify the new tree in homolog first')
            if effect['operation']=='pr':
                from hermes_cli.nfos_destination import destination, review_only, review_pr_problem
                if _owner_mode() and not review_only(destination(conn,task.id)) and evidence.get('candidate'):  # RELEASE_FLOW_20260911: PR com CI define o candidato (rebase)
                    state['candidate_sha']=evidence['candidate']
                    state['candidate_tree']=evidence.get('tree') or state.get('candidate_tree')
                if review_only(destination(conn,task.id)):
                    if evidence.get('tree')!=state.get('candidate_tree'):
                        raise WorkflowError('Review PR readback must show the accepted candidate tree')
                    problem=review_pr_problem(evidence)
                    if problem:
                        raise WorkflowError(problem)
                    state['review_pr']=evidence
            if effect['operation']=='merge':
                if not evidence.get('integrated_sha'):
                    raise WorkflowError('Read the exact integrated SHA')
                state['integrated_sha']=evidence['integrated_sha']
            if effect['operation']=='deploy':
                if not evidence.get('artifact') or not evidence.get('behavior_evidence'):
                    raise WorkflowError('Read the deployed artifact and verify actual behavior')
                from hermes_cli.nfos_destination import destination
                scope=destination(conn,task.id)
                state.update(artifact=evidence['artifact'])
                state['delivery_readback' if scope else 'production_readback']=evidence
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
    if not report or not spec or not _spec_matches_instruction(conn,task_id):
        return None
    try:
        content=json.loads(report['content']); metadata=json.loads(report['evidence'])
        results=_report_results(spec,content)
        strict=not _owner_mode()  # BLOCK_LESS4/BLOCK_LESS8_20260910: fora do owner mode a prova completa continua exigida
        _fails=[c for c,row in results.items() if ((row['status']!='PASS') if strict else (row['status']=='FAIL'))]
        _partial_ok=(not strict) and bool(_fails) and _continuation_allows_partial(conn,task_id,content)  # RECORD_CONTINUATION_20260911
        if not strict and str(content.get('disposition') or '')!='cancelled_by_owner' and mandatory_pending(conn,task_id,spec):  # RESULT_PROBE_20260911
            return None
        if _fails and not _partial_ok:
            return None
        digest=hashlib.sha256(report['content'].encode()).hexdigest()
        if (metadata.get('schema_version')!=1 or metadata.get('report_sha256')!=digest
                or metadata.get('spec_revision')!=spec['revision']):
            return None
        strict=not _owner_mode()  # BLOCK_LESS2/BLOCK_LESS8_20260910
        proved=set()
        for check in (metadata.get('artifact_checks',[]) if strict else []):
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
        if any(((row['status']!='PASS') if strict else (row['status']=='FAIL' and not _partial_ok)) or (strict and criterion not in proved) for criterion,row in results.items()):  # BLOCK_LESS2/BLOCK_LESS4_20260910
            return None
        return {'report_id':report['id'],'report_revision':report['revision'],
                'report_sha256':digest,'spec_revision':spec['revision'],
                'evidence_sha256':hashlib.sha256(report['evidence'].encode()).hexdigest()}
    except (WorkflowError,KeyError,TypeError,ValueError):
        return None


def _credentials_hint(db_path):
    """BLOCK_LESS6_20260910: cofre do projeto do board (nomes de arquivos e de chaves, nunca valores)."""
    try:
        home=os.environ.get('HERMES_HOME') or ''
        vault=Path(home)/'secrets' if home else None
        if not vault or not vault.is_dir():
            return None
        slug=Path(db_path).resolve().parent.name
        keys={slug, slug.split('--')[-1], slug.split('-')[0]}
        files=[]
        for entry in sorted(vault.iterdir()):
            if not any(entry.name.startswith(k) for k in keys if k):
                continue
            paths=[entry] if entry.is_file() else sorted(p for p in entry.rglob('*') if p.is_file())
            for p in paths[:40]:
                item={'path':str(p)}
                if p.suffix=='.env':
                    names=[]
                    for line in p.read_text(encoding='utf-8',errors='replace').splitlines():
                        if '=' in line and not line.lstrip().startswith('#'):
                            names.append(line.split('=',1)[0].strip())
                    item['keys']=sorted(set(names))
                files.append(item)
        if not files:
            return {'vault':str(vault),'files':[],'note':'Nenhum arquivo deste projeto no cofre. Se a tarefa exige credencial, bloqueie com pergunta ao owner indicando o caminho onde ela deve ser colocada.'}
        return {'vault':str(vault),'files':files,
                'how':'Carregue um .env dentro do seu comando: set -a; . <arquivo>; set +a. Arquivos .json são estados de sessão/acessos: leia e use. Nunca cole valores em cards, relatórios, commits ou chat.'}
    except Exception:
        return None


def completion_ready(conn, task_id, *, evidence_check=None):
    wf=get_workflow(conn,task_id)
    if wf is None:
        return True
    if active_suspension(conn, task_id):
        return False
    from hermes_cli.nfos_principal_review import accepted
    if not accepted(conn,task_id,'spec_review') or not accepted(conn,task_id,'final_review'):
        return False
    if _scope_needs_new_spec(wf) or not _spec_matches_instruction(conn,task_id):
        return False
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
    if _owner_mode():  # BLOCK_LESS4/BLOCK_LESS8_20260910: relatório coerente fecha qualquer tipo de card
        return True
    try:
        if not _approved(conn,task_id,wf['spec_revision']):
            return False
    except WorkflowError:
        return False
    task=_kb().get_task(conn,task_id)
    if task.delivery_type=='code':
        state=json.loads(wf['state_json'])
        from hermes_cli.nfos_destination import destination, verified, review_only
        scope=destination(conn,task_id)
        if review_only(scope):
            required=('candidate_sha','candidate_tree')
        else:
            required=('homolog_sha','integrated_sha') if scope else ('homolog_sha','integrated_sha','artifact','production_readback')
        if any(not state.get(k) for k in required):
            return False
        if scope and not verified(conn,task_id,state):
            return False
        if review_only(scope):
            effects=[('pr',state['candidate_sha'])]
        else:
            effects=[('pr',_delivery_candidate(conn,task_id,state)),('merge',_delivery_candidate(conn,task_id,state))]
            if not scope:
                effects.append(('deploy',state['integrated_sha']))
        if not all(_confirmed(conn,task_id,operation,candidate) for operation,candidate in
                effects):
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


def _slot_stale(conn, current, minutes=30):
    """SLOT_STALE_20260911: dono vivo do slot sem publicação andando: todos os efeitos unknown dele têm mais de `minutes`
    e nenhum efeito seu foi atualizado nesse intervalo. Em owner mode outro card pode tomar o slot."""
    try:
        if not _owner_mode():
            return False
        limit = int(time.time()) - minutes * 60
        recent = conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND (updated_at>? OR created_at>?) LIMIT 1",
                              (current['task_id'], limit, limit)).fetchone()
        if recent:
            return False
        return int(current['acquired_at'] or 0) < limit
    except Exception:
        return False


def _urgent_preempts_slot(conn, task_id, current):
    """SLOT_PREEMPT_20260910 (ordem do Maikol): card urgente (priority >= URGENT_PRIORITY) toma o slot de um dono vivo
    que não tem publicação em voo (nenhum efeito unknown em homolog/merge/deploy/staging_*) e que não é urgente."""
    try:
        mine=conn.execute('SELECT priority FROM tasks WHERE id=?',(task_id,)).fetchone()
        theirs=conn.execute('SELECT priority FROM tasks WHERE id=?',(current['task_id'],)).fetchone()
        if not mine or int(mine[0] or 0)<URGENT_PRIORITY or (theirs and int(theirs[0] or 0)>=URGENT_PRIORITY):
            return False
        inflight=conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('homolog','merge','deploy','staging_pr','staging_merge')",
                              (current['task_id'],)).fetchone()
        return inflight is None
    except Exception:
        return False


def acquire_project(conn, project, task_id, run_id, candidate):
    with _kb().write_txn(conn):
        _owned(conn,task_id,run_id)
        current=_row(conn,'nfos_project_delivery','project',project)
        if _owner_mode():  # RECORD_MODE_20260911: o slot registra quem publica, não trava
            if current and (current['task_id'],current['run_id'])!=(task_id,run_id):
                _event(conn,current['task_id'],current['run_id'],'nfos_project_delivery_shared',{'project':project,'with_task':task_id,'with_run':run_id})
            conn.execute('INSERT OR REPLACE INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)',
                         (project,task_id,run_id,candidate,int(time.time())))
            _event(conn,task_id,run_id,'nfos_project_delivery_acquired',{'project':project,'candidate':candidate,'record_only':True})
            return True
        if current:
            if (current['task_id'],current['run_id'],current['candidate'])==(task_id,run_id,candidate):
                return True
            previous=conn.execute('SELECT ended_at FROM task_runs WHERE id=? AND task_id=?',
                                  (current['run_id'],current['task_id'])).fetchone()
            if not previous or not previous['ended_at'] or _run_process_alive(conn,current['task_id'],current['run_id']):
                _stale=_slot_stale(conn,current)  # SLOT_STALE_20260911
                if not _urgent_preempts_slot(conn,task_id,current) and not _stale:  # SLOT_PREEMPT_20260910
                    return False
                conn.execute('DELETE FROM nfos_project_delivery WHERE project=?',(project,))
                _event(conn,current['task_id'],current['run_id'],'nfos_project_delivery_preempted',
                       {'project':project,'by_task':task_id,'by_run':run_id,'candidate':current['candidate']})
                _kb().add_comment(conn,current['task_id'],'nfos-runtime',
                    '[slot] slot de publicação do projeto '+str(project)+' tomado por '+task_id+
                    (' (urgente, priority >= 100)' if not _stale else ' (sua publicação estava parada há mais de 30 min, sem efeito reconciliado)')+
                    '. Quando voltar a publicar em staging/HML, readquira com acquire-project --wait 900.')  # SLOT_STALE_20260911
                conn.execute('INSERT INTO nfos_project_delivery(project,task_id,run_id,candidate,acquired_at) VALUES(?,?,?,?,?)',
                             (project,task_id,run_id,candidate,int(time.time())))
                _event(conn,task_id,run_id,'nfos_project_delivery_acquired',{'project':project,'candidate':candidate,'preempted':current['task_id']})
                return True
            unknown=conn.execute("SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('homolog','merge','deploy','staging_pr','staging_merge')",
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
        if not _owner_mode() and conn.execute(  # RECORD_MODE_20260911
"SELECT 1 FROM nfos_effects WHERE task_id=? AND status='unknown' AND operation IN ('homolog','merge','deploy','staging_pr','staging_merge')",(task_id,)).fetchone():
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
    parser.add_argument('action',choices=['show','probe','lesson','precheck','cancel','save-spec','save-report','progress','ask','decide',
        'pending','effect','reconcile','reconcile-spec','repair-workspace','repair-card','acquire-project','release-project','receive','resume','wait','reconsider'])
    parser.add_argument('--task',default=os.environ.get('HERMES_KANBAN_TASK'))
    parser.add_argument('--run',type=int,default=int(os.environ.get('HERMES_KANBAN_RUN_ID') or 0))
    parser.add_argument('--input',help='JSON file with spec/report/state/question/receipt/request')
    parser.add_argument('--db',help='Exact board database supplied by the current gateway event')
    parser.add_argument('--evidence',help='JSON file with the TL session/output and fallback reason when applicable')
    parser.add_argument('--author',default='Claude TL',choices=['Claude TL','Codex','worker'])  # BLOCK_LESS_20260910
    parser.add_argument('--stage')
    parser.add_argument('--next',dest='next_action',default='')
    parser.add_argument('--kind',choices=['review','spec_review','final_review','impediment','additional_tasks','homologation','preparation'])
    parser.add_argument('--decision')
    parser.add_argument('--timeout',type=float,default=300,help='Maximum wait duration; pending is not failure')
    parser.add_argument('--resolution',choices=['continue','approve','changes','human'])
    parser.add_argument('--operation',choices=['homolog','pr','merge','deploy','staging_pr','staging_merge','repair'])  # RESULT_PROBE_20260911
    parser.add_argument('--criterion',help='Criterion id to measure (probe)')  # RESULT_PROBE_20260911
    parser.add_argument('--all',action='store_true',help='Measure every criterion that has a probe')  # RESULT_PROBE_20260911
    parser.add_argument('--hypothesis',default='',help='progress: hypothesis for the next attempt')  # RESULT_PROBE_20260911
    parser.add_argument('--change',default='',help='progress: bounded change of the next attempt')  # RESULT_PROBE_20260911
    parser.add_argument('--target')
    parser.add_argument('--candidate')
    parser.add_argument('--wait',type=int,default=0,help='Seconds to keep trying the project slot (max 900); BLOCK_LESS_20260910')
    parser.add_argument('--effect-id')
    parser.add_argument('--project',default=os.environ.get('HERMES_KANBAN_BOARD'))
    args=parser.parse_args()
    payload=json.loads(Path(args.input).read_text(encoding='utf-8-sig')) if args.input else {}
    evidence=json.loads(Path(args.evidence).read_text(encoding='utf-8-sig')) if args.evidence else {}
    with _kb().connect_closing(db_path=Path(args.db) if args.db else None) as conn:
        if args.action=='show':
            result={'workflow':get_workflow(conn,args.task),'spec':get_spec(conn,args.task),
                'runtime':{'code_root':str(Path(__file__).resolve().parents[1]),'python':sys.executable},
                'report':_artifact(conn,args.task,'report'),
                'decisions':[dict(r) for r in conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? ORDER BY created_at',(args.task,))],
                'effects':[dict(r) for r in conn.execute('SELECT * FROM nfos_effects WHERE task_id=?',(args.task,))]}
            try:  # BLOCK_LESS3_20260910: premissas do owner na primeira chamada de todo worker
                from hermes_cli.nfos_principal_review import required as _req
                if not _req(conn,args.task):
                    from hermes_cli.nfos_runtime import owner_premises as _op  # RECORD_MODE_20260911
                    result['owner_premises']=_op()
            except Exception:
                pass
            result['credentials']=_credentials_hint(args.db)  # BLOCK_LESS6_20260910
            result['delivery_environment']=_delivery_environment_for_db(args.db)  # DELIVERY_ENV_20260911
            result['continuation']=continuation_links(conn,args.task)  # RECORD_CONTINUATION_20260911
            try:  # RESULT_PROBE_20260911: medições, tentativas e nota de debug
                _st=(json.loads(result['workflow']['state_json'] or '{}') or {}) if result['workflow'] else {}
                result['measurements']=_st.get('measurements'); result['attempts']=_st.get('attempts')
                result['debug_note']=completion_refusal_note(conn,args.task)
                result['learning']=worker_context(conn,args.task)  # LEARNING_20260911
            except Exception:
                pass
            if result['workflow']:
                result['request']=get_request(conn,result['workflow']['request_id'])
                result['production_precheck']=(json.loads(result['workflow']['state_json'] or '{}') or {}).get('production_precheck')  # BLOCK_LESS7_20260910
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_tool_calls'").fetchone():
                from hermes_cli.nfos_tool import read_calls
                result['native_calls']=read_calls(conn,args.task)
        elif args.action=='precheck':  # BLOCK_LESS7_20260910
            result={'precheck':record_precheck(conn,args.task,args.run,payload)}
        elif args.action=='cancel':
            from hermes_cli.kanban_cancellation import cancel_task
            cancel_task(conn,args.task,metadata=payload,
                        expected_run_id=args.run if os.environ.get('HERMES_KANBAN_TASK') else None)
            result={'task_id':args.task,'status':'done','disposition':'cancelled_by_owner','functional_delivery':False}
        elif args.action=='save-spec':
            result={'revision':save_spec(conn,args.task,args.run,payload,author=args.author,evidence=evidence)}
        elif args.action=='save-report':
            save_report(conn,args.task,args.run,payload);result={'saved':True}
        elif args.action=='reconcile-spec':
            result=reconcile_legacy_spec(conn,args.task,**payload)
        elif args.action=='repair-workspace':
            from hermes_cli.nfos_workspace_repair import repair_workspace
            result=repair_workspace(conn,args.task,board=args.project,**payload)
        elif args.action=='repair-card':
            from hermes_cli.nfos_workspace_repair import repair_card
            result=repair_card(conn,args.task,board=args.project,**payload)
        elif args.action=='lesson':  # LEARNING_20260911
            result=lesson_command(conn,payload,author=(os.environ.get('HERMES_KANBAN_TASK') or 'Principal'))
        elif args.action=='probe':  # RESULT_PROBE_20260911
            result={'measurements':run_probes(conn,args.task,args.run,criterion=args.criterion,all_=bool(args.all))}
        elif args.action=='progress':
            if args.hypothesis or args.change:  # RESULT_PROBE_20260911: tentativa registrada no card
                _wf=get_workflow(conn,args.task); _st=(json.loads(_wf['state_json'] or '{}') or {}) if _wf else {}
                _att=list(_st.get('attempts') or []); _att.append({'at':int(time.time()),'run_id':args.run,'hypothesis':args.hypothesis,'change':args.change})
                payload=dict(payload,attempts=_att[-30:])
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
            if args.resolution=='human':  # BLOCK_LESS_20260910: na fronteira do principal, human só com pergunta concreta e destinatário
                if not _human_question_valid(payload.get('human_question'),payload.get('human_to')):
                    raise WorkflowError('human exige human_question (com "?") e human_to (quem responde). Pausa técnica não é human: use continue ou changes.')
                payload['answer']='PERGUNTA para '+str(payload['human_to']).strip()+': '+str(payload['human_question']).strip()+'\n'+str(payload.get('answer') or '').strip()
            resolve_decision(conn,args.decision,action=args.resolution,answer=payload['answer'],author='Principal',
                             proposal=payload.get('proposal'),assessment=payload.get('assessment'))
            result={'saved':True,'decision':get_decision(conn,args.decision)}
        elif args.action=='reconsider':
            decision_id=reconsider_decision(conn,args.decision,action=args.resolution,
                reason=payload.get('reason'),answer=payload.get('answer'),author='Principal')
            decision=get_decision(conn,decision_id)
            result={'decision_id':decision_id,'decision':decision,
                    'task_status':_kb().get_task(conn,decision['task_id']).status}
        elif args.action=='effect':
            result=begin_effect(conn,args.task,args.run,operation=args.operation,target=args.target,candidate=args.candidate)
        elif args.action=='reconcile':
            reconcile_effect(conn,args.effect_id,found=payload['found'],evidence=payload['evidence'],
                caller_task_id=args.task,caller_run_id=args.run);result={'saved':True}
        elif args.action=='acquire-project':
            wait=max(0,min(900,int(args.wait or 0)))  # BLOCK_LESS_20260910: espera o slot em vez de perguntar ao principal
            started=time.time(); acquired=acquire_project(conn,args.project,args.task,args.run,args.candidate)
            while not acquired and time.time()-started<wait:
                time.sleep(min(30,max(1,wait-(time.time()-started))))
                acquired=acquire_project(conn,args.project,args.task,args.run,args.candidate)
            result={'acquired':acquired,'waited_seconds':int(time.time()-started)}
        elif args.action=='release-project':
            result={'released':release_project(conn,args.project,args.task,args.run)}
        elif args.action=='receive':
            if os.environ.get('HERMES_KANBAN_TASK'):
                raise WorkflowError('The Principal dispatches additional tasks; use ask --kind additional_tasks')
            result={'request_id':receive_request(conn,source=payload['source'],text=payload['text'],
                project=payload['project'],attachments=payload.get('attachments',[]),part=payload.get('part','0'))}
        print(_json(result))


if __name__=='__main__':
    try:
        main()
    except WorkflowError as exc:
        print(_json({'error':str(exc),'type':type(exc).__name__}))
        raise SystemExit(1)
