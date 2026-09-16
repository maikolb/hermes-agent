from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
SPEC = importlib.util.spec_from_file_location("project_factory_preflight_test", PLUGIN)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _success_payload(tmp_path: Path) -> dict:
    return {
        "success": True,
        "created": True,
        "workspace": str(tmp_path / "alpha"),
        "project": {"id": "alpha", "name": "Alpha", "workdir": str(tmp_path / "alpha")},
    }


def test_manifest_contract_is_stable(tmp_path: Path):
    workspace = tmp_path / "alpha"
    contract = module._manifest("Alpha", "alpha", "hermes-project-factory", "Project-Factory-26/alpha", workspace)
    assert contract["request_id"] == "alpha-first-code-v1"
    assert contract["deployment"]["trigger"] == "first_production_dockerfile"
    assert contract["success_contract"]["stage"] == "READY"
    assert contract["success_contract"]["image_ref_must_contain"] == "@sha256:"
    assert contract["deployment"]["command_argv"][-1] == str(workspace)


def test_transform_enriches_success_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = _success_payload(tmp_path)
    expected = {"status": "repository_ready", "repository": "Project-Factory-26/alpha"}
    monkeypatch.setattr(module, "_settings", lambda: {})
    monkeypatch.setattr(module, "_ensure_repo", lambda value, settings: expected)
    transformed = module._transform_tool_result(
        tool_name="project_topic_create", result=json.dumps(payload), status="ok"
    )
    decoded = json.loads(transformed)
    assert decoded["success"] is True
    assert decoded["readiness"] == "repository_ready"
    assert decoded["preflight"] == expected


def test_transform_fails_closed_and_preserves_partial_side_effect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = _success_payload(tmp_path)
    monkeypatch.setattr(module, "_settings", lambda: {})

    def fail(value, settings):
        raise module.PreflightError("GitHub unavailable")

    monkeypatch.setattr(module, "_ensure_repo", fail)
    transformed = module._transform_tool_result(
        tool_name="project_topic_create", result=payload, status="ok"
    )
    assert transformed["success"] is False
    assert transformed["readiness"] == "partial"
    assert transformed["partial_side_effect"]["repository_ready"] is False
    assert "GitHub unavailable" in transformed["error"]


def test_transform_ignores_other_tools(tmp_path: Path):
    assert module._transform_tool_result(tool_name="kanban_create", result=_success_payload(tmp_path), status="ok") is None


