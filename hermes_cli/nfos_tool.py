"""Run one native tool with durable live output in its existing Kanban board.

This module owns command execution and its evidence, never card scheduling. A
handshake prevents the command from starting before its intent and process
identity commit. Recovery never repeats a command whose effect is unknown.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import psutil


SCHEMA = """
CREATE TABLE IF NOT EXISTS nfos_tool_calls (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id INTEGER NOT NULL REFERENCES task_runs(id), argv_json TEXT NOT NULL,
    cwd TEXT NOT NULL, stdin_path TEXT, stdin_sha256 TEXT,
    status TEXT NOT NULL, created_at REAL NOT NULL,
    started_at REAL, finished_at REAL, deadline_at REAL,
    timeout_seconds REAL NOT NULL, runner_pid INTEGER, runner_started_at REAL,
    runner_kind TEXT NOT NULL DEFAULT 'embedded',
    worker_pid INTEGER, worker_started_at REAL,
    descendants_json TEXT NOT NULL DEFAULT '[]', returncode INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS nfos_tool_calls_run ON nfos_tool_calls(task_id,run_id,created_at);
CREATE TABLE IF NOT EXISTS nfos_tool_chunks (
    call_id TEXT NOT NULL REFERENCES nfos_tool_calls(id), seq INTEGER NOT NULL,
    stream TEXT NOT NULL, content BLOB NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(call_id,seq)
);
"""
ACTIVE = ('intent', 'running', 'stopping')


class ToolExecutionError(ValueError):
    pass


@contextmanager
def connect(db_path):
    """Open the existing authority, without creating another board or DB."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=FULL')
    try:
        yield conn
    finally:
        conn.close()


def init_schema(conn):
    if conn.in_transaction:
        raise ToolExecutionError('Schema initialization needs its own transaction')
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(nfos_tool_calls)')}
    for name in ('stdin_path', 'stdin_sha256'):
        if name not in columns:
            conn.execute('ALTER TABLE nfos_tool_calls ADD COLUMN ' + name + ' TEXT')
    if 'runner_kind' not in columns:
        conn.execute("ALTER TABLE nfos_tool_calls ADD COLUMN runner_kind TEXT NOT NULL DEFAULT 'embedded'")
    conn.commit()


@contextmanager
def _transaction(conn):
    if conn.in_transaction:
        raise ToolExecutionError('Tool execution needs a separate transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _event(conn, row, kind, **payload):
    conn.execute('INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?)',
        (row['task_id'], row['run_id'], kind, _json(dict(call_id=row['id'], **payload)), int(time.time())))


def _owned(conn, task_id, run_id):
    row = conn.execute('''SELECT t.status,t.current_run_id,r.task_id AS run_task,r.status AS run_status
        FROM tasks t LEFT JOIN task_runs r ON r.id=? WHERE t.id=?''', (run_id, task_id)).fetchone()
    return bool(row and row['status'] == 'running' and row['current_run_id'] == run_id
                and row['run_task'] == task_id and row['run_status'] == 'running')


def _in_exit_grace(conn, task_id, run_id):
    """Existing commands may finish a short flush after an NFOS run closes."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_workflows'").fetchone():
        return False
    row=conn.execute('''SELECT r.status,r.ended_at,r.metadata,t.current_run_id,q.payload
        FROM task_runs r JOIN tasks t ON t.id=r.task_id JOIN nfos_workflows w ON w.task_id=t.id
        LEFT JOIN nfos_requests q ON q.id=w.request_id WHERE r.task_id=? AND r.id=?''',
        (task_id,run_id)).fetchone()
    if not row or not row['ended_at'] or row['current_run_id'] not in (None,run_id):
        return False
    if row['status']!='done' and not (row['status']=='blocked' and conn.execute(
            "SELECT 1 FROM nfos_decisions WHERE task_id=? AND run_id=? AND status='human'",
            (task_id,run_id)).fetchone()):
        return False
    receipt=json.loads(row['metadata'] or '{}').get('nfos_cleanup') or {}
    if receipt.get('status') in {'stopping','confirmed'}:
        return False
    project=json.loads(row['payload'] or '{}').get('project',{})
    not_before=receipt.get('not_before',float(row['ended_at'])+float(project.get('worker_exit_grace_seconds',15)))
    return time.time()<not_before


def _identity(pid):
    try:
        if int(pid) <= 0:
            return None
        proc = psutil.Process(int(pid))
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return {'pid': proc.pid, 'started_at': proc.create_time()}
    except (psutil.Error, TypeError, ValueError, OverflowError):
        return None


def _matches(pid, started_at):
    actual = _identity(pid)
    return bool(actual and started_at is not None and abs(actual['started_at'] - started_at) < .01)


def _group_children(pid, started_at):
    """An owned POSIX session also contains children reparented after tool exit."""
    if os.name == 'nt' or not _matches(pid, started_at):
        return []
    try:
        if os.getpgid(pid) != pid:
            return []
    except ProcessLookupError:
        return []
    children = []
    for proc in psutil.process_iter(['pid', 'create_time']):
        try:
            if proc.pid != pid and proc.info['create_time'] >= started_at and os.getpgid(proc.pid) == pid:
                identity = _identity(proc.pid)
                if identity:
                    children.append(identity)
        except (psutil.Error, ProcessLookupError, PermissionError, TypeError):
            continue
    return children


def _descendants(row, *, include_group=False):
    identities = {int(p['pid']): p for p in json.loads(row['descendants_json'] or '[]')}
    if _matches(row['worker_pid'], row['worker_started_at']):
        try:
            for proc in psutil.Process(row['worker_pid']).children(recursive=True):
                identity = _identity(proc.pid)
                if identity:
                    identities[proc.pid] = identity
        except psutil.Error:
            pass
        if include_group:
            for identity in _group_children(row['worker_pid'], row['worker_started_at']):
                identities[identity['pid']] = identity
    return [p for p in identities.values() if _matches(p['pid'], p['started_at'])]


def _signal_identity(identity, *, kill=False):
    """PID reuse must never turn an old call receipt into authority to kill."""
    if not _matches(identity['pid'], identity['started_at']):
        return
    try:
        proc = psutil.Process(identity['pid'])
        proc.kill() if kill else proc.terminate()
    except psutil.NoSuchProcess:
        pass


def _stop_tree(row, grace=.5):
    root = {'pid': row['worker_pid'], 'started_at': row['worker_started_at']}
    children = _descendants(row, include_group=True)
    root_matches = _matches(root['pid'], root['started_at'])
    # Children first lets the handshake wrapper collect the real command exit
    # status, including a command that handles TERM and exits successfully.
    for identity in reversed(children):
        _signal_identity(identity)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_matches(p['pid'], p['started_at']) for p in children):
        time.sleep(.025)
    # Include descendants born while the first termination was in progress.
    children = {p['pid']: p for p in children + _descendants(row, include_group=True)}
    for identity in reversed(list(children.values())):
        _signal_identity(identity, kill=True)
    if root_matches and _matches(root['pid'], root['started_at']):
        # Give the wrapper a short chance to reap the command and propagate rc.
        deadline = time.monotonic() + .25
        while time.monotonic() < deadline and _matches(root['pid'], root['started_at']):
            time.sleep(.025)
        _signal_identity(root, kill=True)
    expected = list(children.values()) + ([root] if root_matches else [])
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and any(_matches(p['pid'], p['started_at']) for p in expected):
        time.sleep(.025)
    survivors = [p for p in expected if _matches(p['pid'], p['started_at'])]
    return survivors


def _record(row):
    result = dict(row)
    result['timed_out'] = bool(result['timed_out'])
    return result


def get_call(conn, call_id):
    row = conn.execute('SELECT * FROM nfos_tool_calls WHERE id=?', (call_id,)).fetchone()
    return _record(row) if row else None


def read_calls(conn, task_id, run_id=None):
    clauses, args = ['task_id=?'], [task_id]
    if run_id is not None:
        clauses.append('run_id=?')
        args.append(run_id)
    return [_record(row) for row in conn.execute(
        'SELECT * FROM nfos_tool_calls WHERE ' + ' AND '.join(clauses) + ' ORDER BY created_at,id', args)]


def read_chunks(conn, call_id, *, after_seq=0, limit=1000):
    """Return original bytes; consumers concatenate before UTF-8 decoding."""
    return [dict(row) for row in conn.execute('''SELECT * FROM nfos_tool_chunks
        WHERE call_id=? AND seq>? ORDER BY seq LIMIT ?''',
        (call_id, max(0, int(after_seq)), min(1000, max(1, int(limit)))))]


def call_runner_alive(call):
    """A CLI adapter is one disposable process, unlike an embedded caller."""
    return call.get('runner_kind') == 'cli' and _matches(call['runner_pid'], call['runner_started_at'])


def _stop_cli_runner(call):
    # Never terminate the process embedding this Python API (it can own more
    # than one card). Only main() records a disposable, one-call CLI runner.
    if not call_runner_alive(call) or call['runner_pid'] == os.getpid():
        return []
    return _stop_tree({'worker_pid':call['runner_pid'],'worker_started_at':call['runner_started_at'],
                       'descendants_json':'[]'})


def _finish(conn, call_id, status, *, returncode=None, timed_out=False, error=None, only_changed=False):
    with _transaction(conn):
        row = get_call(conn, call_id)
        if row['status'] not in ACTIVE:
            return None if only_changed else row
        if row['status'] == 'stopping':
            timed_out = row['timed_out']
            status = 'timed_out' if timed_out else 'interrupted'
            error = row['error']
        # The wrapper can be killed while cleaning descendants, or encode a
        # negative POSIX return code as 0..255. Prefer the native exit receipt.
        if row['returncode'] is not None:
            returncode = row['returncode']
        conn.execute('''UPDATE nfos_tool_calls SET status=?,finished_at=?,returncode=?,timed_out=?,error=?
            WHERE id=?''', (status, time.time(), returncode, int(timed_out), error, call_id))
        _event(conn, row, 'nfos_tool_finished', status=status, returncode=returncode,
               timed_out=bool(timed_out), error=error)
        return get_call(conn, call_id)


def _request_stop(conn, row, *, timed_out, error):
    # Commit the reason before signaling. The adapter may collect the child's
    # exit concurrently; it must not turn an intentional stop into tool failure.
    with _transaction(conn):
        return bool(conn.execute("""UPDATE nfos_tool_calls SET status='stopping',timed_out=?,error=?
            WHERE id=? AND status IN ('intent','running')""",
            (int(timed_out), error, row['id'])).rowcount)


def reconcile_calls(conn):
    """Primitive for the existing runtime reconciler, never an extra scheduler.

    A dead adapter, expired deadline or lost run stops only the recorded call
    tree. Output survives. No command is replayed and no task is unblocked here.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_tool_calls'").fetchone():
        return []
    rows = [dict(r) for r in conn.execute("SELECT * FROM nfos_tool_calls WHERE status IN ('intent','running','stopping')")]
    reconciled = []
    for row in rows:
        expired = bool(row['deadline_at'] and row['deadline_at'] <= time.time())
        runner_alive = _matches(row['runner_pid'], row['runner_started_at'])
        owned = _owned(conn, row['task_id'], row['run_id'])
        if runner_alive and not expired and row['status'] != 'stopping' and (owned or _in_exit_grace(conn,row['task_id'],row['run_id'])):
            continue
        error = ('Tool deadline exceeded' if expired else
                 'Execution ownership changed' if not owned else 'Execution adapter stopped; effect may be unknown')
        verified = _matches(row['worker_pid'], row['worker_started_at'])
        if row['worker_pid'] and not verified:
            error += '; recorded process identity is absent or no longer matches'
        requested = _request_stop(conn, row, timed_out=expired, error=error)
        if not requested and row['status'] != 'stopping':
            continue
        survivors = _stop_tree(row)
        if survivors:
            # Keep the receipt active so the runtime cannot mistake a live
            # executor for one whose termination has been confirmed.
            with _transaction(conn):
                conn.execute("UPDATE nfos_tool_calls SET status='stopping',error=? WHERE id=?",
                    ('Process termination is not yet confirmed', row['id']))
            reconciled.append(get_call(conn, row['id']))
            continue
        changed = _finish(conn, row['id'], 'timed_out' if expired else 'interrupted',
                          timed_out=expired, error=error, only_changed=not requested)
        if changed:
            changed['runner_termination_pending']=bool(_stop_cli_runner(changed))
            reconciled.append(changed)
    return reconciled


def terminate_calls(conn, task_id, run_id=None, *, reason='Execution was stopped'):
    """Stop only this card/run's recorded native calls, preserving all output.

    The existing runtime may call this before ending a worker. A returned
    ``stopping`` receipt means termination has not yet been confirmed.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_tool_calls'").fetchone():
        return []
    changed = []
    for row in read_calls(conn, task_id, run_id):
        if row['status'] not in ACTIVE:
            if call_runner_alive(row):
                row['runner_termination_pending']=bool(_stop_cli_runner(row))
                changed.append(row)
            continue
        requested = _request_stop(conn, row, timed_out=False, error=reason)
        if not requested and row['status'] != 'stopping':
            continue
        survivors = _stop_tree(row)
        if survivors:
            with _transaction(conn):
                conn.execute("UPDATE nfos_tool_calls SET status='stopping',error=? WHERE id=?",
                             ('Process termination is not yet confirmed', row['id']))
            changed.append(get_call(conn, row['id']))
        else:
            result = _finish(conn, row['id'], 'interrupted', error=reason, only_changed=not requested)
            if result:
                result['runner_termination_pending']=bool(_stop_cli_runner(result))
                changed.append(result)
    return changed


def _hidden_process_options():
    if os.name != 'nt':
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {'creationflags': subprocess.CREATE_NO_WINDOW, 'startupinfo': info}


def _publish_command_exit(payload, command_pid, returncode):
    """Persist the real result before waiting for any remaining descendants."""
    expected = payload['process_identity']
    ancestry = {os.getpid(), *(proc.pid for proc in psutil.Process().parents())}
    if expected['pid'] not in ancestry or not _matches(expected['pid'], expected['started_at']):
        raise ToolExecutionError('Native exit no longer has its recorded process identity')
    with connect(payload['db_path']) as conn:
        with _transaction(conn):
            row = get_call(conn, payload['call_id'])
            if not row or (row['worker_pid'], row['worker_started_at']) != (expected['pid'], expected['started_at']):
                raise ToolExecutionError('Native exit belongs to a different call process')
            if conn.execute('UPDATE nfos_tool_calls SET returncode=? WHERE id=? AND returncode IS NULL',
                            (returncode,row['id'])).rowcount:
                _event(conn,row,'nfos_tool_command_exited',pid=command_pid,returncode=returncode)


def _child():
    # EOF means the adapter died before authorizing execution. This child has
    # no external effect until its PID/create-time are durably recorded.
    line = sys.stdin.buffer.readline()
    if not line:
        return 125
    payload = json.loads(line)
    input_bytes = base64.b64decode(payload['stdin_base64']) if payload.get('stdin_base64') is not None else None
    proc = subprocess.Popen(payload['argv'], cwd=payload['cwd'],
                            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                            **_hidden_process_options())
    proc.communicate(input=input_bytes)
    _publish_command_exit(payload, proc.pid, proc.returncode)
    # Keep the process-group identity valid until even fast, reparented tool
    # descendants have exited. The adapter's deadline applies to all of them.
    identity = _identity(os.getpid())
    while identity and _group_children(identity['pid'], identity['started_at']):
        time.sleep(.25)
    return proc.returncode


def run_command(db_path, *, task_id, run_id, argv, cwd, timeout_seconds, call_id=None, env=None, stdin_path=None,
                _cli_owner=False):
    """Execute an exact vector without a shell. Secrets belong in env, not argv.

    Reusing a call ID only reads the existing receipt. A retry is a new call
    after the workflow has reconciled any uncertain external effect.
    """
    argv = [os.fspath(arg) for arg in argv]
    cwd = str(Path(cwd).resolve(strict=True))
    timeout_seconds = float(timeout_seconds)
    input_path = str(Path(stdin_path).resolve(strict=True)) if stdin_path is not None else None
    input_bytes = Path(input_path).read_bytes() if input_path else None
    input_hash = hashlib.sha256(input_bytes).hexdigest() if input_bytes is not None else None
    if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
        raise ToolExecutionError('An existing absolute executable is required')
    if any('\x00' in arg for arg in argv) or not Path(cwd).is_dir():
        raise ToolExecutionError('Invalid command vector or workspace')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ToolExecutionError('A finite positive tool deadline is required')
    call_id = str(call_id or 'call_' + uuid.uuid4().hex)
    process = None
    threads = []
    cancelled = threading.Event()
    previous_handlers = {}
    with connect(db_path) as conn:
        init_schema(conn)
        with _transaction(conn):
            previous = get_call(conn, call_id)
            if previous:
                if (previous['task_id'], previous['run_id'], previous['argv_json'], previous['cwd'], previous['stdin_sha256']) != (
                        task_id, run_id, _json(argv), cwd, input_hash):
                    raise ToolExecutionError('This call identity belongs to a different command or execution')
                return previous
            if not _owned(conn, task_id, run_id):
                raise ToolExecutionError('The task no longer belongs to this execution')
            identity = _identity(os.getpid())
            conn.execute('''INSERT INTO nfos_tool_calls
                (id,task_id,run_id,argv_json,cwd,stdin_path,stdin_sha256,status,created_at,timeout_seconds,runner_pid,runner_started_at,runner_kind)
                VALUES(?,?,?,?,?,?,?,'intent',?,?,?,?,?)''',
                (call_id, task_id, run_id, _json(argv), cwd, input_path, input_hash, time.time(), timeout_seconds,
                 identity['pid'], identity['started_at'], 'cli' if _cli_owner else 'embedded'))
            _event(conn, get_call(conn, call_id), 'nfos_tool_intent')
        streams = queue.Queue(maxsize=128)
        def pump(pipe, name):
            try:
                while True:
                    data = os.read(pipe.fileno(), 16384)
                    if not data:
                        break
                    streams.put((name, data))
            finally:
                streams.put((name, None))
        try:
            options = _hidden_process_options()
            if os.name != 'nt':
                options['start_new_session'] = True
            process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '_child'],
                cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
            identity = _identity(process.pid)
            if identity is None:
                raise ToolExecutionError('The tool handshake process exited before identity registration')
            with _transaction(conn):
                if not _owned(conn, task_id, run_id):
                    raise ToolExecutionError('The task no longer belongs to this execution')
                now = time.time()
                conn.execute("""UPDATE nfos_tool_calls SET status='running',started_at=?,deadline_at=?,
                    worker_pid=?,worker_started_at=? WHERE id=?""",
                    (now, now + timeout_seconds, identity['pid'], identity['started_at'], call_id))
                _event(conn, get_call(conn, call_id), 'nfos_tool_started', pid=identity['pid'])
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[sig] = signal.signal(sig, lambda *_: cancelled.set())
            for name, pipe in (('stdout', process.stdout), ('stderr', process.stderr)):
                thread = threading.Thread(target=pump, args=(pipe, name), daemon=True)
                thread.start()
                threads.append(thread)
            process.stdin.write((_json({'argv': argv, 'cwd': cwd, 'db_path':str(Path(db_path).resolve()),
                'call_id':call_id,'process_identity':identity,
                'stdin_base64': base64.b64encode(input_bytes).decode() if input_bytes is not None else None}) + '\n').encode())
            process.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            eof, sequence = set(), 0
            last_identity_refresh = 0
            stopped_reason = None
            timed_out = False
            while len(eof) < 2 or process.poll() is None:
                now = time.monotonic()
                if now - last_identity_refresh >= 1:
                    row = get_call(conn, call_id)
                    descendants = _descendants(row)
                    with _transaction(conn):
                        conn.execute('UPDATE nfos_tool_calls SET descendants_json=? WHERE id=?',
                                     (_json(descendants), call_id))
                    if row['status'] not in ACTIVE:
                        stopped_reason = row['error'] or 'Runtime reconciled this call'
                    if not _owned(conn, task_id, run_id) and not _in_exit_grace(conn,task_id,run_id):
                        stopped_reason = 'Execution ownership changed'
                    last_identity_refresh = now
                if (now >= deadline or cancelled.is_set() or stopped_reason) and not stopped_reason == 'termination_complete':
                    timed_out = now >= deadline
                    reason = stopped_reason or ('Tool deadline exceeded' if timed_out else 'Execution adapter interrupted')
                    survivors = _stop_tree(get_call(conn, call_id))
                    if survivors:
                        raise ToolExecutionError('Tool process termination could not be confirmed')
                    stopped_reason = 'termination_complete'
                    final_error = reason
                try:
                    stream, data = streams.get(timeout=.05)
                except queue.Empty:
                    continue
                if data is None:
                    eof.add(stream)
                    continue
                sequence += 1
                with _transaction(conn):
                    conn.execute('INSERT INTO nfos_tool_chunks(call_id,seq,stream,content,created_at) VALUES(?,?,?,?,?)',
                                 (call_id, sequence, stream, data, time.time()))
                    _event(conn, get_call(conn, call_id), 'nfos_tool_chunk', seq=sequence, stream=stream, bytes=len(data))
            returncode = process.wait()
            if stopped_reason:
                return _finish(conn, call_id, 'timed_out' if timed_out else 'interrupted', returncode=returncode,
                               timed_out=timed_out, error=final_error)
            # Detached descendants are still this call's children. A tool must
            # not leave them executing after its receipt says it is finished.
            descendants = _descendants(get_call(conn, call_id))
            if descendants:
                _stop_tree(get_call(conn, call_id))
            return _finish(conn, call_id, 'succeeded' if returncode == 0 else 'failed', returncode=returncode)
        except BaseException as exc:
            row = get_call(conn, call_id)
            if process and process.stdin and not process.stdin.closed:
                process.stdin.close()
            survivors = _stop_tree(row)
            if process:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if survivors:
                with _transaction(conn):
                    conn.execute("UPDATE nfos_tool_calls SET status='stopping',error=? WHERE id=?",
                                 ('Process termination is not yet confirmed', call_id))
                raise
            # Exception messages may contain command arguments from a launcher;
            # record the type only, never environment values or command dumps.
            _finish(conn, call_id, 'spawn_failed' if row['status'] == 'intent' else 'interrupted',
                    returncode=process.poll() if process else None, error=type(exc).__name__)
            raise
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            for thread in threads:
                thread.join(timeout=1)
            if process:
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()


def main():
    if sys.argv[1:2] == ['_child']:
        return _child()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--run', required=True, type=int)
    parser.add_argument('--timeout', required=True, type=float)
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--call-id')
    parser.add_argument('--stdin-file')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    result = run_command(args.db, task_id=args.task, run_id=args.run, argv=argv, cwd=args.cwd,
                         timeout_seconds=args.timeout, call_id=args.call_id, stdin_path=args.stdin_file,
                         _cli_owner=True)
    print(json.dumps(result, ensure_ascii=False))
    if result['timed_out']:
        return 124
    return 0 if result['status'] == 'succeeded' else result['returncode'] or 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ToolExecutionError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        raise SystemExit(1)
