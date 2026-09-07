"""S09 contract: real loopback HTTP/process failures, explicitly synthetic effects.

The destination deliberately accepts duplicate POSTs. Its ledger therefore exposes
an incorrect retry instead of masking it with server-side idempotency. No GitHub,
real approval, deploy, external credential, firewall, or production operation occurs.
Run normally through the canonical repository test runner. The same file provides
the child HTTP server/client entry points so every subprocess uses the checked code.
"""
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import psutil

REPO = Path(__file__).resolve().parents[2]
CANDIDATE = hashlib.sha1(b'synthetic reviewed candidate').hexdigest()
INTEGRATED = hashlib.sha1(b'synthetic integrated commit').hexdigest()
TREE = hashlib.sha1(b'same synthetic artifact tree').hexdigest()
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _destination(root, port, drop_operation):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    root = Path(root)
    ledger = root/'destination.db'
    with sqlite3.connect(ledger) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS effects(seq INTEGER PRIMARY KEY, effect_id TEXT, operation TEXT, candidate TEXT, receipt TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS requests(seq INTEGER PRIMARY KEY, method TEXT, effect_id TEXT, found INTEGER)')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            item = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/effects'
            receipt = {'effect_id': item['effect_id'], 'operation': item['operation'],
                'candidate': item['candidate'], 'tree': TREE,
                'url': f'http://127.0.0.1:{port}/effects/{item["effect_id"]}',
                'integrated_sha': INTEGRATED, 'artifact': 'synthetic-artifact:'+TREE,
                'behavior_evidence': ['Synthetic endpoint state is durably readable over HTTP']}
            with sqlite3.connect(ledger) as conn:
                conn.execute('PRAGMA synchronous=FULL')
                conn.execute('INSERT INTO effects(effect_id,operation,candidate,receipt) VALUES(?,?,?,?)',
                    (item['effect_id'], item['operation'], item['candidate'], json.dumps(receipt)))
                conn.execute('INSERT INTO requests(method,effect_id,found) VALUES(?,?,?)',
                    ('POST', item['effect_id'], 1))
            # Commit is complete before a real socket close, with no HTTP response.
            if item['operation'] == drop_operation:
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            payload = json.dumps(receipt).encode()
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            key = self.path.removeprefix('/effects/')
            with sqlite3.connect(ledger) as conn:
                rows = conn.execute('SELECT receipt FROM effects WHERE effect_id=? ORDER BY seq', (key,)).fetchall()
                conn.execute('INSERT INTO requests(method,effect_id,found) VALUES(?,?,?)', ('GET', key, bool(rows)))
            payload = json.dumps({'found': bool(rows), 'count': len(rows),
                'receipt': json.loads(rows[0][0]) if rows else None}).encode()
            self.send_response(200 if rows else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(('127.0.0.1', port), Handler)
    _write(root/'server-ready.json', {'pid': os.getpid(), 'port': port})
    server.serve_forever()


def _http(url, item=None):
    request = urllib.request.Request(url, data=json.dumps(item).encode() if item is not None else None,
        headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        response = error
    with response:
        return json.load(response)


def _client(config_path, result_path, wait_after_failure):
    startup_wall = time.monotonic()
    startup_cpu = time.process_time()
    sys.path.insert(0, str(REPO))
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    os.environ['HERMES_HOME'] = config['home']
    os.environ['HERMES_KANBAN_DB'] = config['db']
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    # Cold imports are fixture startup, not the HTTP operation being measured.
    # The parent acknowledges a real imported interpreter before starting its
    # unchanged result deadline. No runtime or HTTP timeout is extended here.
    ready = Path(result_path).with_suffix('.ready.json')
    proceed = Path(result_path).with_suffix('.proceed.json')
    _write(ready, {'pid': os.getpid(), 'started_at': psutil.Process().create_time(),
        'phase': 'imports_complete', 'startup_wall_seconds': time.monotonic()-startup_wall,
        'startup_cpu_seconds': time.process_time()-startup_cpu})
    acknowledgement_deadline = time.monotonic()+60
    while not proceed.exists():
        assert time.monotonic() < acknowledgement_deadline, 'Parent did not acknowledge imported client'
        time.sleep(.025)
    args = {key: config[key] for key in ('operation', 'target', 'candidate')}
    trace = []
    with kb.connect_closing(Path(config['db'])) as conn:
        effect = delivery.begin_effect(conn, config['task_id'], config['run_id'], **args)
        trace.append({'action': 'begin', **effect})
        try:
            if effect['reconcile']:
                observed = _http(config['target']+'/'+effect['id'])
                trace.append({'action': 'GET', 'response': observed})
                evidence = {'readback': config['target']+'/'+effect['id'], **(observed['receipt'] or {})}
                delivery.reconcile_effect(conn, effect['id'], found=observed['found'], evidence=evidence,
                    caller_task_id=config['task_id'], caller_run_id=config['run_id'])
                effect = delivery.begin_effect(conn, config['task_id'], config['run_id'], **args)
                trace.append({'action': 'begin_after_readback', **effect})
            if effect['execute']:
                trace.append({'action': 'POST', 'effect_id': effect['id']})
                _http(config['target'], {'effect_id': effect['id'], **args})
                observed = _http(config['target']+'/'+effect['id'])
                trace.append({'action': 'GET', 'response': observed})
                assert observed['found']
                delivery.reconcile_effect(conn, effect['id'], found=True,
                    evidence={'readback': config['target']+'/'+effect['id'], **observed['receipt']},
                    caller_task_id=config['task_id'], caller_run_id=config['run_id'])
            status = conn.execute('SELECT status FROM nfos_effects WHERE id=?', (effect['id'],)).fetchone()[0]
            result = {'pid': os.getpid(), 'status': status, 'effect_id': effect['id'], 'trace': trace}
        except (OSError, http.client.HTTPException) as error:
            result = {'pid': os.getpid(), 'status': 'transport_unknown', 'effect_id': effect['id'],
                'error_type': type(error).__name__, 'trace': trace}
        except delivery.WorkflowError as error:
            result = {'pid': os.getpid(), 'status': 'readback_rejected', 'effect_id': effect['id'],
                'error': str(error), 'trace': trace}
    _write(result_path, result)
    if wait_after_failure and result['status'] == 'transport_unknown':
        while True:
            time.sleep(.1)


class Harness:
    def __init__(self, root):
        self.root = root
        self.processes = []
        self.lifecycle = []
        self.sequence = 0
        self.logs = {}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.server = None

    def spawn(self, *args):
        number = len(self.processes)+1
        stdout_path = self.root/f'child-{number}-stdout.log'
        stderr_path = self.root/f'child-{number}-stderr.log'
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), *map(str, args)],
                cwd=REPO, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=CREATE_FLAGS, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.logs[proc] = (stdout_path, stderr_path)
        self.processes.append(proc)
        self.lifecycle.append({'pid': proc.pid, 'action': 'spawn', 'role': args[0], 'at': time.time(),
            'stdout':str(stdout_path), 'stderr':str(stderr_path)})
        return proc

    def diagnostic(self, proc):
        return '\n'.join(str(path)+':\n'+path.read_text(encoding='utf-8', errors='replace')[-5000:]
            for path in self.logs.get(proc, ()) if path.exists())

    def wait_file(self, path, proc, *, timeout=15, phase='operation result'):
        deadline = time.monotonic()+timeout
        while not path.exists():
            assert proc.poll() is None, f'Child exited {proc.returncode} during {phase} before {path.name}\n{self.diagnostic(proc)}'
            assert time.monotonic() < deadline, f'Timed out during {phase} waiting for {path.name}\n{self.diagnostic(proc)}'
            time.sleep(.025)
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('pid') and proc.poll() is None:
            try:
                actual = psutil.Process(result['pid'])
                assert actual.pid == proc.pid or proc.pid in [p.pid for p in actual.parents()]
                self.lifecycle.append({'pid': actual.pid, 'start_time': actual.create_time(),
                    'launcher_pid': proc.pid, 'action': 'actual_process_confirmed', 'at': time.time()})
            except psutil.NoSuchProcess:
                # A successful short-lived client can exit between the durable
                # marker and this inspection; wait for its launcher to finish.
                assert proc.wait(timeout=10) == 0
                self.lifecycle.append({'pid': result['pid'], 'launcher_pid': proc.pid,
                    'action':'actual_client_already_exited', 'at':time.time()})
        return result

    def stop(self, proc):
        if proc is not None and proc.poll() is None:
            # Windows venv python.exe may launch the actual interpreter as a child.
            # Retain and terminate those owned descendants before the launcher.
            descendants = psutil.Process(proc.pid).children(recursive=True)
            for child in reversed(descendants):
                with contextlib.suppress(psutil.NoSuchProcess):
                    started = child.create_time()
                    child.kill()
                    child.wait(timeout=10)
                    self.lifecycle.append({'pid': child.pid, 'start_time': started,
                        'launcher_pid': proc.pid, 'action':'actual_child_killed_and_reaped', 'at':time.time()})
            proc.kill()
            proc.wait(timeout=10)
            self.lifecycle.append({'pid': proc.pid, 'action': 'killed_and_reaped', 'returncode': proc.returncode, 'at': time.time()})

    def start_server(self, drop_operation='none'):
        ready = self.root/'server-ready.json'
        if ready.exists():
            ready.unlink()
        self.server = self.spawn('--destination', self.root, self.port, drop_operation)
        self.wait_file(ready, self.server, timeout=60, phase='server startup')

    def client(self, config, hold=False):
        self.sequence += 1
        source = self.root/f'client-{self.sequence}-input.json'
        target = self.root/f'client-{self.sequence}-result.json'
        _write(source, config)
        proc = self.spawn('--client', source, target, 'hold' if hold else 'exit')
        ready = self.wait_file(target.with_suffix('.ready.json'), proc, timeout=60, phase='client imports')
        assert ready['phase'] == 'imports_complete'
        self.lifecycle.append({'action':'client_imports_complete', **ready, 'at':time.time()})
        _write(target.with_suffix('.proceed.json'), {'client_pid':ready['pid'], 'at':time.time()})
        result = self.wait_file(target, proc)
        if not hold:
            assert proc.wait(timeout=10) == 0
        return proc, result

    def close(self):
        for proc in reversed(self.processes):
            self.stop(proc)
        assert all(proc.poll() is not None for proc in self.processes)
        for item in self.lifecycle:
            if item['action']=='actual_process_confirmed' and psutil.pid_exists(item['pid']):
                assert psutil.Process(item['pid']).create_time()!=item['start_time'], 'Owned child survived cleanup'
        _write(self.root/'process-cleanup.json', {'all_reaped': True, 'processes': self.lifecycle})


@pytest.fixture
def harness(monkeypatch, request):
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    output = REPO/'outputs/nfos-http-recovery'
    output.mkdir(parents=True, exist_ok=True)
    label = request.node.callspec.params['failure'] if hasattr(request.node,'callspec') else 'wrong_candidate'
    root = Path(tempfile.mkdtemp(prefix=label+'-', dir=output))
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(root/'kanban.db'))
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(conn, source={'platform':'test','chat_id':'synthetic','thread_id':'local','message_id':'1'},
            text='Synthetic external effect recovery contract', project={'board':'synthetic-http','profile':'default','delivery_type':'code','repo_path':str(root)})
        pending = delivery.reserve_request(conn, capacity=1)
        task = delivery.bootstrap_card(conn, rid, pending['claim_token'], pid=os.getpid())
        delivery.save_spec(conn, task.id, task.current_run_id, {'goal':'Synthetic recovery contract',
            'criteria':[{'id':'AC1','text':'Destination effect remains singular and candidate identity matches'}],
            'steps':['Record intent','Contact synthetic HTTP destination','Read back after failure'],'delivery_type':'code'},
            author='Claude TL', evidence={'mode':'Synthetic enum fixture; no actual TL call or production approval'})
        assert delivery.acquire_project(conn, 'synthetic-http', task.id, task.current_run_id, CANDIDATE)
        (root/'synthetic-homolog.json').write_text(json.dumps({'candidate':CANDIDATE,'tree':TREE,'synthetic':True}))
        delivery.advance(conn, task.id, task.current_run_id, 'homolog', next_action='Synthetic external effects',
            state={'homolog_sha':CANDIDATE,'candidate_tree':TREE,'homolog_evidence':[str(root/'synthetic-homolog.json')]})
    instance = Harness(root)
    instance.config = {'home':str(root),'db':str(root/'kanban.db'),'task_id':task.id,'run_id':task.current_run_id,
        'target':f'http://127.0.0.1:{instance.port}/effects'}
    try:
        yield instance
    finally:
        instance.close()


