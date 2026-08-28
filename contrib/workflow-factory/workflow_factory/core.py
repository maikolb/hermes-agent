from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAGES = (
    "REQUESTED",
    "WORKSPACE_CREATED",
    "REPO_CREATED",
    "CI_GREEN",
    "DOKPLOY_CONFIGURED",
    "DEPLOYED",
    "READY",
)


class FactoryError(RuntimeError):
    pass


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    if not slug or len(slug) > 63:
        raise FactoryError("project name must produce a non-empty slug of at most 63 characters")
    return slug


def read_secret(path: str | None, label: str) -> str:
    if not path:
        raise FactoryError(f"{label} token file is not configured")
    token_path = Path(path)
    try:
        value = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FactoryError(f"cannot read {label} token file: {token_path}") from exc
    if not value:
        raise FactoryError(f"{label} token file is empty: {token_path}")
    return value


@dataclass(frozen=True)
class FactoryConfig:
    profiles_path: Path
    workspaces_root: Path
    allowed_source_root: Path
    state_db: Path
    domain_base: str
    github_cli: str
    github_token_file: str | None
    dokploy_base_url: str
    dokploy_api_key_file: str | None
    registry_username: str
    registry_token_file: str | None
    poll_seconds: int
    ci_timeout_seconds: int
    deploy_timeout_seconds: int

    @classmethod
    def load(cls, path: Path) -> "FactoryConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactoryError(f"cannot load factory config: {path}") from exc
        github = raw.get("github", {})
        dokploy = raw.get("dokploy", {})
        config = cls(
            profiles_path=Path(raw["profiles_path"]),
            workspaces_root=Path(raw.get("workspaces_root", "/srv/projects/factory")),
            allowed_source_root=Path(raw.get("allowed_source_root", "/srv/projects")),
            state_db=Path(raw.get("state_db", "/var/lib/workflow-factory/factory.db")),
            domain_base=str(raw.get("domain_base", "")).strip(". "),
            github_cli=str(github.get("cli", "gh")),
            github_token_file=github.get("token_file"),
            dokploy_base_url=str(dokploy.get("base_url", "http://127.0.0.1:3000/api")).rstrip("/"),
            dokploy_api_key_file=dokploy.get("api_key_file"),
            registry_username=str(dokploy.get("registry_username", "")),
            registry_token_file=dokploy.get("registry_token_file"),
            poll_seconds=max(1, int(raw.get("poll_seconds", 10))),
            ci_timeout_seconds=max(60, int(raw.get("ci_timeout_seconds", 1800))),
            deploy_timeout_seconds=max(60, int(raw.get("deploy_timeout_seconds", 900))),
        )
        if not config.domain_base:
            raise FactoryError("domain_base is required (for example: example.com.br)")
        return config

    def owner_for(self, profile: str) -> str:
        try:
            payload = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            owner = payload["profiles"][profile]["github_owner"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise FactoryError(f"unknown Hermes profile: {profile}") from exc
        if not owner:
            raise FactoryError(f"profile {profile} has no approved GitHub owner; refusing to guess")
        return str(owner)


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY,
              profile TEXT NOT NULL,
              owner TEXT NOT NULL,
              project_name TEXT NOT NULL,
              slug TEXT NOT NULL,
              description TEXT NOT NULL,
              stage TEXT NOT NULL,
              workspace TEXT,
              repository TEXT,
              head_sha TEXT,
              run_url TEXT,
              image_ref TEXT,
              dokploy_project_id TEXT,
              environment_id TEXT,
              application_id TEXT,
              url TEXT,
              error TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def get_or_create(
        self, request_id: str, profile: str, owner: str, project_name: str, slug: str, description: str
    ) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row:
            existing = dict(row)
            identity = (profile, owner, project_name, slug)
            if identity != tuple(existing[key] for key in ("profile", "owner", "project_name", "slug")):
                raise FactoryError("request-id already belongs to a different project request")
            return existing
        self.connection.execute(
            """INSERT INTO requests
               (request_id, profile, owner, project_name, slug, description, stage)
               VALUES (?, ?, ?, ?, ?, ?, 'REQUESTED')""",
            (request_id, profile, owner, project_name, slug, description),
        )
        self.connection.commit()
        return self.get(request_id)

    def get(self, request_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            raise FactoryError(f"unknown request-id: {request_id}")
        return dict(row)

    def update(self, request_id: str, stage: str | None = None, **values: Any) -> dict[str, Any]:
        if stage is not None:
            if stage not in STAGES:
                raise FactoryError(f"invalid stage: {stage}")
            values["stage"] = stage
        if not values:
            return self.get(request_id)
        values["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE requests SET {assignments} WHERE request_id = ?",  # keys are internal constants
            (*values.values(), request_id),
        )
        self.connection.commit()
        return self.get(request_id)


def stage_at_least(current: str, target: str) -> bool:
    return STAGES.index(current) >= STAGES.index(target)


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=merged_env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise FactoryError(f"cannot run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise FactoryError(f"command failed ({command[0]}): {detail[-1500:]}")
    return result.stdout.strip()


BUILD_WORKFLOW = """name: build-image
on:
  push:
    branches: [main]
permissions:
  contents: read
  packages: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: vars
        shell: bash
        run: echo "image=ghcr.io/${GITHUB_REPOSITORY,,}" >> "$GITHUB_OUTPUT"
      - id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.vars.outputs.image }}:${{ github.sha }}
          platforms: linux/amd64
      - shell: bash
        run: printf '%s@%s\\n' '${{ steps.vars.outputs.image }}' '${{ steps.build.outputs.digest }}' > image-ref.txt
      - uses: actions/upload-artifact@v4
        with:
          name: deployment-image
          path: image-ref.txt
          retention-days: 7
"""


def install_factory_workflow(workspace: Path) -> None:
    workflow = workspace / ".github/workflows/workflow-factory-build.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(BUILD_WORKFLOW, encoding="utf-8", newline="\n")


def validate_migration_policy(workspace: Path) -> None:
    migration_markers = (
        workspace / "migrations",
        workspace / "prisma/migrations",
        workspace / "alembic",
        workspace / "alembic.ini",
    )
    if not any(path.exists() for path in migration_markers):
        return
    policy_path = workspace / ".workflow-factory/migration-policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError(
            "database migrations detected; .workflow-factory/migration-policy.json is required"
        ) from exc
    if policy.get("strategy") != "expand-contract" or policy.get("backward_compatible") is not True:
        raise FactoryError("automatic deploy requires expand-contract and backward_compatible=true")


def prepare_existing_project(workspace: Path, allowed_root: Path) -> None:
    workspace = workspace.resolve()
    allowed_root = allowed_root.resolve()
    if allowed_root not in workspace.parents:
        raise FactoryError(f"source must be inside {allowed_root}")
    if not workspace.is_dir():
        raise FactoryError(f"source project does not exist: {workspace}")
    if not (workspace / "Dockerfile").is_file():
        raise FactoryError("source project must contain a production Dockerfile")
    validate_migration_policy(workspace)
    install_factory_workflow(workspace)


def scaffold_static_site(workspace: Path, project_name: str, description: str) -> None:
    marker = workspace / ".workflow-factory.json"
    if workspace.exists() and not marker.exists():
        raise FactoryError(f"workspace exists without factory marker: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    safe_title = project_name.replace("<", "&lt;").replace(">", "&gt;")
    safe_description = description.replace("<", "&lt;").replace(">", "&gt;")
    files = {
        "index.html": f"""<!doctype html>
<html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{safe_title}</title><style>body{{font:18px system-ui;max-width:760px;margin:12vh auto;padding:24px;background:#111;color:#eee}}h1{{font-size:3rem}}code{{color:#9fe870}}</style></head>
<body><h1>{safe_title}</h1><p>{safe_description}</p><p><code>Publicado automaticamente por Hermes.</code></p></body></html>
""",
        "nginx.conf": """server {
  listen 80;
  server_name _;
  root /usr/share/nginx/html;
  location = /healthz { access_log off; add_header Content-Type text/plain; return 200 'ok'; }
  location / { try_files $uri $uri/ /index.html; }
}
""",
        "Dockerfile": """FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1/healthz || exit 1
""",
        ".dockerignore": ".git\n.github\nREADME.md\n",
        "README.md": f"# {project_name}\n\n{description}\n\nManaged by Workflow Factory.\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    marker.write_text(json.dumps({"schema_version": 1, "template": "static-site"}, indent=2), encoding="utf-8")
    install_factory_workflow(workspace)


class GitHub:
    def __init__(self, config: FactoryConfig):
        self.cli = config.github_cli
        self.env = {"GH_TOKEN": read_secret(config.github_token_file, "GitHub")}

    def call(self, args: list[str], cwd: Path | None = None) -> str:
        return run([self.cli, *args], cwd=cwd, env=self.env)

    def ensure_private_repo(self, owner: str, slug: str, workspace: Path) -> tuple[str, str]:
        repository = f"{owner}/{slug}"
        self.call(["auth", "setup-git"])
        run(["git", "init", "-b", "main"], cwd=workspace)
        run(["git", "config", "user.name", "Workflow Factory"], cwd=workspace)
        run(["git", "config", "user.email", "workflow-factory@users.noreply.github.com"], cwd=workspace)
        run(["git", "add", "."], cwd=workspace)
        status = run(["git", "status", "--porcelain"], cwd=workspace)
        if status:
            run(["git", "commit", "-m", "Initial project generated by Hermes"], cwd=workspace)
        try:
            visibility = self.call(["repo", "view", repository, "--json", "visibility", "--jq", ".visibility"])
            if visibility.upper() != "PRIVATE":
                raise FactoryError(f"existing repository is not private: {repository}")
            remotes = run(["git", "remote"], cwd=workspace)
            if "origin" not in remotes.splitlines():
                run(["git", "remote", "add", "origin", f"https://github.com/{repository}.git"], cwd=workspace)
            run(["git", "push", "--set-upstream", "origin", "main"], cwd=workspace, env=self.env)
        except FactoryError as exc:
            if "Could not resolve to a Repository" not in str(exc) and "GraphQL: Could not resolve" not in str(exc):
                raise
            self.call(["repo", "create", repository, "--private", "--source", str(workspace), "--remote", "origin", "--push"])
        head_sha = run(["git", "rev-parse", "HEAD"], cwd=workspace)
        return repository, head_sha

    def wait_for_image(self, repository: str, head_sha: str, output_dir: Path, timeout: int, poll: int) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        run_id: str | None = None
        run_url = ""
        while time.monotonic() < deadline:
            payload = json.loads(
                self.call([
                    "run", "list", "--repo", repository, "--workflow", "workflow-factory-build.yml",
                    "--limit", "20", "--json", "databaseId,headSha,status,conclusion,url",
                ]) or "[]"
            )
            matching = next((item for item in payload if item.get("headSha") == head_sha), None)
            if not matching:
                time.sleep(poll)
                continue
            run_id = str(matching["databaseId"])
            run_url = str(matching.get("url", ""))
            if matching.get("status") == "completed":
                if matching.get("conclusion") != "success":
                    raise FactoryError(f"GitHub Actions failed: {run_url}")
                break
            time.sleep(poll)
        if not run_id:
            raise FactoryError("timed out waiting for GitHub Actions to start")
        if time.monotonic() >= deadline:
            raise FactoryError(f"timed out waiting for GitHub Actions: {run_url}")
        output_dir.mkdir(parents=True, exist_ok=True)
        self.call(["run", "download", run_id, "--repo", repository, "--name", "deployment-image", "--dir", str(output_dir)])
        image_ref = (output_dir / "image-ref.txt").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}", image_ref):
            raise FactoryError(f"invalid immutable image reference from CI: {image_ref}")
        return image_ref, run_url


class Dokploy:
    def __init__(self, config: FactoryConfig):
        self.config = config
        self.api_key = read_secret(config.dokploy_api_key_file, "Dokploy")

    def request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.config.dokploy_base_url}/{endpoint.lstrip('/')}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "x-api-key": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[-1500:]
            raise FactoryError(f"Dokploy {endpoint} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise FactoryError(f"Dokploy is unavailable at {self.config.dokploy_base_url}: {exc.reason}") from exc

    def all_projects(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "project.all")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        raise FactoryError("unexpected project.all response from Dokploy")

    def ensure_application(self, owner: str, slug: str, image_ref: str) -> tuple[str, str, str]:
        project_name = f"{owner}-{slug}".lower()
        app_name = slug
        for project in self.all_projects():
            if project.get("name") != project_name:
                continue
            for environment in project.get("environments", []):
                for application in environment.get("applications", []):
                    if application.get("appName") == app_name or application.get("name") == app_name:
                        return str(project["projectId"]), str(environment["environmentId"]), str(application["applicationId"])
            environment = next(iter(project.get("environments", [])), None)
            if environment:
                return self._create_application(str(project["projectId"]), str(environment["environmentId"]), app_name, image_ref)

        created = self.request("POST", "project.create", {"name": project_name, "description": f"Managed project {owner}/{slug}"})
        project = created.get("project", created)
        environment = created.get("environment")
        if not environment:
            project_id = str(project["projectId"])
            refreshed = next(item for item in self.all_projects() if str(item.get("projectId")) == project_id)
            environment = next(iter(refreshed.get("environments", [])), None)
        if not environment:
            raise FactoryError("Dokploy project was created without an environment")
        return self._create_application(str(project["projectId"]), str(environment["environmentId"]), app_name, image_ref)

    def _create_application(self, project_id: str, environment_id: str, app_name: str, image_ref: str) -> tuple[str, str, str]:
        created = self.request(
            "POST", "application.create",
            {"name": app_name, "appName": app_name, "environmentId": environment_id},
        )
        application_id = str(created.get("applicationId") or created.get("application", {}).get("applicationId"))
        if not application_id or application_id == "None":
            raise FactoryError("Dokploy did not return an applicationId")
        self.set_image(application_id, image_ref)
        return project_id, environment_id, application_id

    def set_image(self, application_id: str, image_ref: str) -> None:
        self.request(
            "POST", "application.saveDockerProvider",
            {
                "applicationId": application_id,
                "dockerImage": image_ref,
                "registryUrl": "ghcr.io",
                "username": self.config.registry_username,
                "password": read_secret(self.config.registry_token_file, "GHCR"),
            },
        )

    def create_domain(self, application_id: str, slug: str) -> str:
        host = f"{slug}.staging.{self.config.domain_base}"
        try:
            self.request(
                "POST", "domain.create",
                {
                    "host": host,
                    "path": "/",
                    "port": 80,
                    "https": True,
                    "certificateType": "letsencrypt",
                    "applicationId": application_id,
                },
            )
        except FactoryError as exc:
            if "already" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
        return f"https://{host}"

    def deploy(self, application_id: str, image_ref: str) -> None:
        self.set_image(application_id, image_ref)
        self.request(
            "POST", "application.deploy",
            {"applicationId": application_id, "title": "Workflow Factory deploy", "description": image_ref},
        )


def wait_for_url(url: str, timeout: int, poll: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "workflow-factory/0.1"})
            with urllib.request.urlopen(request, timeout=15) as response:
                if 200 <= response.status < 400:
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(poll)
    raise FactoryError(f"deployment did not become healthy at {url}: {last_error}")


class Factory:
    def __init__(self, config: FactoryConfig):
        self.config = config
        self.state = StateStore(config.state_db)

    def create(
        self, profile: str, name: str, description: str, request_id: str | None = None,
        source: Path | None = None,
    ) -> dict[str, Any]:
        owner = self.config.owner_for(profile)
        slug = slugify(name)
        request_id = request_id or str(uuid.uuid4())
        record = self.state.get_or_create(request_id, profile, owner, name, slug, description)
        workspace = source.resolve() if source else self.config.workspaces_root / owner / slug
        if record.get("workspace") and Path(record["workspace"]).resolve() != workspace.resolve():
            raise FactoryError("request-id already belongs to a different source workspace")
        try:
            if not stage_at_least(record["stage"], "WORKSPACE_CREATED"):
                if source:
                    prepare_existing_project(workspace, self.config.allowed_source_root)
                else:
                    scaffold_static_site(workspace, name, description)
                record = self.state.update(request_id, "WORKSPACE_CREATED", workspace=str(workspace), error=None)

            github = GitHub(self.config)
            if not stage_at_least(record["stage"], "REPO_CREATED"):
                repository, head_sha = github.ensure_private_repo(owner, slug, workspace)
                record = self.state.update(request_id, "REPO_CREATED", repository=repository, head_sha=head_sha)

            if not stage_at_least(record["stage"], "CI_GREEN"):
                artifact_dir = workspace / ".factory-artifacts" / record["head_sha"]
                image_ref, run_url = github.wait_for_image(
                    record["repository"], record["head_sha"], artifact_dir,
                    self.config.ci_timeout_seconds, self.config.poll_seconds,
                )
                record = self.state.update(request_id, "CI_GREEN", image_ref=image_ref, run_url=run_url)

            dokploy = Dokploy(self.config)
            if not stage_at_least(record["stage"], "DOKPLOY_CONFIGURED"):
                project_id, environment_id, application_id = dokploy.ensure_application(owner, slug, record["image_ref"])
                url = dokploy.create_domain(application_id, slug)
                record = self.state.update(
                    request_id, "DOKPLOY_CONFIGURED", dokploy_project_id=project_id,
                    environment_id=environment_id, application_id=application_id, url=url,
                )

            if not stage_at_least(record["stage"], "DEPLOYED"):
                dokploy.deploy(record["application_id"], record["image_ref"])
                record = self.state.update(request_id, "DEPLOYED")

            if not stage_at_least(record["stage"], "READY"):
                wait_for_url(record["url"], self.config.deploy_timeout_seconds, self.config.poll_seconds)
                record = self.state.update(request_id, "READY")
            return record
        except Exception as exc:
            self.state.update(request_id, error=str(exc))
            raise


def stable_request_id(profile: str, name: str, description: str, source: str | None = None) -> str:
    material = json.dumps([profile, name, description, source], ensure_ascii=False, separators=(",", ":"))
    return str(uuid.UUID(hashlib.md5(material.encode("utf-8"), usedforsecurity=False).hexdigest()))
