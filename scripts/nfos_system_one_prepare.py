"""System One preparation only: drift checks and isolated rollback rehearsal.

There is deliberately no activation, merge, restart, production write or training command.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROTECTED = ('/etc', '/usr/local', '/srv/hermes/profiles', '/opt')
SCOPED_KEYS = ('system_one', 'laya', 'jev', 'decision_engines')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output_path(value):
    path = Path(value).resolve()
    if path.suffix != '.json' or any(path == Path(p) or Path(p) in path.parents for p in PROTECTED):
        raise ValueError('Receipt must be a JSON preparation artifact outside production paths')
    return path


def command(args, *, env=None, cwd=None):
    kwargs = {}
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW, 'startupinfo': startup}
    result = subprocess.run(args, env=env, cwd=cwd, text=True, encoding='utf-8', capture_output=True,
                            check=False, timeout=120, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Native command exited {result.returncode}: {result.stderr.strip()}')
    return result.stdout


def check_baseline(baseline, *, check_units=True):
    """Compare current bytes with today's owner-captured baseline; do not refresh it."""
    result = []
    for name, item in sorted(baseline['files'].items()):
        actual = sha(item['path'])
        backup = sha(item['backup'])
        result.append({'name': name, 'path': item['path'], 'expected': item['sha256'],
                       'actual': actual, 'backup': backup,
                       'matches': actual == backup == item['sha256']})
    for unit, expected in sorted(baseline.get('unit_sha256', {}).items()):
        if not check_units:
            raise ValueError('Baseline includes unit hashes; skipping their readback is not allowed')
        actual = hashlib.sha256(command(['systemctl', 'cat', unit]).encode()).hexdigest()
        result.append({'name': unit, 'expected': expected, 'actual': actual, 'matches': actual == expected})
    return {'matches': bool(result) and all(r['matches'] for r in result), 'files': result}


def rollback_config(before, applied, current):
    """Restore only this upgrade's keys; preserve new projects and refuse owned-key drift."""
    def delivery(value):
        return (value.get('kanban') or {}).get('delivery') or {}
    previous, expected, latest = delivery(before), delivery(applied), delivery(current)
    missing = object()
    for key in SCOPED_KEYS:
        if latest.get(key, missing) != expected.get(key, missing):
            raise ValueError('Scoped configuration drift: '+key)
    restored = copy.deepcopy(current)
    target = restored.setdefault('kanban', {}).setdefault('delivery', {})
    for key in SCOPED_KEYS:
        if key in previous:
            target[key] = copy.deepcopy(previous[key])
        else:
            target.pop(key, None)
    return restored


NATIVE_CREATE = r'''
import json, pathlib, sqlite3, sys, time
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
root=pathlib.Path(sys.argv[1]); expected=pathlib.Path(sys.argv[2]).resolve(); phase=sys.argv[3]
assert pathlib.Path(kb.__file__).resolve().is_relative_to(expected), kb.__file__
db=root/'kanban.db'; workspace=root/'workspace';workspace.mkdir(exist_ok=True)
conn=kb.connect(db)
task=kb.create_task(conn,title='SYNTHETIC System One rollback '+phase,created_by='system-one-rehearsal',assignee='default',
 workspace_kind='dir',workspace_path=str(workspace),requires_repo=False,delivery_type='report')
claimed=kb.claim_task(conn,task,claimer='synthetic-rehearsal')
assert claimed is not None
run=kb.latest_run(conn,task);assert run is not None
if phase=='candidate':
 with kb.write_txn(conn):
  delivery._event(conn,task,run.id,'nfos_system_one_opportunity',{'opportunity_id':'synthetic-o1','input':{'state':{'synthetic':True}},'versions':{'schema':'1'}})
  delivery._event(conn,task,run.id,'nfos_system_one_outcome',{'opportunity_id':'synthetic-o1','outcome':{'status':'NOT_PROVEN'}})
conn.close()
print(json.dumps({'task':task,'run':run.id,'module':kb.__file__,'phase':phase}))
'''