def _effect_status(harness, effect_id):
    with sqlite3.connect(harness.config['db']) as conn:
        return conn.execute('SELECT status FROM nfos_effects WHERE id=?',(effect_id,)).fetchone()[0]


def _approve_synthetic(harness):
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    with kb.connect_closing(Path(harness.config['db'])) as conn:
        decision = delivery.ask_principal(conn, harness.config['task_id'], harness.config['run_id'],
            kind='review', question='Synthetic test approval, not a business approval', context={})
        delivery.resolve_decision(conn, decision, action='approve', answer='Synthetic fixture candidate only; no business approval', author='Principal')


@pytest.mark.parametrize('failure', ['response_lost', 'destination_offline'])
def test_http_effect_recovers_without_repeating_pr_merge_or_deploy(harness, failure):
    proofs = []
    for operation in ['pr', 'merge', 'deploy']:
        config = {**harness.config, 'operation':operation, 'candidate':INTEGRATED if operation=='deploy' else CANDIDATE}
        if operation == 'merge':
            _approve_synthetic(harness)
        harness.start_server(operation if failure=='response_lost' else 'none')
        if failure == 'destination_offline':
            harness.stop(harness.server)
        client, first = harness.client(config, hold=True)
        assert first['status'] == 'transport_unknown'
        assert _effect_status(harness, first['effect_id']) == 'unknown'
        assert client.poll() is None
        harness.stop(client)  # A real killed client, then a different process starts.
        harness.stop(harness.server)
        if failure == 'destination_offline':
            _, offline = harness.client(config)
            assert offline['status'] == 'transport_unknown'
            assert offline['trace'][0]['reconcile'] and not offline['trace'][0]['execute']
            assert _effect_status(harness, first['effect_id']) == 'unknown'
        # A new destination process reads the previous on-disk ledger.
        harness.start_server()
        restored, result = harness.client(config)
        assert restored.pid != client.pid
        assert result['status'] == 'confirmed'
        assert result['effect_id'] == first['effect_id']
        assert result['trace'][0]['reconcile'] and not result['trace'][0]['execute']
        _, repeated = harness.client(config)
        assert repeated['status'] == 'confirmed'
        assert [step['action'] for step in repeated['trace']] == ['begin']
        with sqlite3.connect(harness.root/'destination.db') as conn:
            rows = conn.execute('SELECT candidate,receipt FROM effects WHERE effect_id=?',(first['effect_id'],)).fetchall()
            requests = conn.execute('SELECT method,found FROM requests WHERE effect_id=? ORDER BY seq',(first['effect_id'],)).fetchall()
        assert len(rows) == 1 and rows[0][0] == config['candidate']
        assert sum(method=='POST' for method, _ in requests) == 1
        assert requests[0][0] == ('POST' if failure=='response_lost' else 'GET')
        if failure == 'destination_offline':
            assert requests[0] == ('GET', 0)  # Real absent readback precedes retry.
        proofs.append({'operation':operation,'candidate':config['candidate'],'effect_id':first['effect_id'],
            'destination_count':len(rows),'destination_requests':requests,'first_client':first,'restored_client':result})
        harness.stop(harness.server)
    _write(harness.root/'proof.json', {'status':'PASS','scenario':failure,'mode':'Real local HTTP/process/SQLite failures; synthetic PR/merge/deploy and review fixtures',
        'source_file':str(Path(__file__).resolve()),'destination_deduplicates':False,'effects':proofs,
        'limitations':['Does not exercise GitHub or a real deploy','Restarts transport client within the same NFOS run; runtime run recovery is separate']})