def test_ensure_repo_end_to_end_with_local_git_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = _success_payload(tmp_path)
    workspace = Path(payload["workspace"])
    factory = tmp_path / "workflow-factory"
    factory.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    factory.chmod(0o755)
    bare = tmp_path / "remote.git"
    module._run(["git", "init", "--bare", str(bare)])
    state = {"exists": False}
    real_run = module._run

    def fake_repo(repository: str):
        if not state["exists"]:
            return None
        return {
            "nameWithOwner": repository,
            "isPrivate": True,
            "visibility": "PRIVATE",
            "url": f"https://github.com/{repository}",
        }

    def fake_run(argv, *, cwd=None, timeout=120):
        if argv == [str(factory), "doctor"]:
            return json.dumps({"ok": True})
        if argv[:3] == ["gh", "repo", "create"]:
            real_run(["git", "remote", "add", "origin", str(bare)], cwd=workspace)
            real_run(["git", "push", "-u", "origin", "main"], cwd=workspace)
            state["exists"] = True
            return "https://github.com/Project-Factory-26/alpha"
        return real_run(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(module, "_gh_repo", fake_repo)
    monkeypatch.setattr(module, "_run", fake_run)
    result = module._ensure_repo(
        payload,
        {
            "owner": "Project-Factory-26",
            "profile": "hermes-project-factory",
            "workspace_root": str(tmp_path),
            "workflow_factory": str(factory),
        },
    )
    assert result["status"] == "repository_ready"
    assert result["visibility"] == "PRIVATE"
    assert result["deployment_state"] == "pending_source"
    assert result["head_sha"]
    contract = json.loads((workspace / ".workflow-factory/project.json").read_text(encoding="utf-8"))
    assert contract["request_id"] == "alpha-first-code-v1"
    assert contract["deployment"]["state"] == "pending_source"


def test_workspace_cannot_escape_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    with pytest.raises(module.PreflightError, match="escapes"):
        module._validate_workspace(outside, root)


def test_existing_repository_is_cloned_without_initializing_or_pushing(monkeypatch, tmp_path):
    bare=tmp_path/'existing.git';seed=tmp_path/'seed';seed.mkdir()
    module._run(['git','init','--bare',str(bare)])
    module._run(['git','init','-b','main'],cwd=seed)
    module._run(['git','config','user.name','Test'],cwd=seed)
    module._run(['git','config','user.email','test@example.invalid'],cwd=seed)
    (seed/'application.txt').write_text('existing application')
    module._run(['git','add','.'],cwd=seed);module._run(['git','commit','-m','Application'],cwd=seed)
    module._run(['git','remote','add','origin',str(bare)],cwd=seed)
    module._run(['git','push','origin','main'],cwd=seed)
    module._run(['git','symbolic-ref','HEAD','refs/heads/main'],cwd=bare)
    head=module._run(['git','rev-parse','HEAD'],cwd=seed)
    payload=_success_payload(tmp_path);real=module._run;calls=[]
    monkeypatch.setattr(module,'_gh_repo',lambda repo:{'isPrivate':True,'url':f'https://github.com/{repo}'})
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[:2]==['git','clone']:
            argv=[str(bare) if str(x).startswith('https://github.com/') else x for x in argv]
        if argv[:4]==['git','remote','get-url','origin']:
            return 'https://github.com/Project-Factory-26/alpha.git'
        return real(argv,**kwargs)
    monkeypatch.setattr(module,'_run',run)
    result=module._ensure_repo(payload,{'owner':'Project-Factory-26','profile':'hermes-project-factory',
        'workspace_root':str(tmp_path),'workflow_factory':str(tmp_path/'unavailable-factory')})
    assert result['status']=='repository_ready'
    assert (Path(payload['workspace'])/'application.txt').read_text()=='existing application'
    assert real(['git','rev-parse','HEAD'],cwd=Path(payload['workspace']))==head
    assert not any(argv[:2] in (['git','push'],['git','init'],['git','commit']) for argv in calls)


def test_onboarding_enables_delivery_once_and_preserves_explicit_project_settings(monkeypatch,tmp_path):
    from hermes_cli import config
    saved={'kanban':{'delivery':{'projects':{}}},'unrelated':{'keep':1}}
    monkeypatch.setattr(config,'load_config_readonly',lambda:saved)
    monkeypatch.setattr(config,'get_config_path',lambda:tmp_path/'config.yaml')
    writes=[]
    monkeypatch.setattr(config,'save_config',lambda value,**kwargs:writes.append(value))
    monkeypatch.setattr(module,'_settings',lambda:{'profile':'hermes-project-factory'})
    monkeypatch.setattr(module,'_ensure_repo',lambda *args:{'status':'repository_ready','repository':'Project-Factory-26/alpha'})
    payload=dict(_success_payload(tmp_path),profile='hermes-project-factory',board_slug='team--alpha',
        source={'platform':'telegram','chat_id':'100','thread_id':'200'})
    assert module._on_project_provisioned(**payload)['success']
    project=saved['kanban']['delivery']['projects']['team--alpha']
    assert project['enabled'] and project['repo_path']==payload['workspace']
    assert project['source']['thread_id']=='200'
    project['delivery_environment']='hml';project['enabled']=False
    assert module._on_project_provisioned(**payload)['success']
    assert project['delivery_environment']=='hml' and project['enabled'] is False
    assert saved['unrelated']=={'keep':1} and len(writes)==1


def test_actual_flat_tool_payload_and_other_profiles(tmp_path,monkeypatch):
    fields=module._project_fields({'slug':'alpha','project_id':'p_1234','workdir':str(tmp_path/'alpha')})
    assert fields[0]=='alpha' and fields[2]==tmp_path/'alpha'
    monkeypatch.setattr(module,'_settings',lambda:{'profile':'hermes-project-factory'})
    assert module._on_project_provisioned(profile='other') is None


def test_human_topic_provisions_repository_and_native_nfos_config_idempotently(tmp_path,monkeypatch):
    from gateway.project_router import ProjectRouter
    from hermes_cli import config,lifecycle,nfos_runtime
    home=tmp_path/'home';home.mkdir();root=tmp_path/'projects'
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(home/'kanban'))
    monkeypatch.setattr(config,'get_config_path',lambda:home/'config.yaml')
    settings={'owner':'Project-Factory-26','profile':'hermes-project-factory',
        'workspace_root':str(root),'workflow_factory':str(tmp_path/'unused')}
    monkeypatch.setattr(module,'_settings',lambda:settings)
    real=module._run;bare=tmp_path/'remote.git';real(['git','init','--bare',str(bare)])
    created=[]
    monkeypatch.setattr(module,'_gh_repo',lambda repo:None if not created else {'isPrivate':True,'url':f'https://github.com/{repo}'})
    def run(argv,**kwargs):
        if argv[:3]==['gh','repo','create']:
            created.append(argv[3]);workspace=Path(argv[argv.index('--source')+1])
            real(['git','remote','add','origin',str(bare)],cwd=workspace)
            real(['git','push','origin','main'],cwd=workspace)
            return ''
        return real(argv,**kwargs)
    monkeypatch.setattr(module,'_run',run)
    monkeypatch.setattr(lifecycle,'has_hook',lambda name:name=='on_project_provisioned')
    monkeypatch.setattr(lifecycle,'invoke_hook',lambda name,**payload:[module._on_project_provisioned(**payload)])
    with ProjectRouter(tmp_path/'router.db','hermes-project-factory') as router:
        for _ in range(2):
            project=router.provision_topic_project('Alpha','Alpha','telegram','100','200',
                workspace_root=root,allowed_users={'7':'allow'},board_creator=lambda *args,**kwargs:None)
    cfg=config.load_config_readonly()
    enabled=nfos_runtime.project_config(project.board_slug,cfg)
    assert enabled['enabled'] and enabled['repo_path']==str(project.workdir)
    assert enabled['source']['thread_id']=='200'
    assert enabled['onboarding_repository']['repository']=='Project-Factory-26/alpha'
    assert created==['Project-Factory-26/alpha']
    assert real(['git','rev-parse','HEAD'],cwd=project.workdir)==real(['git','rev-parse','refs/heads/main'],cwd=bare)
