"""NFOS request bootstrap inside the existing Kanban dispatcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery


def project_config(board, config=None):
    if config is None:
        from hermes_cli.config import load_config
        config=load_config()
    settings=((config or {}).get('kanban') or {}).get('delivery') or {}
    projects=settings.get('projects') or {}
    project=projects.get(board)
    return dict(project,board=board) if isinstance(project,dict) and project.get('enabled') is True else None


def adopt_existing_tasks(conn, *, board, project):
    """Enroll idle retained work in the same dispatcher, without replaying it.

    The task and its run/file history remain authoritative. This is an attached
    recovery request, never a new transport message or a synthetic worker run.
    A live old executor keeps its protocol until it exits; a later tick adopts
    its unfinished card before any replacement can be dispatched.
    """
    adopted=[]
    ids=conn.execute("""SELECT id FROM tasks WHERE task_role='work'
        AND status NOT IN ('running','done','archived') AND current_run_id IS NULL
        AND id NOT IN (SELECT task_id FROM nfos_workflows) ORDER BY created_at,id""").fetchall()
    for candidate in ids:
        with kb.write_txn(conn):
            task=kb.get_task(conn,candidate['id'])
            if (task.status in {'running','done','archived'} or task.current_run_id
                    or delivery.get_workflow(conn,task.id)):
                continue
            if task.worker_pid and (kb._process_identity_matches(task.worker_pid,task.worker_started_at)
                                    if task.worker_started_at is not None else kb._pid_alive(task.worker_pid)):
                continue
            runs=conn.execute('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC',(task.id,)).fetchall()
            if any(run_termination_pending(conn,task.id,run['id']) for run in runs):
                continue
            # Explicit intake recovery already owns this card. It must keep
            # its original request identity rather than race the rollout.
            if conn.execute("""SELECT 1 FROM nfos_requests WHERE status IN ('pending','starting')
                AND json_extract(payload,'$.project.existing_task_id')=?""",(task.id,)).fetchone():
                continue
            sub=conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=? ORDER BY created_at,platform,chat_id,thread_id LIMIT 1',(task.id,)).fetchone()
            source=(dict(platform=sub['platform'],chat_id=sub['chat_id'],thread_id=sub['thread_id'],
                         profile=sub['notifier_profile']) if sub else dict(project.get('source') or {}))
            source.update(message_id=task.id,message_identity_kind='retained-card')
            source.setdefault('platform','kanban');source.setdefault('chat_id',board)
            source.setdefault('thread_id',board)
            profile=task.assignee or project.get('profile','default')
            retained_project=dict(project,profile=profile,board=board)
            retained_project.pop('existing_task_id',None)
            source_key=delivery._json(['retained-card',board,task.id])
            request_id='req_'+hashlib.sha256(source_key.encode()).hexdigest()[:24]
            last_run=runs[0]['id'] if runs else None
            origin={'kind':'retained-card','board':board,'task_id':task.id,
                    'last_run_id':last_run,'status':task.status,
                    'workspace_path':task.workspace_path,'branch_name':task.branch_name,
                    'previous_goal_mode':task.goal_mode,'previous_delivery_type':task.delivery_type}
            attachments=[dict(row) for row in conn.execute('SELECT * FROM task_attachments WHERE task_id=?',(task.id,))]
            payload={'source':source,'text':task.body or task.title,'project':retained_project,
                     'attachments':attachments,'origin':origin}
            now=int(time.time())
            conn.execute("INSERT INTO nfos_requests(id,source_key,payload,status,task_id,created_at) VALUES(?,?,?,'attached',?,?)",
                         (request_id,source_key,delivery._json(payload),task.id,now))
            conn.execute('INSERT INTO nfos_workflows(task_id,request_id,state_json,next_action,updated_at) VALUES(?,?,?,?,?)',
                         (task.id,request_id,delivery._json({'legacy_adoption':origin}),
                          'Read retained history and evidence; persist missing NFOS spec, then continue the unfinished step',now))
            # The same NFOS delivery authority used for new cards replaces
            # the old review gate. Preserve all old receipts for readback.
            conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task.id,))
            conn.execute('UPDATE tasks SET goal_mode=1 WHERE id=?',(task.id,))
            if task.assignee is None:
                conn.execute('UPDATE tasks SET assignee=? WHERE id=?',(profile,task.id))
            if task.delivery_type is None:
                conn.execute('UPDATE tasks SET delivery_type=? WHERE id=?',
                             (project.get('delivery_type','code'),task.id))
            if source.get('platform')=='telegram' and source.get('chat_id') and source.get('thread_id'):
                # Older subscriptions only displayed notifications. NFOS
                # also persists the wake obligation for its Principal. The
                # existing upsert preserves the subscription's read cursor.
                kb.add_notify_sub(conn,task_id=task.id,platform='telegram',chat_id=source['chat_id'],
                    thread_id=source['thread_id'],notifier_profile=source.get('profile') or profile,
                    chat_type=None if sub else source.get('chat_type','group'),
                    delivery_mode='notify+wake')
            kb._append_event(conn,task.id,'nfos_legacy_adopted',dict(origin,request_id=request_id))
            if task.status in {'blocked','review'}:
                event=conn.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN ('blocked','gave_up','review_requested') ORDER BY id DESC LIMIT 1",(task.id,)).fetchone()
                context={'legacy_adoption':origin,'last_event':dict(event) if event else None,
                         'instruction_revision':task.instruction_revision}
                question=('Reavaliar o impedimento registrado no histórico e retomar o mesmo card se resolvível.'
                          if task.status=='blocked' else
                          'Conferir a entrega preservada e retomar no mesmo card para registrar a spec e evidências no NFOS.')
                decision_id='dec_'+hashlib.sha256(source_key.encode()).hexdigest()[:24]
                # Zero explicitly means this retained card has no historical
                # run. Do not manufacture a run merely to ask its Principal.
                conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,0,?)',
                             (decision_id,task.id,last_run or 0,'impediment',question,delivery._json(context),now))
                kb._append_event(conn,task.id,'nfos_principal_requested',
                    {'decision_id':decision_id,'kind':'impediment','question':question,'retained_card':True},run_id=last_run)
            adopted.append(task.id)
    return adopted


def _sync_directory(directory):
    # Production runs on Linux. Flushing the file alone does not persist its
    # rename or newly created parent directory entries across a power loss.
    if os.name=='posix':
        fd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def preserve_attachments(paths, types, *, directory):
    """Copy originals before receipt; publish each copy atomically by content hash."""
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    attachments=[]
    for index,value in enumerate(paths):
        src=Path(value)
        if not src.is_file():
            raise delivery.WorkflowError(f'Original attachment is unavailable: {src.name}')
        digest=hashlib.sha256()
        with src.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
        sha=digest.hexdigest();dest=directory/(sha+src.suffix.lower())
        if not dest.exists():
            # Concurrent Telegram handlers can run in threads of the same
            # process. Each copy owns its temporary file until atomic publish.
            temp=directory/(sha+'.'+uuid.uuid4().hex+'.tmp')
            try:
                with src.open('rb') as source,temp.open('wb') as target:
                    shutil.copyfileobj(source,target);target.flush();os.fsync(target.fileno())
                os.replace(temp,dest)
            finally:
                temp.unlink(missing_ok=True)
        attachments.append({'original':str(dest),'source_path':str(src),'sha256':sha,
                            'mime_type':types[index] if index<len(types) else ''})
    # A previous attempt may have created these directories but failed before
    # flushing their parent links. Existence on retry is not durability proof.
    # Flush the ancestry as well, without a second recovery marker/store.
    for parent in (directory,*directory.parents):
        _sync_directory(parent)
    return attachments


def reconcile_starting(conn, *, now=None):
    """Recover bootstrap reservations, never replace a live process identity."""
    now=int(time.time()) if now is None else now
    rows=conn.execute("SELECT * FROM nfos_requests WHERE status='starting'").fetchall()
    recovered=[]
    for row in rows:
        pid=row['worker_pid']
        if pid:
            current=kb._process_start_time(pid)
            if current is not None and current==row['worker_started_at']:
                continue
        elif now-(row['claimed_at'] or now)<30:
            continue
        with kb.write_txn(conn):
            changed=conn.execute("UPDATE nfos_requests SET status='pending',claim_token=NULL,worker_pid=NULL,worker_started_at=NULL,last_error='Bootstrap interrupted before card attachment' WHERE id=? AND status='starting' AND claim_token=?",
                                 (row['id'],row['claim_token'])).rowcount
            if changed:
                recovered.append(row['id'])
    return recovered


def dispatch_requests(conn, *, board, capacity, spawn_limit=None):
    """Called only under the canonical board dispatcher lock."""
    reconcile_starting(conn)
    started=[]
    while spawn_limit is None or len(started)<spawn_limit:
        request=delivery.reserve_request(conn,capacity=capacity)
        if request is None:
            break
        db_path=next(row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main')
        root=Path(__file__).resolve().parents[1]
        cmd=[sys.executable,'-m','hermes_cli.nfos_runtime','bootstrap','--db',db_path,
             '--board',board,'--request',request['id'],'--token',request['claim_token']]
        log_dir=Path(db_path).parent/'logs';log_dir.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ);env['PYTHONPATH']=str(root)
        try:
            with (log_dir/(request['id']+'.log')).open('ab') as log:
                proc=subprocess.Popen(cmd,cwd=root,env=env,stdin=subprocess.DEVNULL,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            with kb.write_txn(conn):
                conn.execute("UPDATE nfos_requests SET worker_pid=?,worker_started_at=? WHERE id=? AND status='starting' AND claim_token=?",
                    (proc.pid,kb._process_start_time(proc.pid),request['id'],request['claim_token']))
            started.append(request['id'])
        except OSError as exc:
            with kb.write_txn(conn):
                conn.execute("UPDATE nfos_requests SET status='pending',claim_token=NULL,last_error=? WHERE id=? AND status='starting'",
                             (str(exc),request['id']))
            break
    return started


def _cleanup_metadata(conn, run_id):
    row=conn.execute('SELECT metadata FROM task_runs WHERE id=?',(run_id,)).fetchone()
    return json.loads(row['metadata'] or '{}') if row else {}


def _save_cleanup(conn, run, receipt, *, event=None):
    with kb.write_txn(conn):
        metadata=_cleanup_metadata(conn,run['id'])
        previous=metadata.get('nfos_cleanup')
        metadata['nfos_cleanup']=receipt
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=? AND task_id=?',
                     (json.dumps(metadata,ensure_ascii=False),run['id'],run['task_id']))
        if event and previous!=receipt:
            kb._append_event(conn,run['task_id'],event,receipt,run_id=run['id'])


def _worker_identity_receipt(conn, run):
    rows=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND run_id=? AND kind IN ('spawned','nfos_worker_created_card','nfos_worker_recovered_card') ORDER BY id DESC",
                      (run['task_id'],run['id'])).fetchall()
    for row in rows:
        payload=json.loads(row['payload'] or '{}')
        if payload.get('pid') and (not run['worker_pid'] or payload['pid']==run['worker_pid']):
            return {'worker_pid':payload['pid'],'worker_started_at':payload.get('worker_started_at'),
                    'source_event_id':row['id']}
    return {'worker_pid':run['worker_pid'],'worker_started_at':None,'source_event_id':None}


def run_termination_pending(conn, task_id, run_id):
    """Neither a human answer nor a retry may replace known live work."""
    if run_id is None:
        return False
    from hermes_cli import nfos_tool as tool
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_tool_calls'").fetchone():
        if any(call['status'] in tool.ACTIVE or tool.call_runner_alive(call)
               for call in tool.read_calls(conn,task_id,run_id)):
            return True
    run=conn.execute('SELECT * FROM task_runs WHERE task_id=? AND id=?',(task_id,run_id)).fetchone()
    if not run:
        return False
    receipt=_cleanup_metadata(conn,run_id).get('nfos_cleanup') or _worker_identity_receipt(conn,run)
    if receipt.get('status')=='confirmed':
        return False
    if receipt.get('worker_started_at') is None and receipt.get('worker_pid'):
        return kb._pid_alive(receipt['worker_pid'])
    identities=json.loads(receipt.get('descendants_json') or '[]')
    identities.append({'pid':receipt.get('worker_pid'),'started_at':receipt.get('worker_started_at')})
    return any(tool._matches(item['pid'],item['started_at']) for item in identities)


def previous_runs_termination_pending(conn, task_id):
    """Read-only predicate called inside the atomic claim transaction.

    Reclaim clears the card's current pointer, so the run history is the
    authority for old process receipts. Cards outside NFOS retain their
    existing claim behavior. Process termination stays outside this transaction
    in the canonical runtime reconciliation.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_workflows'").fetchone():
        return False
    if not conn.execute('SELECT 1 FROM nfos_workflows WHERE task_id=?',(task_id,)).fetchone():
        return False
    return any(run_termination_pending(conn,task_id,row['id']) for row in conn.execute(
        'SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC',(task_id,)).fetchall())