def test_http_readback_of_wrong_candidate_remains_unknown(harness):
    config = {**harness.config, 'operation':'pr', 'candidate':CANDIDATE}
    harness.start_server('pr')
    client, first = harness.client(config, hold=True)
    assert first['status'] == 'transport_unknown'
    harness.stop(client)
    with sqlite3.connect(harness.root/'destination.db') as conn:
        receipt = json.loads(conn.execute('SELECT receipt FROM effects').fetchone()[0])
        receipt['candidate'] = 'wrong-synthetic-candidate'
        conn.execute('UPDATE effects SET receipt=?',(json.dumps(receipt),))
    _, result = harness.client(config)
    assert result['status'] == 'readback_rejected'
    assert _effect_status(harness, first['effect_id']) == 'unknown'
    with sqlite3.connect(harness.root/'destination.db') as conn:
        assert conn.execute('SELECT count(*) FROM effects').fetchone()[0] == 1
    _write(harness.root/'proof.json', {'status':'PASS','scenario':'wrong_candidate_readback','mode':'Synthetic corrupt destination identity, real HTTP GET',
        'result':result,'effect_status':'unknown','destination_count':1})


if __name__ == '__main__':
    if sys.argv[1] == '--destination':
        _destination(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    elif sys.argv[1] == '--client':
        _client(sys.argv[2], sys.argv[3], sys.argv[4]=='hold')
    else:
        raise SystemExit('Only synthetic destination/client child modes are supported')