NATIVE_READ = r'''
import hashlib,json,pathlib,sqlite3,sys
from hermes_cli import kanban_db as kb, nfos_reviewer as reviewer
root=pathlib.Path(sys.argv[1]);expected=pathlib.Path(sys.argv[2]).resolve()
assert pathlib.Path(kb.__file__).resolve().is_relative_to(expected), kb.__file__
assert pathlib.Path(reviewer.__file__).resolve().is_relative_to(expected), reviewer.__file__
db=root/'kanban.db'
conn=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
ids=[row[0] for row in conn.execute('SELECT id FROM tasks ORDER BY id')]
tasks=[kb.get_task(conn,tid) for tid in ids]
runs=[r.id for tid in ids for r in kb.list_runs(conn,tid)]
kinds=[e.kind for tid in ids for e in kb.list_events(conn,tid)]
conn.close()
cases=reviewer.collect(db,'synthetic-rehearsal',0,9999999999)
assert len(tasks)==2 and len(runs)==2
assert {'nfos_system_one_opportunity','nfos_system_one_outcome'} <= set(kinds)
assert len(cases)==2
print(json.dumps({'tasks':ids,'runs':runs,'additive_events_read':True,'reviewer_cases':len(cases),
 'kanban_module':kb.__file__,'reviewer_module':reviewer.__file__,'mode':'sqlite-ro'}))
'''


def native_process(python, runtime, workspace, code, *args):
    env = dict(os.environ)
    env.update(HERMES_HOME=str(workspace/'native-home'), HERMES_KANBAN_HOME=str(workspace),
               PYTHONPATH=str(Path(runtime).resolve()), HERMES_SKIP_CONTEXT_FILES='1')
    env.pop('HERMES_PROFILE', None)
    env.pop('HERMES_KANBAN_TASK', None)
    env.pop('HERMES_KANBAN_RUN_ID', None)
    output = command([str(python), '-c', code, str(workspace), str(runtime), *args], env=env, cwd=workspace)
    return json.loads(output.strip().splitlines()[-1])