def reconcile_terminal_workers(conn, *, worker_exit_grace_seconds=15):
    """Clean only closed runs. Pending Principal decisions keep their worker."""
    from hermes_cli import nfos_tool as tool
    rows=conn.execute("""SELECT r.* FROM task_runs r JOIN nfos_workflows w ON w.task_id=r.task_id
        WHERE r.ended_at IS NOT NULL AND (r.status<>'blocked' OR (r.status='blocked' AND EXISTS (
            SELECT 1 FROM nfos_decisions d WHERE d.task_id=r.task_id AND d.run_id=r.id
            AND (d.status='human' OR (d.action='continue' AND json_extract(d.context,'$.human_reply') IS NOT NULL)))))
        ORDER BY r.ended_at,r.id""").fetchall()
    changed=[]
    for run in rows:
        receipt=_cleanup_metadata(conn,run['id']).get('nfos_cleanup')
        if receipt and receipt.get('status')=='confirmed':
            continue
        task=kb.get_task(conn,run['task_id'])
        if not task:
            continue
        if receipt is None:
            # A completed/human-blocked worker gets time to flush its final
            # state. An interrupted run has already lost execution ownership;
            # its surviving commands must stop before a replacement can claim.
            grace=float(worker_exit_grace_seconds) if run['status'] in {'done','blocked'} else 0
            request=conn.execute('SELECT q.payload FROM nfos_workflows w JOIN nfos_requests q ON q.id=w.request_id WHERE w.task_id=?',
                                 (run['task_id'],)).fetchone()
            if request and run['status'] in {'done','blocked'}:
                grace=float(json.loads(request['payload']).get('project',{}).get('worker_exit_grace_seconds',grace))
            grace=max(0,grace)
            receipt=dict(_worker_identity_receipt(conn,run),status='waiting',grace_seconds=grace,
                         not_before=float(run['ended_at'])+grace,descendants_json='[]',requested_at=time.time())
        # An old receipt cannot authorize signaling the same process after it
        # has been deliberately attached to a newer active run.
        if (task.current_run_id and task.current_run_id!=run['id'] and task.worker_pid==receipt['worker_pid']
                and task.worker_started_at==receipt['worker_started_at']):
            continue
        if receipt.get('worker_pid') and receipt.get('worker_started_at') is None and kb._pid_alive(receipt['worker_pid']):
            receipt.update(status='identity_unavailable',error='Live worker lacks a persisted creation-time identity')
            _save_cleanup(conn,run,receipt,event='nfos_worker_exit_pending')
            continue
        # Capture while the worker is still alive, including during its grace
        # period, so a later crash retains the identities of known children.
        receipt['descendants_json']=json.dumps(tool._descendants(receipt))
        if time.time()<receipt['not_before']:
            _save_cleanup(conn,run,receipt)
            continue
        first_stop=receipt['status']!='stopping'
        receipt.update(status='stopping',error=None)
        _save_cleanup(conn,run,receipt,event='nfos_worker_exit_requested' if first_stop else None)
        try:
            calls=tool.terminate_calls(conn,run['task_id'],run['id'],reason='The owning worker run is closed')
            # Recheck the current pointer after command cleanup and before the
            # worker signal; never stop a process now registered for new work.
            current=kb.get_task(conn,run['task_id'])
            if (current.current_run_id and current.current_run_id!=run['id']
                    and current.worker_pid==receipt['worker_pid']
                    and current.worker_started_at==receipt['worker_started_at']):
                continue
            survivors=tool._stop_tree(receipt)
            if survivors or any(call['status'] in tool.ACTIVE or call.get('runner_termination_pending') for call in calls):
                receipt.update(status='stopping',error='Known process termination remains unconfirmed')
                _save_cleanup(conn,run,receipt,event='nfos_worker_exit_pending')
                continue
        except (OSError,RuntimeError,tool.psutil.Error) as exc:
            receipt.update(status='stopping',error=type(exc).__name__)
            _save_cleanup(conn,run,receipt,event='nfos_worker_exit_pending')
            continue
        receipt.update(status='confirmed',confirmed_at=time.time(),error=None)
        _save_cleanup(conn,run,receipt,event='nfos_worker_exit_confirmed')
        changed.append(run['id'])
    return changed


