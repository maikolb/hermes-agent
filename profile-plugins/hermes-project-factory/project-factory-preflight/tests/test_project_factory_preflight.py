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