def rehearse(baseline, workspace, old_runtime, candidate_runtime, python, candidate_config=None):
    """Swap only isolated pointer/config copies, then prove old code reads advancing state."""
    workspace = Path(workspace).resolve()
    if not workspace.name.startswith('system-one-rehearsal-') or workspace.exists():
        raise ValueError('Use a new, explicitly named system-one-rehearsal-* directory')
    if any(workspace == Path(p) or Path(p) in workspace.parents for p in PROTECTED):
        raise ValueError('Rehearsal workspace cannot be a production path')
    for item in baseline['files'].values():
        if sha(item['backup']) != item['sha256']:
            raise ValueError('Captured baseline backup changed')
    workspace.mkdir(parents=True, mode=0o700)
    native_home = workspace/'native-home'; native_home.mkdir(mode=0o700)
    (native_home/'config.yaml').write_text('kanban:\n  default_assignee: default\n  executor_profiles: [default]\n', encoding='utf-8')
    pointer, config = workspace/'runtime.entrypoint', workspace/'config.yaml'
    shutil.copyfile(baseline['files']['entrypoint']['backup'], pointer)
    shutil.copyfile(baseline['files']['config']['backup'], config)
    for path in (pointer, config):
        path.chmod(0o600)
    before = {'runtime': sha(pointer), 'config': sha(config)}
    original = native_process(python, old_runtime, workspace, NATIVE_CREATE, 'baseline')
    pointer.write_text('PREPARATION ONLY: '+str(candidate_runtime)+'\n', encoding='utf-8')
    import yaml
    before_config = yaml.safe_load(config.read_text(encoding='utf-8')) or {}
    applied_config = yaml.safe_load(Path(candidate_config).read_text(encoding='utf-8')) if candidate_config else copy.deepcopy(before_config)
    if not candidate_config:
        applied_config.setdefault('kanban', {}).setdefault('delivery', {})['system_one'] = {'mode': 'shadow'}
    config.write_text(yaml.safe_dump(applied_config, sort_keys=False), encoding='utf-8')
    added = native_process(python, candidate_runtime, workspace, NATIVE_CREATE, 'candidate')
    # Only synthetic markers are used. No productive credential or memory is copied.
    memory, credential = workspace/'MEMORY.md', workspace/'.env'
    memory.write_text('Synthetic memory written after candidate switch. Must survive rollback.\n', encoding='utf-8')
    credential.write_text('SYNTHETIC_REHEARSAL_TOKEN=not-a-real-credential\n', encoding='utf-8')
    memory.chmod(0o600); credential.chmod(0o600)
    keep = {'memory': sha(memory), 'credential': sha(credential)}
    db = workspace/'kanban.db'
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    keep['sqlite'] = sha(db)
    # A new project arrives after the hypothetical switch. Rollback must retain it.
    current_config = copy.deepcopy(applied_config)
    current_config.setdefault('kanban', {}).setdefault('delivery', {}).setdefault('projects', {})[
        'synthetic-project-after-upgrade'] = {'synthetic': True}
    config.write_text(yaml.safe_dump(current_config, sort_keys=False), encoding='utf-8')
    # Rollback of runtime/config only. In particular, never restore a DB snapshot.
    shutil.copyfile(baseline['files']['entrypoint']['backup'], pointer)
    restored_config = rollback_config(before_config, applied_config, current_config)
    config.write_text(yaml.safe_dump(restored_config, sort_keys=False), encoding='utf-8')
    readback = native_process(python, old_runtime, workspace, NATIVE_READ)
    after = {'runtime': sha(pointer), 'config': sha(config)}
    preserved = {'memory': sha(memory), 'credential': sha(credential), 'sqlite': sha(db)}
    scoped_restored = all((restored_config.get('kanban', {}).get('delivery', {}).get(k) ==
        before_config.get('kanban', {}).get('delivery', {}).get(k)) for k in SCOPED_KEYS)
    assert before['runtime'] == after['runtime'] and scoped_restored and keep == preserved
    assert restored_config['kanban']['delivery']['projects']['synthetic-project-after-upgrade'] == {'synthetic': True}
    assert added['task'] in readback['tasks'] and added['run'] in readback['runs']
    return {'status': 'PREPARED_ONLY', 'productive_mutations': False, 'rehearsal': 'PASS',
            'workspace': str(workspace), 'runtime_config_restored': before['runtime'] == after['runtime'] and scoped_restored,
            'new_project_preserved': True, 'scoped_config_keys': list(SCOPED_KEYS),
            'native_home': 'isolated minimal executor fixture; productive config copied only for rollback comparison',
            'state_preserved': keep == preserved, 'before': before, 'after': after,
            'preserved_sha256': preserved, 'original': original, 'added': added,
            'old_runtime_readback': readback, 'sqlite_restored': False,
            'credentials': 'synthetic marker only; productive credentials untouched'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'dry-run', 'rehearsal'])
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--candidate-manifest')
    parser.add_argument('--workspace')
    parser.add_argument('--old-runtime')
    parser.add_argument('--candidate-runtime')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--candidate-config')
    args = parser.parse_args()
    receipt = output_path(args.receipt)
    result = {'status': 'PREPARED_ONLY', 'productive_mutations': False,
              'action': args.action, 'at': time.time()}
    code = 0
    try:
        baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        if args.action == 'rehearsal':
            if not all([args.workspace, args.old_runtime, args.candidate_runtime]):
                raise ValueError('Rehearsal requires workspace, old-runtime and candidate-runtime')
            result.update(rehearse(baseline, args.workspace, args.old_runtime,
                                  args.candidate_runtime, args.python, args.candidate_config))
        else:
            result['drift'] = check_baseline(baseline)
            if not result['drift']['matches']:
                raise ValueError('Productive baseline drift: reconcile, never overwrite today\'s baseline')
            if args.candidate_manifest:
                manifest = json.loads(Path(args.candidate_manifest).read_text(encoding='utf-8'))
                candidate_sha = manifest.get('sha', '')
                if len(candidate_sha) != 40 or any(c not in '0123456789abcdef' for c in candidate_sha):
                    raise ValueError('Manifest must pin exact integrated Git SHA')
                result['candidate_sha'] = candidate_sha
                result['candidate_manifest_sha256'] = sha(args.candidate_manifest)
            result['proposed_order'] = ['fresh master HOLD review and exact SHA confirmation',
                'fresh baseline/rollout readback and designated deployment lock',
                'immutable artifacts and proportional current backups',
                'Laya isolated service identity and warm readiness',
                'NFOS profile-only pointer/config and worker-preserving native drain',
                'Suporte consumer alignment without changing its source package',
                'Vigilia only if its separately reviewed companion UI is included',
                'native target readback, preserved workers/state, final receipt']
    except (ValueError, KeyError, TypeError, OSError, AssertionError, RuntimeError, subprocess.SubprocessError) as exc:
        result.update(status='REFUSED', reason=str(exc))
        code = 2
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    receipt.chmod(0o600)
    print(json.dumps({'status': result['status'], 'receipt': str(receipt),
                      'productive_mutations': False, 'exit_code': code}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
