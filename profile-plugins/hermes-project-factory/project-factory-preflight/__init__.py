"""Atomic Project Factory preflight for newly-created Telegram project Topics.

The Hermes core creates the Topic, project binding, workspace, and Kanban board.
This profile-local extension completes that same tool result with the private
GitHub repository and a stable Workflow Factory first-code contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

PLUGIN_ID = "project-factory-preflight"
_DEFAULT_OWNER = "Project-Factory-26"
_DEFAULT_PROFILE = "hermes-project-factory"
_DEFAULT_WORKSPACE_ROOT = Path("/srv/projects")
_REPOSITORY_URL = "https://github.com/{repository}"


class PreflightError(RuntimeError):
    pass


def _settings() -> dict[str, Any]:
    settings: dict[str, Any] = {
        "owner": _DEFAULT_OWNER,
        "profile": _DEFAULT_PROFILE,
        "workspace_root": str(_DEFAULT_WORKSPACE_ROOT),
        "workflow_factory": shutil.which("workflow-factory") or "/usr/local/bin/workflow-factory",
    }
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        plugins = config.get("plugins")
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        configured = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
        if isinstance(configured, dict):
            for key in tuple(settings):
                if configured.get(key) not in (None, ""):
                    settings[key] = configured[key]
    except Exception:
        pass
    owner = str(settings["owner"]).strip()
    profile = str(settings["profile"]).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner):
        raise PreflightError("invalid GitHub owner in plugin configuration")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", profile):
        raise PreflightError("invalid Hermes profile in plugin configuration")
    return settings


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = 120) -> str:
    process = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        env=os.environ.copy(),
    )
    if process.returncode != 0:
        detail = " ".join((process.stderr or process.stdout or "command failed").split())
        raise PreflightError(f"{Path(argv[0]).name} failed: {detail[:600]}")
    return process.stdout.strip()


def _decode_result(result: Any) -> tuple[dict[str, Any] | None, bool]:
    if isinstance(result, dict):
        return dict(result), False
    if not isinstance(result, str):
        return None, False
    try:
        decoded = json.loads(result)
    except json.JSONDecodeError:
        return None, True
    return (dict(decoded), True) if isinstance(decoded, dict) else (None, True)


def _encode_result(payload: dict[str, Any], was_string: bool) -> Any:
    return json.dumps(payload, ensure_ascii=False) if was_string else payload


def _project_fields(payload: Mapping[str, Any]) -> tuple[str, str, Path]:
    project = payload.get("project")
    project = project if isinstance(project, Mapping) else {}
    project_id = str(project.get("id") or "").strip()
    name = str(project.get("name") or project_id).strip()
    workspace_raw = str(payload.get("workspace") or project.get("workdir") or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", project_id):
        raise PreflightError("project_topic_create returned an invalid project id")
    if not name:
        raise PreflightError("project_topic_create returned an empty project name")
    if not workspace_raw:
        raise PreflightError("project_topic_create returned no canonical workspace")
    return project_id, name, Path(workspace_raw).expanduser().resolve()


def _validate_workspace(workspace: Path, root: Path) -> None:
    root = root.expanduser().resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise PreflightError("canonical workspace escapes the configured Project OS root") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace.is_symlink():
        raise PreflightError("canonical workspace must not be a symlink")


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _manifest(name: str, slug: str, profile: str, repository: str, workspace: Path) -> dict[str, Any]:
    request_id = f"{slug}-first-code-v1"
    description = f"Projeto {name} criado e operado pelo Hermes Project Factory."
    argv = [
        "workflow-factory", "create",
        "--profile", profile,
        "--name", name,
        "--description", description,
        "--request-id", request_id,
        "--source", str(workspace),
    ]
    return {
        "schema_version": 1,
        "project": name,
        "slug": slug,
        "profile": profile,
        "github_repository": repository,
        "visibility": "private",
        "request_id": request_id,
        "source": str(workspace),
        "deployment": {
            "provider": "dokploy",
            "trigger": "first_production_dockerfile",
            "state": "pending_source",
            "command_argv": argv,
            "command_display": shlex.join(argv),
        },
        "success_contract": {
            "stage": "READY",
            "repository_private": True,
            "image_ref_must_contain": "@sha256:",
            "url_scheme": "https",
        },
    }


def _gh_repo(repository: str) -> dict[str, Any] | None:
    process = subprocess.run(
        ["gh", "repo", "view", repository, "--json", "nameWithOwner,isPrivate,url,visibility"],
        capture_output=True,
        text=True,
        shell=False,
        timeout=60,
        env=os.environ.copy(),
    )
    if process.returncode != 0:
        combined = " ".join((process.stderr + " " + process.stdout).split()).lower()
        if "could not resolve to a repository" in combined or "not found" in combined:
            return None
        raise PreflightError(f"GitHub repository lookup failed: {combined[:500]}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError("GitHub repository lookup returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PreflightError("GitHub repository lookup returned an invalid payload")
    return payload


def _ensure_repo(payload: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    slug, name, workspace = _project_fields(payload)
    owner = str(settings["owner"])
    profile = str(settings["profile"])
    root = Path(str(settings["workspace_root"]))
    workflow_factory = Path(str(settings["workflow_factory"]))
    _validate_workspace(workspace, root)
    if not workflow_factory.is_file() or not os.access(workflow_factory, os.X_OK):
        raise PreflightError("Workflow Factory executable is unavailable")

    doctor = json.loads(_run([str(workflow_factory), "doctor"], timeout=60))
    if not isinstance(doctor, dict) or doctor.get("ok") is not True:
        raise PreflightError("Workflow Factory doctor is not green")

    repository = f"{owner}/{slug}"
    workflow_dir = workspace / ".workflow-factory"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    contract_path = workflow_dir / "project.json"
    contract = _manifest(name, slug, profile, repository, workspace)
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    owned = [str(contract_path.relative_to(workspace))]

    ignore_path = workspace / ".gitignore"
    if _write_if_missing(
        ignore_path,
        "node_modules/\ndist/\nbuild/\n.next/\n.turbo/\ncoverage/\n.env\n.env.*\n!.env.example\n*.pem\n*.key\n*.p12\n*.pfx\n*.db\n*.sqlite*\n__pycache__/\n.venv/\n.hermes/\n",
    ):
        owned.append(str(ignore_path.relative_to(workspace)))
    readme_path = workspace / "README.md"
    if _write_if_missing(
        readme_path,
        f"# {name}\n\nWorkspace canônico do projeto **{name}**.\n\nO primeiro código com `Dockerfile` de produção segue `.workflow-factory/project.json` até `stage=READY`.\n",
    ):
        owned.append(str(readme_path.relative_to(workspace)))

    if not (workspace / ".git").exists():
        _run(["git", "init", "-b", "main"], cwd=workspace)
    _run(["git", "config", "user.name", "Hermes Project Factory"], cwd=workspace)
    _run(["git", "config", "user.email", "workflow-factory@users.noreply.github.com"], cwd=workspace)
    _run(["git", "branch", "-M", "main"], cwd=workspace)
    _run(["git", "add", "--", *owned], cwd=workspace)
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=workspace)
    if staged:
        _run(["git", "commit", "-m", "Initialize Project Factory preflight"], cwd=workspace)

    existing = _gh_repo(repository)
    if existing is not None and existing.get("isPrivate") is not True:
        raise PreflightError(f"existing repository is not private: {repository}")
    remotes = _run(["git", "remote"], cwd=workspace).splitlines()
    expected_remote = f"https://github.com/{repository}.git"
    if "origin" in remotes:
        current_remote = _run(["git", "remote", "get-url", "origin"], cwd=workspace)
        if current_remote.rstrip("/").removesuffix(".git").lower() != expected_remote.rstrip("/").removesuffix(".git").lower():
            raise PreflightError("existing origin points to a different repository")
    elif existing is not None:
        _run(["git", "remote", "add", "origin", expected_remote], cwd=workspace)

    if existing is None:
        _run([
            "gh", "repo", "create", repository, "--private", "--source", str(workspace),
            "--remote", "origin", "--push", "--description",
            f"{name} project workspace managed by Hermes Project Factory",
        ], timeout=180)
    else:
        _run(["git", "push", "-u", "origin", "main"], cwd=workspace, timeout=180)

    readback = _gh_repo(repository)
    if not readback or readback.get("isPrivate") is not True:
        raise PreflightError("private repository read-back failed")
    local_head = _run(["git", "rev-parse", "HEAD"], cwd=workspace)
    remote_head = _run(["git", "ls-remote", "origin", "refs/heads/main"], cwd=workspace).split()[0]
    if local_head != remote_head:
        raise PreflightError("local and remote main do not match after preflight")
    return {
        "status": "repository_ready",
        "repository": repository,
        "url": str(readback.get("url") or _REPOSITORY_URL.format(repository=repository)),
        "visibility": "PRIVATE",
        "request_id": contract["request_id"],
        "deployment_state": "pending_source",
        "head_sha": local_head,
        "contract": str(contract_path),
    }


def _transform_tool_result(tool_name: str = "", result: Any = None, status: str = "", **_: Any) -> Any:
    if tool_name != "project_topic_create" or status not in {"", "ok"}:
        return None
    payload, was_string = _decode_result(result)
    if not payload or payload.get("success") is not True:
        return None
    try:
        preflight = _ensure_repo(payload, _settings())
        payload["preflight"] = preflight
        payload["readiness"] = "repository_ready"
    except Exception as exc:
        logger.exception("Project Factory preflight failed")
        payload["success"] = False
        payload["readiness"] = "partial"
        payload["error"] = f"project preflight incomplete: {type(exc).__name__}: {str(exc)[:600]}"
        payload["partial_side_effect"] = {
            "topic_project_board_workspace_created": True,
            "repository_ready": False,
        }
    return _encode_result(payload, was_string)


def _write_runtime_registration_marker(profile_name: str) -> None:
    marker = Path(__file__).resolve().parent / "runtime-registration.json"
    temp = marker.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(
        json.dumps({
            "pid": os.getpid(),
            "profile": profile_name,
            "registered_at_epoch": time.time(),
            "hook": "transform_tool_result",
        }, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temp, marker)


def register(ctx: Any) -> None:
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    _write_runtime_registration_marker(str(getattr(ctx, "profile_name", "") or "unknown"))