def reconcile_runtime(conn, *, worker_exit_grace_seconds=15):
    """Run NFOS recovery inside the existing canonical dispatcher tick."""
    delivery.reconcile_human_answers(conn)
    reconcile_terminal_workers(conn,worker_exit_grace_seconds=worker_exit_grace_seconds)
    delivery.reconcile_human_answers(conn)
    from hermes_cli.nfos_tool import reconcile_calls
    for call in reconcile_calls(conn):
        task=kb.get_task(conn,call['task_id'])
        if (call['status'] not in {'timed_out','interrupted'} or not task or task.status!='running'
                or task.current_run_id!=call['run_id']):
            continue
        delivery.ask_principal(conn,task.id,task.current_run_id,kind='impediment',
            question=f"Diagnose native call {call['id']}: {call['status']}",
            context={'call_id':call['id'],'original_run_id':call['run_id'],
                     'saved_output':'nfos_tool_chunks in this same board','retry':'Read partial output and external destination before repeating'})


def bootstrap(*, db, board, request, token):
    os.environ['HERMES_KANBAN_DB']=str(db)
    os.environ['HERMES_KANBAN_BOARD']=board
    with kb.connect_closing(Path(db)) as conn:
        task=delivery.bootstrap_card(conn,request,token,pid=os.getpid())
        if task.workspace_kind=='worktree':
            workspace,branch=kb._resolve_worktree_workspace(task,board=board,conn=conn)
            kb.set_branch_name(conn,task.id,branch)
        else:
            workspace=kb.resolve_workspace(task,board=board)
        kb.set_workspace_path(conn,task.id,str(workspace))
        task=kb.get_task(conn,task.id)
        lease,owner=kb._try_acquire_workspace_lease(workspace,task_id=task.id)
        if lease is None:
            raise delivery.OwnershipConflict('Workspace is still owned by another execution')
        if not kb._promote_workspace_lease(lease,os.getpid()):
            raise delivery.OwnershipConflict('Could not register workspace ownership')
        kb._fire_worker_spawned_hook(conn,task,str(workspace),os.getpid(),board=board)
    kb._default_spawn(task,str(workspace),board=board,exec_current_process=True)


