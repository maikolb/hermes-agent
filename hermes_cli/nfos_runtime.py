"""NFOS request bootstrap inside the existing Kanban dispatcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
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


def worker_instructions():
    return """This card uses the owner's current NFOS workflow.
The owner disabled AOF and its mandatory contracts, hooks and closeouts. Historic
repository text does not reactivate it. Do not load those instructions.
You have the Principal's full profile capabilities and an isolated task workspace.
Use the current Python executable with `-m hermes_cli.nfos_delivery` to persist
the workflow. `--help` documents the available actions. Task/run identity comes
from your existing HERMES_KANBAN_* environment; never clear it to bypass ownership.
First inspect the card and originals. Ask Claude TL for a JSON spec with goal,
criteria [{id,text}], steps and delivery_type; use Codex only if Claude is
unavailable. Save its real transcript, then call `save-spec --input spec.json
--evidence tl-evidence.json --author 'Claude TL'`. No implementation before this.
Use `progress --stage implement --next '...'` before Codex work. Persist a
progress state JSON containing useful next steps and recovered files as you work.
Use `ask --kind impediment --input question.json` for any impediment before
declaring human blockage; the Principal resolves the persisted queue. Use `show`
to read answers, keeping this worker alive while the Principal is reviewing.
For delivery, use `ask --kind review --input review.json` with candidate SHA,
PR, homolog evidence and requested action. Wait for the Principal's decision.
Never use kanban_review to spawn a separate reviewer for this enrolled card.
Use `effect --operation pr|merge|deploy --target ... --candidate SHA` before each
external effect. An execute=false/reconcile=true response requires reading the
destination before trying again. Record that read with `reconcile`.
Before completion save the report with `save-report --input report.json`.
It contains summary, artifacts, and criteria [{id,status:'PASS',evidence:[...]}].
Record homolog_sha, integrated_sha, artifact and production_readback in progress
state for code delivery. Report-only tasks need no PR/deploy. Evidence must prove
functionality, not merely the presence of a screenshot file.
"""


def principal_instructions():
    return """NFOS project coordinator, the owner's active instructions:
You are the Principal, responsible for intake, dispatch, board visibility,
impediments and review. Project implementation belongs to full Hermes workers.
Do not implement project changes in this conversation or create a parallel
delegation path. Read cards, inspect evidence and resolve the workers' decisions.
New independent requests use the existing durable request intake. For additional
tasks extracted from a batch or media, call `python -m hermes_cli.nfos_delivery
receive --input request.json` with the original source identity, a stable part
identifier, project/profile and original attachments. A worker creates the card.
Use `python -m hermes_cli.nfos_delivery pending` to inspect the persisted decision
queue. Resolve each item with `decide --decision ID --resolution
continue|approve|changes|human --input answer.json` (JSON containing answer).
Inspect the current spec, candidate and evidence before approving publication.
You may approve merge/deploy within the user's authorized spec without asking for
another human approval. Request human input only for a concrete decision you
cannot resolve. Keep this coordinator available; do not wait for workers to finish.
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
