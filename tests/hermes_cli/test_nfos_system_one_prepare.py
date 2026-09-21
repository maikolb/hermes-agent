import importlib.util
import json
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location('system_one_prepare',
    Path(__file__).resolve().parents[2]/'scripts/nfos_system_one_prepare.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def baseline(tmp_path):
    values = {'entrypoint': '#!/bin/sh\n# synthetic runtime 6381\n',
              'config': 'memory:\n  memory_char_limit: 50000\ntimezone: America/Sao_Paulo\n'}
    result = {'baseline_kind': 'today_before_activation', 'files': {}}
    for name, value in values.items():
        current, saved = tmp_path/(name+'.current'), tmp_path/(name+'.before')
        current.write_text(value); saved.write_text(value)
        result['files'][name] = {'path': str(current), 'backup': str(saved), 'sha256': prepare.sha(current)}
    return result


def test_dry_run_refuses_drift_without_changing_current_or_baseline(tmp_path, monkeypatch):
    saved = baseline(tmp_path)
    manifest = tmp_path/'baseline.json'; manifest.write_text(json.dumps(saved))
    receipt = tmp_path/'receipt.json'
    config = Path(saved['files']['config']['path'])
    config.write_text(config.read_text()+'# concurrent owner change\n')
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    monkeypatch.setattr(sys, 'argv', ['prepare', 'dry-run', '--baseline', str(manifest), '--receipt', str(receipt)])
    assert prepare.main() == 2
    result = json.loads(receipt.read_text())
    assert result['status'] == 'REFUSED' and result['productive_mutations'] is False
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not result['drift']['matches']


def test_check_reports_prepared_only_and_has_no_activation_command(tmp_path, monkeypatch):
    manifest = tmp_path/'baseline.json'; manifest.write_text(json.dumps(baseline(tmp_path)))
    candidate = tmp_path/'candidate.json'; candidate.write_text(json.dumps({'sha': 'a'*40}))
    receipt = tmp_path/'check.json'
    monkeypatch.setattr(sys, 'argv', ['prepare', 'check', '--baseline', str(manifest), '--receipt', str(receipt),
                                   '--candidate-manifest', str(candidate)])
    assert prepare.main() == 0
    result = json.loads(receipt.read_text())
    assert result['status'] == 'PREPARED_ONLY' and result['candidate_sha'] == 'a'*40
    monkeypatch.setattr(sys, 'argv', ['prepare', 'activate', '--baseline', str(manifest), '--receipt', str(receipt)])
    with pytest.raises(SystemExit) as error:
        prepare.main()
    assert error.value.code == 2


def test_rehearsal_preserves_advancing_native_cards_runs_memory_and_credentials(tmp_path):
    saved = baseline(tmp_path)
    repository = Path(__file__).resolve().parents[2]
    workspace = tmp_path/'system-one-rehearsal-native-test'
    result = prepare.rehearse(saved, workspace, repository, repository, sys.executable)
    assert result['rehearsal'] == 'PASS' and result['runtime_config_restored'] is True
    assert result['state_preserved'] is True and result['sqlite_restored'] is False
    assert result['new_project_preserved'] is True
    assert result['added']['task'] in result['old_runtime_readback']['tasks']
    assert result['added']['run'] in result['old_runtime_readback']['runs']
    assert result['old_runtime_readback']['additive_events_read'] is True
    assert result['old_runtime_readback']['mode'] == 'sqlite-ro'
    assert prepare.check_baseline(saved)['matches']
    # This local test checks the mechanism; the VPS receipt must prove actual 6381 origin.
    assert result['old_runtime_readback']['kanban_module'].replace('\\','/').endswith('hermes_cli/kanban_db.py')


def test_rehearsal_refuses_existing_workspace_and_corrupted_backup(tmp_path):
    saved = baseline(tmp_path)
    workspace = tmp_path/'system-one-rehearsal-existing'; workspace.mkdir()
    sentinel = workspace/'work'; sentinel.write_text('preserve me')
    with pytest.raises(ValueError, match='new, explicitly named'):
        prepare.rehearse(saved, workspace, tmp_path, tmp_path, sys.executable)
    assert sentinel.read_text() == 'preserve me'
    Path(saved['files']['config']['backup']).write_text('altered baseline')
    new = tmp_path/'system-one-rehearsal-new'
    with pytest.raises(ValueError, match='baseline backup changed'):
        prepare.rehearse(saved, new, tmp_path, tmp_path, sys.executable)
    assert not new.exists()


def test_scoped_rollback_preserves_new_projects_and_refuses_concurrent_policy():
    before = {'kanban': {'delivery': {'projects': {'ccm': {'enabled': True}}}}, 'model': {'default': 'pinned'}}
    applied = {'kanban': {'delivery': {'projects': {'ccm': {'enabled': True}},
                                      'system_one': {'mode': 'shadow'}, 'decision_engines': {'allowed': ['laya']}}},
               'model': {'default': 'pinned'}}
    import copy
    current = copy.deepcopy(applied)
    current['kanban']['delivery']['projects']['new-project'] = {'enabled': True}
    restored = prepare.rollback_config(before, applied, current)
    assert restored['kanban']['delivery']['projects'] == current['kanban']['delivery']['projects']
    assert 'system_one' not in restored['kanban']['delivery']
    assert 'decision_engines' not in restored['kanban']['delivery']
    assert restored['model'] == before['model']
    current['kanban']['delivery']['system_one']['mode'] = 'active'
    snapshot = copy.deepcopy(current)
    with pytest.raises(ValueError, match='Scoped configuration drift'):
        prepare.rollback_config(before, applied, current)
    assert current == snapshot