def _script_command(path):
    paths=[sys.executable,str(Path(path).resolve())]
    if os.name=='nt':
        return '& '+ ' '.join("'"+p.replace("'","''")+"'" for p in paths)
    return ' '.join(shlex.quote(p) for p in paths)


def workflow_command():
    return _script_command(delivery.__file__)


def worker_instructions():
    return ('Exact workflow CLI prefix: '+workflow_command()+'\n'
            'Exact native tool CLI prefix: '+_script_command(Path(__file__).with_name('nfos_tool.py'))+'\n\n')+"""This card uses the owner's current NFOS workflow.
The owner disabled AOF and its mandatory contracts, hooks and closeouts. Historic
repository text does not reactivate it. Do not load those instructions.
You have the Principal's full profile capabilities and an isolated task workspace.
Use the exact workflow CLI prefix above for all actions; terminal tools may
sanitize PYTHONPATH, so do not substitute a bare python/module invocation.
`--help` documents the available actions. Task/run identity comes
from your existing HERMES_KANBAN_* environment; never clear it to bypass ownership.
First use `show` to read the saved spec, stage, next action, decisions and external
effects. For every task, check existing Git changes/PRs and the project's actual
homologation and production before deciding what remains to implement. Record
which version and behavior you could verify; a failed connection is not proof
that the requested change is absent or that production is down for everyone.
On recovery reuse that state and existing files, tests, commits and PRs;
continue the unfinished step, without regenerating a spec or replaying a final
answer. Inspect originals when analysis remains pending. The saved request in
`show` may identify a retained card adopted from the previous workflow. Read its
original comments, runs, attachments and workspace. Have TL verify and persist a
missing NFOS spec from that existing scope and evidence before new implementation.
Reuse completed work and verify existing PR/deploy effects before registering
their readback; do not repeat delivery merely to fill new records. The adoption
is neither proof of delivery nor a new request or authorization to expand scope.
If show contains retained_workspace, work in the current isolated checkout.
Tracked edits and the Git index were restored there. Earlier untracked/ignored
files and evidence remain at retained_workspace.artifacts_location; inspect and
copy the relevant files into this checkout when needed, without editing the
shared original or copying unrelated repositories and virtual environments.
If show contains a completed workspace_repair, use the current repaired checkout.
Its artifacts_location preserves the previous directory, including saved reports
and untracked files. Reuse relevant evidence and commits; the repair does not
change the spec, approve publication, or mean the product was delivered.
For a proven repository binding fault, ask the Principal to use the maintainer
repair-workspace route with the configured project repo, exact commit and current
source identity. Do not alter SQLite/ownership or invent an empty PR.
The saved request in
`show` retains the original Telegram identity and attachments. If a batch or media
contains additional independent tasks, persist them for the Principal with
`ask --kind additional_tasks --input tasks.json`. JSON:
{"question":"Dispatch additional tasks?","primary_task":"First task on this card",
"tasks":[{"key":"audio:00:42","text":"The additional requested task",
"source_ref":"original audio 00:42-00:57"}]}. Use stable locations in the original
message/media as keys, never transient numbering or a newly generated identifier.
Key 0 is reserved for the original task. Do not include this task again. A derived
card's original media also contains its siblings: stay within this card's assigned
text and use the saved lineage when identifying any genuinely additional item.
The Principal reviews the saved proposal and dispatches its items atomically;
do not call receive or implement independent tasks secretly inside this card.
Keep reading/analyzing while that decision is pending. Before implementation
whose scope depends on the split, wait for its resolution and use task_partition
from show for this card's scope. A changes answer retains the full proposal:
correct it and ask again with the same item keys; already dispatched items are
reused. Continue keeps this worker on the first task; other workers create their
own cards. Delimit an initial batch before its first spec. Later discoveries of
independent items keep the accepted primary_task unchanged. If the Principal
explicitly changes that task's scope, request TL/Codex to revise and persist the
spec before more implementation, effects or completion; task_partition records
the revision that must be superseded. Single-task requests need no split decision.
For a new spec or an
explicitly requested spec revision, ask Claude TL for a JSON spec with goal,
criteria [{id,text}], steps and delivery_type; use Codex only if Claude is
unavailable. Save its real transcript, then call `save-spec --input spec.json
--evidence tl-evidence.json --author 'Claude TL'`. No implementation before this.
When principal_validation is enabled, saving the spec atomically queues
kind=spec_review. Read its decision in show and wait for Principal continue.
For a retained spec without that decision, ask kind=spec_review explicitly.
Never treat a saved spec or a worker's own opinion as Principal acceptance.
After changes, correct the spec through the same TL workflow and submit again.
Instruction/spec changes invalidate acceptance. Do not code before its acceptance.
Use `progress --stage implement --next '...'` before Codex work. Persist a
progress state JSON containing useful next steps and recovered files as you work.
Invoke Claude/Codex and long verification commands through the native tool CLI
above: --db DB --task TASK --run RUN --cwd WORKSPACE --timeout SECONDS
[--stdin-file PROMPT_FILE] -- EXECUTABLE ARGUMENTS. Use your actual existing
HERMES_KANBAN_DB/TASK/RUN_ID values, a real executable and an appropriate command
deadline. This persists actual output while the command runs, for the Live Log,
and records timeout separately from exit status. Use Codex --json or Claude's
native stream format when supported. A prior call receipt is available in show;
read its output/partial files and unknown external effects before a new attempt.
Use `ask --kind impediment --input question.json` for any impediment before
declaring human blockage; the Principal resolves the persisted queue. Use
`wait --decision ID --timeout 300` to await an answer inside one native tool call,
keeping this worker alive while the Principal is reviewing. A pending response
means keep waiting, not failure. Save context before waiting. An action=human
answer means save the next step, block the card with that concrete question and
exit along with this task's children. Continue/changes preserves this execution.
For report/audit delivery, save the complete report on the card with `save-report`
BEFORE requesting review. The review binds that saved report revision; do not
change it after approval without requesting another review. Minimal report JSON:
{"summary":"Result","artifacts":[{"id":"proof","path":"result.json"}],
"criteria":[{"id":"C1","status":"PASS","evidence":["proof"]}]}.
Each criteria.evidence entry names the id or path of a declared artifact, not a
free-text claim. Use FAIL or NOT_RUN for criteria that have not been demonstrated.
For code delivery whose canonical HML deployment requires staging integration,
use a separate preparation review BEFORE that integration; do not fabricate HML.
Keep candidate_sha and candidate_tree in progress state. Ask with
`ask --kind preparation --input preparation.json`, whose context.preparation is
{"candidate_sha":"FULL_SHA","candidate_tree":"FULL_TREE",
 "repository":"https://github.com/OWNER/REPO","base_ref":"staging",
 "target":"https://github.com/OWNER/REPO/tree/staging"}.
The Principal's `continue` binds that task/spec/candidate/tree/exact staging
repository and destination; it is NOT production approval. Acquire the project
slot for this candidate. Journal `effect --operation staging_pr|staging_merge`
with --target equal to that exact staging target and --candidate equal to the
reviewed SHA. Read back the staging PR before merge. Both receipts identify
candidate, tree, repository, base_ref, target, actual PR url and readback;
staging_merge also identifies integrated_sha. The integrated tree must match
this prepared candidate. Unknown/repeated effects require readback before retry.
These separate staging effects never satisfy production PR/merge/review gates.
After integration, reconcile the actual HML artifact and test it normally below.
If HML and the production PR candidate differ, use the explicit homologation
binding review below; never erase candidate_sha or invent equivalence.

For code delivery, acquire the project slot with `acquire-project --candidate SHA`
before homologation; if occupied, save state and let the predecessor finish.
Record the HML deployment with `effect --operation homolog --target HML_URL
--candidate SHA`, followed by actual destination readback through `reconcile`.
Homologation precedes Principal publication review; `deploy` means production
and requires the reviewed integrated SHA. HML readback does not itself prove
that the acceptance tests passed.
Save candidate_sha, candidate_tree, homolog_sha and homolog_evidence in progress
state after testing the exact candidate in the project's actual homologation.
If the actual HML SHA differs from the PR candidate, preserve both identities.
Use `ask --kind homologation --input binding.json` for a Principal judgment,
with context.homologation containing candidate_sha, candidate_tree, homolog_sha,
homolog_tree, baseline_sha, scope (same_tree or limited_delta), covered criteria
IDs and evidence (local file paths). First reconcile the actual HML deployment.
Evidence must compare the complete PR delta, explain the candidate baseline and
show which criteria the real validation covers. A partial comparison is not
full-tree equivalence. The Principal may reject it and require exact-candidate
homologation. Only its continue decision binds distinct identities; this does
not authorize publication. Reacquire the slot for the bound candidate afterward.
Create or update its PR through the effect/reconcile protocol. Then use
`ask --kind review --input review.json` with candidate SHA,
PR, homolog evidence and requested action. Wait for the Principal's decision.
Never use kanban_review to spawn a separate reviewer for this enrolled card.
Use `effect --operation homolog|pr|merge|deploy --target ... --candidate SHA` before each
external effect. An execute=false/reconcile=true response requires reading the
destination before trying again. Record that read with `reconcile`.
Before completion save the report with `save-report --input report.json`.
When principal_validation is enabled, request `ask --kind final_review` after
the final report and verified target state are saved, for EVERY delivery type.
Wait for Principal continue. This is separate from code publication approval.
For reports, retain the existing kind=review approval as well. If asked for
changes, correct the result, save a new report and request final_review again.
Keep every criterion linked to its real artifact; never certify your own work.
Use the same report JSON format above, linking each criterion to its artifacts.
After verified delivery, release the project slot with `release-project`.
Record homolog_sha, integrated_sha, artifact and production_readback in progress
state for code delivery. Report-only tasks need no PR/deploy. Evidence must prove
functionality, not merely the presence of a screenshot file.
"""


