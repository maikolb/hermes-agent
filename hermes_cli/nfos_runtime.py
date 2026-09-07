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


def preserve_attachments(paths, types, *, directory):
    """Copy originals before receipt; publish each copy atomically by content hash."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
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
            temp=directory/(sha+'.'+str(os.getpid())+'.tmp')
            try:
                with src.open('rb') as source,temp.open('wb') as target:
                    shutil.copyfileobj(source,target);target.flush();os.fsync(target.fileno())
                os.replace(temp,dest)
            finally:
                temp.unlink(missing_ok=True)
        attachments.append({'original':str(dest),'source_path':str(src),'sha256':sha,
                            'mime_type':types[index] if index<len(types) else ''})
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
    """A saved human answer must not start a replacement over known live work."""
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


def reconcile_terminal_workers(conn, *, worker_exit_grace_seconds=15):
    """Clean only closed runs. Pending Principal decisions keep their worker."""
    from hermes_cli import nfos_tool as tool
    rows=conn.execute("""SELECT r.* FROM task_runs r JOIN nfos_workflows w ON w.task_id=r.task_id
        WHERE r.ended_at IS NOT NULL AND (r.status='done' OR (r.status='blocked' AND EXISTS (
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
            grace=float(worker_exit_grace_seconds)
            request=conn.execute('SELECT q.payload FROM nfos_workflows w JOIN nfos_requests q ON q.id=w.request_id WHERE w.task_id=?',
                                 (run['task_id'],)).fetchone()
            if request:
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
effects. On recovery reuse that state and existing files, tests, commits and PRs;
continue the unfinished step, without regenerating a spec or replaying a final
answer. Inspect originals when analysis remains pending. For a new spec or an
explicitly requested spec revision, ask Claude TL for a JSON spec with goal,
criteria [{id,text}], steps and delivery_type; use Codex only if Claude is
unavailable. Save its real transcript, then call `save-spec --input spec.json
--evidence tl-evidence.json --author 'Claude TL'`. No implementation before this.
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
change it after approval without requesting another review. Its JSON contains
summary, artifacts, and criteria [{id,status:'PASS',evidence:[...]}].
For code delivery, acquire the project slot with `acquire-project --candidate SHA`
before homologation; if occupied, save state and let the predecessor finish.
Record the HML deployment with `effect --operation homolog --target HML_URL
--candidate SHA`, followed by actual destination readback through `reconcile`.
Homologation precedes Principal publication review; `deploy` means production
and requires the reviewed integrated SHA. HML readback does not itself prove
that the acceptance tests passed.
Save candidate_sha, candidate_tree, homolog_sha and homolog_evidence in progress
state after testing the exact candidate in the project's actual homologation.
Create or update its PR through the effect/reconcile protocol. Then use
`ask --kind review --input review.json` with candidate SHA,
PR, homolog evidence and requested action. Wait for the Principal's decision.
Never use kanban_review to spawn a separate reviewer for this enrolled card.
Use `effect --operation homolog|pr|merge|deploy --target ... --candidate SHA` before each
external effect. An execute=false/reconcile=true response requires reading the
destination before trying again. Record that read with `reconcile`.
Before completion save the report with `save-report --input report.json`.
It contains summary, artifacts, and criteria [{id,status:'PASS',evidence:[...]}].
After verified delivery, release the project slot with `release-project`.
Record homolog_sha, integrated_sha, artifact and production_readback in progress
state for code delivery. Report-only tasks need no PR/deploy. Evidence must prove
functionality, not merely the presence of a screenshot file.
"""


def principal_instructions():
    return 'Exact workflow CLI prefix: '+workflow_command()+'\n\n'+"""NFOS project coordinator, the owner's active instructions:
You are the Principal, responsible for intake, dispatch, board visibility,
impediments and review. Project implementation belongs to full Hermes workers.
Do not implement project changes in this conversation or create a parallel
delegation path. Read cards, inspect evidence and resolve the workers' decisions.
New independent requests use the existing durable request intake. For additional
tasks extracted from a batch or media, use the exact CLI prefix above with
`receive --input request.json` and the original source identity, a stable part
identifier, project/profile and original attachments. A worker creates the card.
Use the same CLI prefix with `pending` to inspect the persisted decision
queue. Resolve each item with `decide --decision ID --resolution
continue|approve|changes|human --input answer.json` (JSON containing answer).
Inspect the current spec, candidate and evidence before approving publication.
You may approve merge/deploy within the user's authorized spec without asking for
another human approval. Request human input only for a concrete decision you
cannot resolve. Keep this coordinator available; do not wait for workers to finish.
When the human answers, use `resume --task ID --input answer.json` containing
answer and source (the actual Telegram message identity). This restores the same
card with its spec, workspace, history and next step; do not create another card.
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