def coordinator_intake_instructions(context, *, reply_to=None):
    """Preserved dispatch context, not a task classification or a task receipt."""
    command=workflow_command()+' receive --db '+shlex.quote(context['db_path'])+' --input request.json'
    return ("NFOS preserved-message coordination context. The original is persisted, but has not been dispatched to a worker.\n"
        "Decide its meaning as the Principal. If it requests independent work, including an audit or report, "
        "save the exact request JSON below to request.json and run the exact receive command. "
        "Do not execute the task yourself or create its card. The worker creates the card and analyzes any batch.\n"
        "For a status question, correction to existing work, or a human answer, coordinate that existing work instead; "
        "do not call receive merely because this context exists. For resume, reuse this message's source identity. "
        "Do not reconstruct IDs, change project, drop original attachments, or ask for another business approval. "
        "After receive succeeds, report the returned request ID; its normal durable receipt uses the same intake.\n"
        "Reply context: "+str(reply_to or 'not a reply')+"\n"
        "Command: "+command+"\nRequest JSON:\n"+json.dumps(context['request'],ensure_ascii=False))


def principal_instructions():
    return 'Exact workflow CLI prefix: '+workflow_command()+'\n\n'+"""NFOS project coordinator, the owner's active instructions:
You are the Principal, responsible for intake, dispatch, board visibility,
impediments and review. Project implementation belongs to full Hermes workers.
Do not implement project changes in this conversation or create a parallel
delegation path. Read cards, inspect evidence and resolve the workers' decisions.
Manage the board as well as its workers. Before resuming old work, inspect its
Git/PR history and actual homologation/production state. Reconcile deliveries
already made instead of implementing them again. Archive obsolete or superseded
work with a recorded reason and links to its replacement, preserving history;
do not label cancellation as a successful delivery. Process the owner's latest
stop/cancel/scope corrections before dispatching further work they supersede.
New independent messages use the existing durable request intake. Messages that
do not match the automatic fast path carry a persisted coordination context;
when they request work, dispatch its exact payload through receive, rather than
implementing it here. Status, corrections and human answers remain coordination.
Additional
tasks found by a worker arrive as kind=additional_tasks in your decision queue.
Read primary_task, tasks and their source_ref against the original request and
media. Resolve with continue to atomically dispatch all accepted items through
the same intake. You may refine the split by adding "proposal":{"primary_task":
"First task","tasks":[{"key":"stable original location","text":"Task",
"source_ref":"Original message/media reference"}]} to answer.json. Keep stable
keys for the same task. An explicit empty tasks list keeps only the first task.
Delimit the initial batch before the first spec. If you change an already
specified primary_task, explicitly instruct the worker to revise/persist its spec
against that changed scope before implementation. Additional independent items
that preserve primary_task do not require another spec for the current card.
Use changes for unclear boundaries; the original proposal remains saved. Invalid
or conflicting keys return changes without partial dispatch. Do not reconstruct
manual receive calls for this proposal or create its cards yourself. The runtime
reserves capacity and each new worker creates its own card. No new human approval
is required for additional tasks already present in the authorized request.
Use the same CLI prefix with `pending` to inspect the persisted decision
queue. Resolve each item with `decide --decision ID --resolution
continue|approve|changes|human --input answer.json` (JSON containing answer).
Inspect the current spec, candidate and evidence before approving publication.
With principal_validation enabled, you own two mandatory acceptances in this
same queue. For spec_review, compare the ORIGINAL request and attachments with
the TL spec, test each criterion's clarity, scope coverage, verification method,
dependencies and exclusions. Use changes for gaps and explain how to fix them.
For final_review, independently open the artifacts and real screenshots, inspect
test outputs and the actual target/readback, and assess EVERY criterion against
the accepted spec. A file hash proves identity, not correctness. Worker PASS or
a screenshot's existence is never enough. Reject inadequate, stale, wrong-target
or partial evidence, even if the worker claims success. Record FAIL/NOT_RUN as
unproven; require rework rather than relaxing the criterion to obtain completion.
Accept either kind with continue and answer.json containing answer plus:
{"assessment":{"request_alignment":"How the original request is covered",
"scope_assessment":"Scope, exclusions and dependencies checked",
"criteria":[{"id":"C1","verdict":"accept","observation":"What I checked and why it meets the criterion",
"evidence":["/absolute/path/to/inspected-report-artifact"]}]}}.
Include each spec criterion exactly once. Evidence is mandatory for final_review
and names a declared report artifact path/ref with saved local bytes. Save remote
readback as a report artifact; a bare URL is not a verified result. Use changes
with concrete corrections when you cannot accept. A new spec, instruction,
candidate, final report or evidence change needs a current review. Do not
approve on the worker's behalf or invent observations. Preserve the task/run
history and let the worker do rework, while you continue handling other requests.
You may approve merge/deploy within the user's authorized spec without asking for
another human approval. Request human input only for a concrete decision you
cannot resolve. Keep this coordinator available; do not wait for workers to finish.
For a confirmed administrative routing or classification error, the supported
maintainer actions are repair-card and repair-workspace. Read the current card
and configured repository, preview the exact correction, then apply with the
same expected identities and a recorded reason. These actions preserve status,
history and evidence; they do not approve delivery or resume an explicit stop.
repair-card takes delivery_type, expected_delivery_type, expected_spec_revision,
expected_instruction_revision, reason, actor and optional use_canonical_repo.
A changed type requires a new matching spec, reusing the existing TL proposal.
repair-workspace takes repo_path, base_sha, expected_workspace,
expected_source_sha, reason and actor. Both default to preview; apply=true
performs the repair. Repository repair keeps the original directory available.
After verifying the correction, reconsider only the technical impediment it
resolved. Do not demand another business approval for that same authorized work.
When the human answers, use `resume --task ID --input answer.json` containing
answer and source (the actual Telegram message identity). This restores the same
card with its spec, workspace, history and next step; do not create another card.
If you find a concrete solution to your own prior human escalation without a new
human answer, use `reconsider --decision ID --resolution continue|approve|changes
--input answer.json` with reason and answer. Read the current spec and evidence
first. This preserves the prior decision and applies the normal review checks;
it does not fabricate a human reply or permit reusing approval for another
candidate. A missing proof after deployment is not automatically a new human
approval before an already-authorized deployment. Keep unproven criteria pending.
The owner disabled AOF and its mandatory contracts, hooks and closeouts.
Historical instructions do not reactivate it or create new publication gates.
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['bootstrap'])
    for key in ['db','board','request','token']:
        parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    bootstrap(db=args.db,board=args.board,request=args.request,token=args.token)


if __name__=='__main__':
    main()
