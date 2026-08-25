"""Read-only proof that a Git/GitHub delivery really completed.

This module is intentionally a *verifier*, not a delivery engine.  It never
stages, commits, fetches, pushes, merges, deletes branches, or prunes
worktrees.  :func:`verify_git_delivery` proves the completed remote workflow;
the explicit :func:`verify_and_persist_git_delivery` adapter only seals a
successful receipt in the caller's existing Kanban SQLite connection.

The boundary is fail-closed: every missing or contradictory fact is returned
as a structured error code.  A successful receipt binds the canonical
worktree, local and remote object IDs, merged PR, required checks, declared
artifacts, and a deterministic digest suitable for a final TOCTOU recheck.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import unquote, urlsplit, urlunsplit

from hermes_cli._subprocess_compat import (
    noninteractive_git_env,
    windows_hidden_popen_kwargs,
)


_HEX_OID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SCP_REMOTE = re.compile(r"^(?P<user>[^@/:]+)@(?P<host>[^:]+):(?P<path>.+)$")
_SECRET_TOKEN = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b"
)
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:access_token|auth|password|token)=)[^&#\s]+")
_RECEIPT_SCHEMA_VERSION = 2
_HEAD_REMOTE_PRESENT = "present"
_HEAD_REMOTE_DELETED_AFTER_MERGE = "deleted_after_merge"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "verified_at",
        "canonical_worktree",
        "branch",
        "head_sha",
        "head_remote_sha",
        "head_remote_disposition",
        "base_remote_sha",
        "merge_sha",
        "pr_number",
        "pr_url",
        "head_repository",
        "base_repository",
        "base_branch",
        "checks",
        "artifacts",
        "candidate_digest",
        "remote_proof_digest",
    }
)


class GitDeliveryErrorCode(str, Enum):
    """Stable machine-readable verifier outcomes."""

    OK = "ok"
    CONFIG_INVALID = "config_invalid"
    COMMAND_NOT_FOUND = "command_not_found"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_FAILED = "command_failed"
    NOT_A_REPOSITORY = "not_a_repository"
    NONCANONICAL_WORKTREE = "noncanonical_worktree"
    DETACHED_HEAD = "detached_head"
    WRONG_BRANCH = "wrong_branch"
    DIRTY_WORKTREE = "dirty_worktree"
    WRONG_REMOTE = "wrong_remote"
    REMOTE_BRANCH_MISSING = "remote_branch_missing"
    REMOTE_SHA_MISMATCH = "remote_sha_mismatch"
    INVALID_ARTIFACT = "invalid_artifact"
    ARTIFACT_UNSUPPORTED = "artifact_unsupported"
    PR_QUERY_FAILED = "pr_query_failed"
    PR_NOT_FOUND = "pr_not_found"
    PR_WRONG_NUMBER = "pr_wrong_number"
    PR_WRONG_HEAD_REPOSITORY = "pr_wrong_head_repository"
    PR_WRONG_HEAD_BRANCH = "pr_wrong_head_branch"
    PR_WRONG_HEAD_OID = "pr_wrong_head_oid"
    PR_WRONG_BASE_REPOSITORY = "pr_wrong_base_repository"
    PR_WRONG_BASE_BRANCH = "pr_wrong_base_branch"
    PR_DRAFT = "pr_draft"
    PR_NOT_MERGED = "pr_not_merged"
    PR_FILES_INCOMPLETE = "pr_files_incomplete"
    PR_ARTIFACT_MISMATCH = "pr_artifact_mismatch"
    REQUIRED_CHECK_MISSING = "required_check_missing"
    REQUIRED_CHECK_NOT_GREEN = "required_check_not_green"
    MERGE_OBJECT_MISSING = "merge_object_missing"
    MERGE_NOT_ANCESTOR = "merge_not_ancestor"
    HEAD_NOT_ANCESTOR = "head_not_ancestor"
    CANDIDATE_CHANGED = "candidate_changed"
    RECEIPT_INVALID = "receipt_invalid"
    RECEIPT_PERSIST_FAILED = "receipt_persist_failed"


@dataclass(frozen=True)
class GitDeliveryConfig:
    """Expected identity of one delivery candidate.

    ``expected_*_remote_url`` values are exact remote identities after safe
    normalization (credentials, a trailing ``.git``, and transport spelling
    do not affect the comparison).  Local paths are supported for hermetic
    tests and self-hosted workflows.
    """

    repo_path: Path
    canonical_worktree: Path
    expected_branch: str
    head_remote: str
    expected_head_remote_url: str
    base_remote: str
    expected_base_remote_url: str
    head_repository: str
    base_repository: str
    base_branch: str
    pr_number: int
    required_checks: tuple[str, ...] = ()
    declared_artifacts: tuple[str, ...] = ()
    expected_candidate_digest: str | None = None
    git_executable: str = "git"
    gh_executable: str = "gh"
    github_host: str = "github.com"
    timeout_seconds: float = 20.0
    required_mode: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_path", Path(self.repo_path))
        object.__setattr__(self, "canonical_worktree", Path(self.canonical_worktree))
        object.__setattr__(self, "required_checks", tuple(self.required_checks))
        object.__setattr__(self, "declared_artifacts", tuple(self.declared_artifacts))


@dataclass(frozen=True)
class ArtifactEvidence:
    path: str
    state: str
    sha256: str | None
    size: int | None
    git_oid: str | None = None
    git_mode: str | None = None


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    state: str


@dataclass(frozen=True)
class GitDeliveryReceipt:
    """Immutable facts proven by a successful verification."""

    schema_version: int
    verified_at: str
    canonical_worktree: str
    branch: str
    head_sha: str
    head_remote_sha: str | None
    head_remote_disposition: str
    base_remote_sha: str
    merge_sha: str
    pr_number: int
    pr_url: str
    head_repository: str
    base_repository: str
    base_branch: str
    checks: tuple[CheckEvidence, ...]
    artifacts: tuple[ArtifactEvidence, ...]
    candidate_digest: str
    remote_proof_digest: str


@dataclass(frozen=True)
class GitDeliveryResult:
    ok: bool
    code: GitDeliveryErrorCode
    message: str
    receipt: GitDeliveryReceipt | None = None
    candidate_digest: str | None = None
    artifacts: tuple[ArtifactEvidence, ...] = ()
    details: tuple[tuple[str, str], ...] = field(default_factory=tuple)


GitHubQuery = Callable[[GitDeliveryConfig], Mapping[str, Any]]


class _VerificationFailure(RuntimeError):
    def __init__(
        self,
        code: GitDeliveryErrorCode,
        message: str,
        **details: object,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = tuple(
            sorted((str(key), _redact(str(value))) for key, value in details.items())
        )


def _redact(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1***@", value)
    value = _QUERY_SECRET.sub(r"\1***", value)
    return _SECRET_TOKEN.sub("***", value)


def _failure(exc: _VerificationFailure) -> GitDeliveryResult:
    return GitDeliveryResult(
        ok=False,
        code=exc.code,
        message=_redact(exc.message),
        details=exc.details,
    )


def build_git_delivery_config_from_contract(
    *,
    policy: Mapping[str, Any],
    request: Mapping[str, Any],
    repo_path: str | Path,
    canonical_worktree: str | Path,
    branch: str,
) -> GitDeliveryConfig:
    """Resolve one task config from sealed generic policy + PR manifest.

    Project names, repositories, remotes and checks come exclusively from the
    board policy. The per-card request contributes only the authoritative PR
    identity and declared artifact paths. Invalid or incomplete contracts
    raise an actionable ``ValueError`` before any GitHub query occurs.
    """

    if policy.get("required") is not True:
        raise ValueError("git_delivery policy is not marked required=true")
    required_policy_fields = (
        "head_remote",
        "expected_head_remote_url",
        "base_remote",
        "expected_base_remote_url",
        "head_repository",
        "base_repository",
        "base_branch",
    )
    missing = [
        field_name
        for field_name in required_policy_fields
        if not str(policy.get(field_name) or "").strip()
    ]
    checks_raw = policy.get("required_checks")
    if not isinstance(checks_raw, (list, tuple)) or not checks_raw:
        missing.append("required_checks")
    if missing:
        raise ValueError(
            "git_delivery policy is incomplete; missing: " + ", ".join(missing)
        )

    pull_request = request.get("pull_request")
    pr_number: int
    if isinstance(pull_request, int) and not isinstance(pull_request, bool):
        pr_number = pull_request
    else:
        raw_pr = str(pull_request or "").strip()
        if raw_pr.isdigit():
            pr_number = int(raw_pr)
        else:
            parsed = urlsplit(raw_pr)
            host = str(policy.get("github_host") or "github.com").casefold()
            parts = [part for part in parsed.path.split("/") if part]
            if (
                parsed.scheme not in {"http", "https"}
                or (parsed.hostname or "").casefold() != host
                or len(parts) != 4
                or parts[2] != "pull"
                or not parts[3].isdigit()
            ):
                raise ValueError(
                    "pull_request must be a positive PR number or canonical PR URL "
                    f"on {host}"
                )
            url_repository = f"{parts[0]}/{parts[1]}".casefold()
            if url_repository != str(policy.get("base_repository")).casefold():
                raise ValueError(
                    "pull_request repository does not match git_delivery.base_repository"
                )
            pr_number = int(parts[3])
    if pr_number <= 0:
        raise ValueError("pull_request must identify a positive PR number")

    artifacts_raw = request.get("declared_artifacts")
    if not isinstance(artifacts_raw, (list, tuple)) or not artifacts_raw:
        raise ValueError("declared_artifacts must be a non-empty list of paths")
    try:
        artifacts = _declared_artifacts(tuple(str(path) for path in artifacts_raw))
    except _VerificationFailure as exc:
        raise ValueError(f"invalid declared_artifacts: {exc}") from exc

    timeout_raw = policy.get("timeout_seconds", 20.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("git_delivery.timeout_seconds must be numeric") from exc
    if not 1.0 <= timeout <= 120.0:
        raise ValueError("git_delivery.timeout_seconds must be between 1 and 120")

    config = GitDeliveryConfig(
        repo_path=Path(repo_path),
        canonical_worktree=Path(canonical_worktree),
        expected_branch=str(branch or ""),
        head_remote=str(policy["head_remote"]),
        expected_head_remote_url=str(policy["expected_head_remote_url"]),
        base_remote=str(policy["base_remote"]),
        expected_base_remote_url=str(policy["expected_base_remote_url"]),
        head_repository=str(policy["head_repository"]),
        base_repository=str(policy["base_repository"]),
        base_branch=str(policy["base_branch"]),
        pr_number=pr_number,
        required_checks=tuple(str(check) for check in checks_raw),
        declared_artifacts=artifacts,
        github_host=str(policy.get("github_host") or "github.com"),
        timeout_seconds=timeout,
        required_mode=True,
    )
    try:
        _validate_config(config)
        _validate_git_refs(config)
    except _VerificationFailure as exc:
        raise ValueError(f"{exc.code.value}: {exc}") from exc
    return config


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = noninteractive_git_env(env)
    command_env.update({
        "GH_PROMPT_DISABLED": "1",
        "GH_PAGER": "cat",
        "PAGER": "cat",
        "NO_COLOR": "1",
    })
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=command_env,
            **windows_hidden_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_NOT_FOUND,
            f"Required executable is unavailable: {argv[0]}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_TIMEOUT,
            f"Read-only command timed out: {Path(str(argv[0])).name}",
        ) from exc
    except OSError as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            f"Could not start read-only command: {_redact(str(exc))}",
        ) from exc
    if completed.returncode not in allowed_returncodes:
        stderr = _redact((completed.stderr or "").strip())
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            f"Read-only command failed ({completed.returncode})",
            command=Path(str(argv[0])).name,
            stderr=stderr[-1000:],
        )
    return completed


def _run_bytes(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a read-only command without decoding its authoritative bytes."""

    command_env = noninteractive_git_env(env)
    command_env.update({
        "GH_PROMPT_DISABLED": "1",
        "GH_PAGER": "cat",
        "PAGER": "cat",
        "NO_COLOR": "1",
    })
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=command_env,
            **windows_hidden_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_NOT_FOUND,
            f"Required executable is unavailable: {argv[0]}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_TIMEOUT,
            f"Read-only command timed out: {Path(str(argv[0])).name}",
        ) from exc
    except OSError as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            f"Could not start read-only command: {_redact(str(exc))}",
        ) from exc
    if completed.returncode not in allowed_returncodes:
        stderr = _redact((completed.stderr or b"").decode("utf-8", "replace").strip())
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            f"Read-only command failed ({completed.returncode})",
            command=Path(str(argv[0])).name,
            stderr=stderr[-1000:],
        )
    return completed


def _git(
    config: GitDeliveryConfig,
    *args: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    return _run(
        (config.git_executable, *args),
        cwd=config.repo_path,
        timeout=config.timeout_seconds,
        allowed_returncodes=allowed_returncodes,
    )


def _git_bytes(
    config: GitDeliveryConfig,
    *args: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    return _run_bytes(
        (config.git_executable, *args),
        cwd=config.repo_path,
        timeout=config.timeout_seconds,
        allowed_returncodes=allowed_returncodes,
    )


def _validate_repository_slug(value: str, label: str) -> tuple[str, str]:
    parts = value.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            f"{label} must be owner/name",
        )
    return parts[0], parts[1]


def _validate_config(config: GitDeliveryConfig) -> None:
    scalar_values = {
        "expected_branch": config.expected_branch,
        "head_remote": config.head_remote,
        "expected_head_remote_url": config.expected_head_remote_url,
        "base_remote": config.base_remote,
        "expected_base_remote_url": config.expected_base_remote_url,
        "base_branch": config.base_branch,
        "git_executable": config.git_executable,
        "gh_executable": config.gh_executable,
        "github_host": config.github_host,
    }
    missing = [name for name, value in scalar_values.items() if not str(value).strip()]
    if missing:
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            "Required delivery configuration is empty",
            fields=",".join(sorted(missing)),
        )
    _validate_repository_slug(config.head_repository, "head_repository")
    _validate_repository_slug(config.base_repository, "base_repository")
    if config.pr_number <= 0 or config.timeout_seconds <= 0:
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            "PR number and timeout must be positive",
        )
    checks = [item.strip() for item in config.required_checks]
    if any(not item for item in checks) or len(set(checks)) != len(checks):
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            "Required check names must be non-empty and unique",
        )
    if config.required_mode and not checks:
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            "Required delivery mode must declare at least one required check",
        )
    for label, value in (
        ("expected_branch", config.expected_branch),
        ("base_branch", config.base_branch),
        ("head_remote", config.head_remote),
        ("base_remote", config.base_remote),
    ):
        if value.startswith("-"):
            raise _VerificationFailure(
                GitDeliveryErrorCode.CONFIG_INVALID,
                f"{label} cannot begin with a dash",
            )
    if config.expected_candidate_digest is not None and not re.fullmatch(
        r"[0-9a-f]{64}", config.expected_candidate_digest
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.CONFIG_INVALID,
            "expected_candidate_digest must be a lowercase SHA-256 digest",
        )


def _validate_git_refs(config: GitDeliveryConfig) -> None:
    """Reject unsafe or syntactically invalid branch and remote ref inputs."""

    for label, branch in (
        ("expected_branch", config.expected_branch),
        ("base_branch", config.base_branch),
    ):
        result = _git(
            config,
            "check-ref-format",
            "--branch",
            branch,
            allowed_returncodes=frozenset({0, 1, 128}),
        )
        if result.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.CONFIG_INVALID,
                f"{label} is not a valid Git branch name",
                value=branch,
            )
    for label, remote in (
        ("head_remote", config.head_remote),
        ("base_remote", config.base_remote),
    ):
        result = _git(
            config,
            "check-ref-format",
            f"refs/remotes/{remote}/hermes-verifier-probe",
            allowed_returncodes=frozenset({0, 1, 128}),
        )
        if result.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.CONFIG_INVALID,
                f"{label} is not safe for a remote-tracking ref",
                value=remote,
            )


def _canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Configured worktree does not resolve to an existing path",
            path=path,
        ) from exc


def _path_key(path: Path) -> str:
    value = str(path)
    return os.path.normcase(value) if os.name == "nt" else value


def task_owns_delivery_branch(task_id: str, branch: str) -> bool:
    """Recognize the two Hermes task-id branch conventions."""

    leaf = str(branch or "").rsplit("/", 1)[-1]
    return leaf == task_id or leaf.startswith(f"{task_id}-")


def _verify_linked_worktree_registration(
    config: GitDeliveryConfig,
    canonical: Path,
    branch: str,
) -> None:
    """Prove this is a registered Hermes worktree, never the main checkout."""

    common_raw = _git(
        config,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ).stdout.strip()
    common = _canonical_path(Path(common_raw))
    if common.name.casefold() != ".git":
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Delivery verification requires a linked worktree of a normal repository",
        )
    main_checkout = common.parent
    if _path_key(main_checkout) == _path_key(canonical):
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Delivery verification refuses the repository's main checkout",
        )
    if canonical.parent.name.casefold() != ".worktrees":
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Delivery worktree is outside the managed .worktrees directory",
        )

    listing = _git(config, "worktree", "list", "--porcelain", "-z").stdout
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in listing.split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        records.append(current)

    expected_ref = f"refs/heads/{branch}"
    for record in records:
        raw_path = record.get("worktree")
        if not raw_path:
            continue
        registered = _canonical_path(Path(raw_path))
        if _path_key(registered) == _path_key(canonical):
            if record.get("branch") != expected_ref:
                raise _VerificationFailure(
                    GitDeliveryErrorCode.WRONG_BRANCH,
                    "Registered worktree branch differs from the delivery branch",
                    actual=record.get("branch") or "detached",
                    expected=expected_ref,
                )
            return
    raise _VerificationFailure(
        GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
        "Configured path is not an exact registered linked worktree",
    )


def _verify_worktree(
    config: GitDeliveryConfig, *, require_clean: bool
) -> tuple[Path, str, str]:
    configured = _canonical_path(config.repo_path)
    canonical = _canonical_path(config.canonical_worktree)
    if _path_key(configured) != _path_key(canonical):
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Verification was not run in the configured canonical worktree",
            actual=configured,
            expected=canonical,
        )
    try:
        root_raw = _git(config, "rev-parse", "--show-toplevel").stdout.strip()
    except _VerificationFailure as exc:
        if exc.code is GitDeliveryErrorCode.COMMAND_FAILED:
            raise _VerificationFailure(
                GitDeliveryErrorCode.NOT_A_REPOSITORY,
                "Configured worktree is not a Git repository",
            ) from exc
        raise
    root = _canonical_path(Path(root_raw))
    if _path_key(root) != _path_key(canonical):
        raise _VerificationFailure(
            GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
            "Git top-level differs from the configured canonical worktree",
            actual=root,
            expected=canonical,
        )
    symbolic = _git(
        config,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allowed_returncodes=frozenset({0, 1}),
    )
    if symbolic.returncode != 0 or not symbolic.stdout.strip():
        raise _VerificationFailure(
            GitDeliveryErrorCode.DETACHED_HEAD,
            "Delivery verification refuses a detached HEAD",
        )
    branch = symbolic.stdout.strip()
    if branch != config.expected_branch:
        raise _VerificationFailure(
            GitDeliveryErrorCode.WRONG_BRANCH,
            "Current branch is not the declared delivery branch",
            actual=branch,
            expected=config.expected_branch,
        )
    _verify_linked_worktree_registration(config, canonical, branch)
    head_sha = (
        _git(config, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip().lower()
    )
    if not _HEX_OID.fullmatch(head_sha):
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            "Git returned an invalid HEAD object ID",
        )
    status = _git(config, "status", "--porcelain=v2", "--untracked-files=all").stdout
    if require_clean and status:
        raise _VerificationFailure(
            GitDeliveryErrorCode.DIRTY_WORKTREE,
            "Canonical worktree has tracked or untracked changes",
        )
    return canonical, branch, head_sha


def _normalize_remote(value: str, *, repo: Path) -> str:
    raw = value.strip()
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        local = Path(raw)
        try:
            resolved = local.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = local.absolute()
        return f"path:{_path_key(resolved)}"
    scp = _SCP_REMOTE.fullmatch(raw)
    if scp:
        host = scp.group("host").lower()
        path = scp.group("path").strip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        return f"host:{host}/{path.lower()}"
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.scheme != "file":
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
        path = unquote(parsed.path).strip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        return f"host:{host}{port}/{path.lower()}"
    if parsed.scheme == "file":
        local_value = unquote(parsed.path)
    else:
        local_value = raw
    local = Path(local_value)
    if not local.is_absolute():
        local = repo / local
    try:
        resolved = local.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = local.absolute()
    return f"path:{_path_key(resolved)}"


def _verify_remote_url(
    config: GitDeliveryConfig,
    remote: str,
    expected_url: str,
) -> None:
    actual = _git(config, "remote", "get-url", remote).stdout.strip()
    if _normalize_remote(actual, repo=config.repo_path) != _normalize_remote(
        expected_url, repo=config.repo_path
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.WRONG_REMOTE,
            "Git remote does not match the declared repository",
            remote=remote,
            actual=_redact(actual),
            expected=_redact(expected_url),
        )


def _remote_branch_sha(
    config: GitDeliveryConfig,
    remote: str,
    branch: str,
    *,
    allow_missing: bool = False,
) -> str | None:
    result = _git(
        config,
        "ls-remote",
        "--exit-code",
        remote,
        f"refs/heads/{branch}",
        allowed_returncodes=frozenset({0, 2}),
    )
    if result.returncode != 0 or not result.stdout.strip():
        if allow_missing:
            return None
        raise _VerificationFailure(
            GitDeliveryErrorCode.REMOTE_BRANCH_MISSING,
            "Declared remote branch does not exist",
            remote=remote,
            branch=branch,
        )
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or not _HEX_OID.fullmatch(rows[0][0]):
        raise _VerificationFailure(
            GitDeliveryErrorCode.COMMAND_FAILED,
            "Remote branch query returned ambiguous output",
            remote=remote,
            branch=branch,
        )
    return rows[0][0].lower()


def _normalize_artifact(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise _VerificationFailure(
            GitDeliveryErrorCode.INVALID_ARTIFACT,
            "Declared artifact path is empty",
        )
    raw = path.strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or raw.startswith("//"):
        raise _VerificationFailure(
            GitDeliveryErrorCode.INVALID_ARTIFACT,
            "Declared artifact must be repository-relative",
            path=path,
        )
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise _VerificationFailure(
            GitDeliveryErrorCode.INVALID_ARTIFACT,
            "Declared artifact contains an unsafe path segment",
            path=path,
        )
    if parts[0].lower() == ".git":
        raise _VerificationFailure(
            GitDeliveryErrorCode.INVALID_ARTIFACT,
            "Git administration paths cannot be delivery artifacts",
            path=path,
        )
    return pure.as_posix()


def _declared_artifacts(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(_normalize_artifact(item) for item in values)
    if len(set(normalized)) != len(normalized):
        raise _VerificationFailure(
            GitDeliveryErrorCode.INVALID_ARTIFACT,
            "Declared artifact paths must be unique",
        )
    return tuple(sorted(normalized))


def _split_nul(output: str) -> set[str]:
    return {item for item in output.split("\0") if item}


def _tree_artifacts(
    config: GitDeliveryConfig,
    treeish: str,
    declared: tuple[str, ...],
) -> tuple[ArtifactEvidence, ...]:
    evidence: list[ArtifactEvidence] = []
    for relative in declared:
        result = _git(
            config,
            "ls-tree",
            "-z",
            treeish,
            "--",
            relative,
        ).stdout
        rows = [row for row in result.split("\0") if row]
        if not rows:
            evidence.append(
                ArtifactEvidence(
                    path=relative,
                    state="deleted",
                    sha256=None,
                    size=None,
                    git_oid=None,
                    git_mode=None,
                )
            )
            continue
        if len(rows) != 1 or "\t" not in rows[0]:
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
                "Declared artifact is ambiguous in a proven Git tree",
                path=relative,
                treeish=treeish,
            )
        metadata, tree_path = rows[0].split("\t", 1)
        fields = metadata.split()
        if (
            tree_path != relative
            or len(fields) != 3
            or fields[0] not in {"100644", "100755"}
            or fields[1] != "blob"
            or not _HEX_OID.fullmatch(fields[2])
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.ARTIFACT_UNSUPPORTED,
                "Declared artifact is not a regular blob in a proven Git tree",
                path=relative,
                treeish=treeish,
            )
        oid = fields[2].lower()
        payload = _git_bytes(config, "cat-file", "blob", oid).stdout
        evidence.append(
            ArtifactEvidence(
                path=relative,
                state="committed",
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
                git_oid=oid,
                git_mode=fields[0],
            )
        )
    return tuple(evidence)


def _first_parent(config: GitDeliveryConfig, commit: str) -> str:
    line = _git(config, "rev-list", "--parents", "-n", "1", commit).stdout.strip()
    parts = line.split()
    if len(parts) < 2 or parts[0].lower() != commit.lower():
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
            "Merged PR object has no usable first parent",
            merge_sha=commit,
        )
    parent = parts[1].lower()
    if not _HEX_OID.fullmatch(parent):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
            "Merged PR first parent is not a valid commit object ID",
        )
    return parent


def _merge_changed_paths(
    config: GitDeliveryConfig,
    *,
    merge_sha: str,
) -> tuple[str, ...]:
    parent = _first_parent(config, merge_sha)
    changed = _split_nul(
        _git(
            config,
            "diff-tree",
            "--no-commit-id",
            "-r",
            "--no-renames",
            "--name-only",
            "-z",
            parent,
            merge_sha,
            "--",
        ).stdout
    )
    return tuple(sorted(_normalize_artifact(path) for path in changed))


def _merge_rename_sources(
    config: GitDeliveryConfig,
    *,
    merge_sha: str,
) -> dict[str, str]:
    parent = _first_parent(config, merge_sha)
    raw = _git(
        config,
        "diff-tree",
        "--no-commit-id",
        "-r",
        "--find-renames",
        "--name-status",
        "-z",
        parent,
        merge_sha,
        "--",
    ).stdout
    fields = [field for field in raw.split("\0") if field]
    renames: dict[str, str] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
                "Merged PR rename metadata is malformed",
            )
        paths = fields[index : index + path_count]
        index += path_count
        if status.startswith("R"):
            old_path = _normalize_artifact(paths[0])
            new_path = _normalize_artifact(paths[1])
            renames[new_path] = old_path
    return renames


def _verify_artifact_trees(
    config: GitDeliveryConfig,
    *,
    pr: Mapping[str, Any],
    head_sha: str,
    merge_sha: str,
    declared: tuple[str, ...],
) -> tuple[ArtifactEvidence, ...]:
    files = pr.get("files")
    if not isinstance(files, Mapping):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_FILES_INCOMPLETE,
            "PR file list is missing",
        )
    file_page_info = files.get("pageInfo")
    if (
        not isinstance(file_page_info, Mapping)
        or file_page_info.get("hasNextPage") is not False
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_FILES_INCOMPLETE,
            "PR file list exceeds the bounded query",
        )
    nodes = files.get("nodes")
    if not isinstance(nodes, list):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_FILES_INCOMPLETE,
            "PR file list is malformed",
        )
    renames = _merge_rename_sources(config, merge_sha=merge_sha)
    expanded_pr_paths: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_FILES_INCOMPLETE,
                "PR file list contains a malformed entry",
            )
        path = _normalize_artifact(str(node.get("path") or ""))
        expanded_pr_paths.append(path)
        if str(node.get("changeType") or "").upper() == "RENAMED":
            old_path = renames.get(path)
            if old_path is None:
                raise _VerificationFailure(
                    GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
                    "PR reports a rename that the merged tree cannot prove",
                    path=path,
                )
            expanded_pr_paths.append(old_path)
    pr_paths = tuple(sorted(expanded_pr_paths))
    merge_paths = _merge_changed_paths(config, merge_sha=merge_sha)
    if pr_paths != declared or merge_paths != declared:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
            "Declared artifacts do not exactly match both PR and merged-tree paths",
            declared=",".join(declared),
            pr_files=",".join(pr_paths),
            merge_files=",".join(merge_paths),
        )

    head_artifacts = _tree_artifacts(config, head_sha, declared)
    merge_artifacts = _tree_artifacts(config, merge_sha, declared)
    for head_artifact, merge_artifact in zip(
        head_artifacts, merge_artifacts, strict=True
    ):
        if (
            head_artifact.state != merge_artifact.state
            or head_artifact.git_oid != merge_artifact.git_oid
            or head_artifact.git_mode != merge_artifact.git_mode
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH,
                "Artifact state differs between canonical HEAD and merged PR tree",
                path=head_artifact.path,
                head_state=head_artifact.state,
                head_oid=head_artifact.git_oid,
                head_mode=head_artifact.git_mode,
                merge_state=merge_artifact.state,
                merge_oid=merge_artifact.git_oid,
                merge_mode=merge_artifact.git_mode,
            )
    return head_artifacts


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remote_proof_digest(
    *,
    candidate_digest: str,
    verified_at: str,
    head_sha: str,
    head_remote_sha: str | None,
    head_remote_disposition: str,
    base_remote_sha: str,
    merge_sha: str,
) -> str:
    """Bind mutable remote evidence without destabilizing candidate identity."""

    return _digest(
        {
            "schema": 1,
            "candidate_digest": candidate_digest,
            "verified_at": verified_at,
            "head_sha": head_sha,
            "head_remote_sha": head_remote_sha,
            "head_remote_disposition": head_remote_disposition,
            "base_remote_sha": base_remote_sha,
            "merge_sha": merge_sha,
        }
    )


_PR_GRAPHQL = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    nameWithOwner
    pullRequest(number:$number){
      number url state isDraft merged mergedAt headRefName headRefOid baseRefName
      headRepository{nameWithOwner}
      baseRepository{nameWithOwner}
      mergeCommit{oid}
      files(first:100){nodes{path changeType} pageInfo{hasNextPage}}
      commits(last:1){nodes{commit{statusCheckRollup{
        state contexts(first:100){
          pageInfo{hasNextPage}
          nodes{
            __typename
            ... on CheckRun{name status conclusion}
            ... on StatusContext{context state}
          }
        }
      }}}}
    }
  }
}
""".strip()


def query_github_pull_request(config: GitDeliveryConfig) -> Mapping[str, Any]:
    """Read one PR through ``gh api graphql`` with prompting disabled."""

    owner, name = _validate_repository_slug(config.base_repository, "base_repository")
    try:
        result = _run(
            (
                config.gh_executable,
                "api",
                "graphql",
                "--hostname",
                config.github_host,
                "-f",
                f"query={_PR_GRAPHQL}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={config.pr_number}",
            ),
            cwd=config.repo_path,
            timeout=config.timeout_seconds,
        )
    except _VerificationFailure as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR query command failed",
            cause=exc.code.value,
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR query did not return valid JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR query returned an invalid document",
        )
    return payload


def _pull_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("errors"):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR response contains GraphQL errors",
        )
    try:
        repository = payload["data"]["repository"]
        pr = repository["pullRequest"]
    except (KeyError, TypeError) as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR response is missing required fields",
        ) from exc
    if not isinstance(repository, Mapping) or not isinstance(pr, Mapping):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_NOT_FOUND,
            "Declared GitHub pull request was not found",
        )
    return pr


def _repo_name(value: object) -> str:
    if isinstance(value, Mapping):
        raw = cast(Mapping[str, Any], value).get("nameWithOwner")
        return str(raw or "")
    return ""


def _check_evidence(
    pr: Mapping[str, Any], required: Sequence[str]
) -> tuple[CheckEvidence, ...]:
    try:
        rollup = pr["commits"]["nodes"][-1]["commit"]["statusCheckRollup"]
        contexts = rollup["contexts"]
        nodes = contexts["nodes"]
    except (KeyError, TypeError, IndexError) as exc:
        if required:
            raise _VerificationFailure(
                GitDeliveryErrorCode.REQUIRED_CHECK_MISSING,
                "PR has no complete status-check rollup",
            ) from exc
        return ()
    page_info = contexts.get("pageInfo") if isinstance(contexts, Mapping) else None
    if (
        not isinstance(contexts, Mapping)
        or not isinstance(page_info, Mapping)
        or page_info.get("hasNextPage") is not False
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.REQUIRED_CHECK_MISSING,
            "PR status checks exceed the bounded query",
        )
    by_name: dict[str, list[str]] = {}
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, Mapping):
            continue
        if node.get("__typename") == "CheckRun":
            name = str(node.get("name") or "")
            status = str(node.get("status") or "").upper()
            conclusion = str(node.get("conclusion") or "").upper()
            state = (
                "SUCCESS"
                if status == "COMPLETED" and conclusion == "SUCCESS"
                else f"{status}/{conclusion}"
            )
        elif node.get("__typename") == "StatusContext":
            name = str(node.get("context") or "")
            state = str(node.get("state") or "").upper()
        else:
            continue
        if name:
            by_name.setdefault(name, []).append(state)
    evidence: list[CheckEvidence] = []
    for required_name in required:
        states = by_name.get(required_name)
        if not states:
            raise _VerificationFailure(
                GitDeliveryErrorCode.REQUIRED_CHECK_MISSING,
                "A required PR check is absent",
                check=required_name,
            )
        if states != ["SUCCESS"]:
            raise _VerificationFailure(
                GitDeliveryErrorCode.REQUIRED_CHECK_NOT_GREEN,
                "A required PR check is not uniquely green",
                check=required_name,
                states=",".join(states),
            )
        evidence.append(CheckEvidence(required_name, "SUCCESS"))
    return tuple(evidence)


def _verify_pr(
    config: GitDeliveryConfig,
    payload: Mapping[str, Any],
    *,
    head_sha: str,
) -> tuple[Mapping[str, Any], tuple[CheckEvidence, ...], str]:
    try:
        repository = payload["data"]["repository"]
    except (KeyError, TypeError) as exc:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR response is missing the queried repository",
        ) from exc
    if (
        not isinstance(repository, Mapping)
        or _repo_name(repository).casefold() != config.base_repository.casefold()
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_QUERY_FAILED,
            "GitHub PR response repository differs from the delivery contract",
        )
    pr = _pull_request(payload)
    if pr.get("number") != config.pr_number:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_WRONG_NUMBER,
            "PR response number differs from the delivery contract",
        )
    head_repository = _repo_name(pr.get("headRepository"))
    base_repository = _repo_name(pr.get("baseRepository"))
    comparisons = (
        (
            head_repository.casefold() == config.head_repository.casefold(),
            GitDeliveryErrorCode.PR_WRONG_HEAD_REPOSITORY,
            "PR head repository differs from the delivery contract",
        ),
        (
            str(pr.get("headRefName") or "") == config.expected_branch,
            GitDeliveryErrorCode.PR_WRONG_HEAD_BRANCH,
            "PR head branch differs from the delivery contract",
        ),
        (
            str(pr.get("headRefOid") or "").lower() == head_sha,
            GitDeliveryErrorCode.PR_WRONG_HEAD_OID,
            "PR head object ID differs from the canonical worktree",
        ),
        (
            base_repository.casefold() == config.base_repository.casefold(),
            GitDeliveryErrorCode.PR_WRONG_BASE_REPOSITORY,
            "PR base repository differs from the delivery contract",
        ),
        (
            str(pr.get("baseRefName") or "") == config.base_branch,
            GitDeliveryErrorCode.PR_WRONG_BASE_BRANCH,
            "PR base branch differs from the delivery contract",
        ),
    )
    for matched, code, message in comparisons:
        if not matched:
            raise _VerificationFailure(code, message)
    if pr.get("isDraft") is not False:
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_DRAFT,
            "Pull request is draft or draft state is unknown",
        )
    if (
        pr.get("merged") is not True
        or str(pr.get("state") or "").upper() != "MERGED"
        or not pr.get("mergedAt")
    ):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_NOT_MERGED,
            "Pull request is not provably merged",
        )
    merge_sha = str((pr.get("mergeCommit") or {}).get("oid") or "").lower()
    if not _HEX_OID.fullmatch(merge_sha):
        raise _VerificationFailure(
            GitDeliveryErrorCode.PR_NOT_MERGED,
            "Merged pull request has no valid merge object ID",
        )
    return pr, _check_evidence(pr, config.required_checks), merge_sha


def verify_git_delivery(
    config: GitDeliveryConfig,
    *,
    github_query: GitHubQuery | None = None,
) -> GitDeliveryResult:
    """Prove the complete local -> remote branch -> merged PR -> base chain."""

    try:
        _validate_config(config)
        _validate_git_refs(config)
        canonical, branch, head_sha = _verify_worktree(config, require_clean=True)
        declared = _declared_artifacts(config.declared_artifacts)
        _verify_remote_url(config, config.head_remote, config.expected_head_remote_url)
        _verify_remote_url(config, config.base_remote, config.expected_base_remote_url)
        head_remote_sha = _remote_branch_sha(
            config,
            config.head_remote,
            config.expected_branch,
            allow_missing=True,
        )
        if head_remote_sha is not None and head_remote_sha != head_sha:
            raise _VerificationFailure(
                GitDeliveryErrorCode.REMOTE_SHA_MISMATCH,
                "Remote delivery branch is not the exact canonical HEAD",
                local=head_sha,
                remote=head_remote_sha,
            )
        base_remote_sha = _remote_branch_sha(
            config, config.base_remote, config.base_branch
        )
        if base_remote_sha is None:  # allow_missing=False makes this unreachable.
            raise _VerificationFailure(
                GitDeliveryErrorCode.REMOTE_BRANCH_MISSING,
                "Declared base branch does not exist",
            )
        query = github_query or query_github_pull_request
        try:
            payload = query(config)
        except _VerificationFailure:
            raise
        except Exception as exc:
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_QUERY_FAILED,
                f"GitHub PR query failed: {_redact(str(exc))}",
            ) from exc
        if not isinstance(payload, Mapping):
            raise _VerificationFailure(
                GitDeliveryErrorCode.PR_QUERY_FAILED,
                "GitHub PR query returned a non-object payload",
            )
        pr, checks, merge_sha = _verify_pr(config, payload, head_sha=head_sha)
        # A merged PR's immutable headRefOid remains authoritative after the
        # source branch is deleted.  If the branch still exists, it must still
        # resolve to that exact object.
        object_probe = _git(
            config,
            "cat-file",
            "-e",
            f"{merge_sha}^{{commit}}",
            allowed_returncodes=frozenset({0, 1, 128}),
        )
        if object_probe.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.MERGE_OBJECT_MISSING,
                "PR merge object is not available in the local object database",
                merge_sha=merge_sha,
            )
        ancestry = _git(
            config,
            "merge-base",
            "--is-ancestor",
            merge_sha,
            base_remote_sha,
            allowed_returncodes=frozenset({0, 1}),
        )
        if ancestry.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.MERGE_NOT_ANCESTOR,
                "Merged PR object is not an ancestor of the exact base remote SHA",
                merge_sha=merge_sha,
                base_sha=base_remote_sha,
            )
        head_ancestry = _git(
            config,
            "merge-base",
            "--is-ancestor",
            head_sha,
            base_remote_sha,
            allowed_returncodes=frozenset({0, 1}),
        )
        if head_ancestry.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.HEAD_NOT_ANCESTOR,
                "Task HEAD is not an ancestor of the exact base remote SHA",
                head_sha=head_sha,
                base_sha=base_remote_sha,
            )
        artifacts = _verify_artifact_trees(
            config,
            pr=pr,
            head_sha=head_sha,
            merge_sha=merge_sha,
            declared=declared,
        )

        candidate_payload = {
            "schema": 2,
            "canonical_worktree": str(canonical),
            "branch": branch,
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "head_repository": config.head_repository.casefold(),
            "base_repository": config.base_repository.casefold(),
            "base_branch": config.base_branch,
            "pr_number": config.pr_number,
            "pr_url": str(pr.get("url") or ""),
            "checks": [check.__dict__ for check in checks],
            "artifacts": [artifact.__dict__ for artifact in artifacts],
        }
        candidate_digest = _digest(candidate_payload)
        if (
            config.expected_candidate_digest is not None
            and candidate_digest != config.expected_candidate_digest
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.CANDIDATE_CHANGED,
                "Delivery candidate digest changed since the caller's prior proof",
                expected=config.expected_candidate_digest,
                actual=candidate_digest,
            )

        # Final cheap re-read closes the common local/remote TOCTOU window.
        _, final_branch, final_head = _verify_worktree(config, require_clean=True)
        final_head_remote = _remote_branch_sha(
            config,
            config.head_remote,
            config.expected_branch,
            allow_missing=True,
        )
        final_base_remote = _remote_branch_sha(
            config, config.base_remote, config.base_branch
        )
        if (
            final_branch != branch
            or final_head != head_sha
            or final_head_remote not in {None, head_sha}
            or final_base_remote != base_remote_sha
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.CANDIDATE_CHANGED,
                "Delivery candidate changed during verification",
            )

        receipt_head_remote_sha = final_head_remote
        head_remote_disposition = (
            _HEAD_REMOTE_PRESENT
            if receipt_head_remote_sha is not None
            else _HEAD_REMOTE_DELETED_AFTER_MERGE
        )
        verified_at = datetime.now(timezone.utc).isoformat()
        remote_proof_digest = _remote_proof_digest(
            candidate_digest=candidate_digest,
            verified_at=verified_at,
            head_sha=head_sha,
            head_remote_sha=receipt_head_remote_sha,
            head_remote_disposition=head_remote_disposition,
            base_remote_sha=base_remote_sha,
            merge_sha=merge_sha,
        )
        receipt = GitDeliveryReceipt(
            schema_version=_RECEIPT_SCHEMA_VERSION,
            verified_at=verified_at,
            canonical_worktree=str(canonical),
            branch=branch,
            head_sha=head_sha,
            head_remote_sha=receipt_head_remote_sha,
            head_remote_disposition=head_remote_disposition,
            base_remote_sha=base_remote_sha,
            merge_sha=merge_sha,
            pr_number=config.pr_number,
            pr_url=str(pr.get("url") or ""),
            head_repository=config.head_repository,
            base_repository=config.base_repository,
            base_branch=config.base_branch,
            checks=checks,
            artifacts=artifacts,
            candidate_digest=candidate_digest,
            remote_proof_digest=remote_proof_digest,
        )
        return GitDeliveryResult(
            ok=True,
            code=GitDeliveryErrorCode.OK,
            message="Git delivery is proven from canonical worktree through merged base",
            receipt=receipt,
            candidate_digest=candidate_digest,
            artifacts=artifacts,
        )
    except _VerificationFailure as exc:
        return _failure(exc)
    except Exception as exc:  # fail closed at the public boundary
        return _failure(
            _VerificationFailure(
                GitDeliveryErrorCode.COMMAND_FAILED,
                f"Unexpected Git-delivery verification failure: {_redact(str(exc))}",
            )
        )


def verify_and_persist_git_delivery(
    conn: Any,
    task_id: str,
    config: GitDeliveryConfig | None = None,
    *,
    github_query: GitHubQuery | None = None,
) -> GitDeliveryResult:
    """Verify one required delivery and durably seal it on the same card.

    Required mode is forced here: an empty required-check contract fails before
    any receipt can be written. The underlying verifier stays authoritative for
    PR identity, merge state, checks, artifacts and ancestry; this adapter adds
    only the idempotent SQLite checkpoint consumed by ``complete_task``.
    """

    try:
        from hermes_cli import kanban_db

        task = kanban_db.get_task(conn, task_id)
        contract = kanban_db.get_git_delivery_contract(conn, task_id)
        if task is None or contract is None or not contract["required"]:
            raise ValueError("task has no required sealed Git delivery contract")
        policy = contract["policy"]
        request = contract["request"]
        if not isinstance(policy, Mapping) or not isinstance(request, Mapping):
            raise ValueError("sealed Git delivery policy or PR manifest is missing")
        sealed_config = build_git_delivery_config_from_contract(
            policy=policy,
            request=request,
            repo_path=str(task.workspace_path or ""),
            canonical_worktree=str(task.workspace_path or ""),
            branch=str(task.branch_name or ""),
        )
        task_path = Path(str(task.workspace_path or "")).expanduser().resolve(
            strict=False
        )
        if (
            task_path.parent.name.casefold() != ".worktrees"
            or task_path.name != task_id
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.NONCANONICAL_WORKTREE,
                "Task worktree is outside its exact .worktrees/<task-id> owner path",
            )
        if not task_owns_delivery_branch(task_id, str(task.branch_name or "")):
            raise _VerificationFailure(
                GitDeliveryErrorCode.WRONG_BRANCH,
                "Task branch is not owned by this task id",
                task_id=task_id,
                branch=str(task.branch_name or ""),
            )
        comparable = (
            "repo_path",
            "canonical_worktree",
            "expected_branch",
            "head_remote",
            "expected_head_remote_url",
            "base_remote",
            "expected_base_remote_url",
            "head_repository",
            "base_repository",
            "base_branch",
            "pr_number",
            "required_checks",
            "declared_artifacts",
            "github_host",
        )
        if config is not None:
            mismatched = [
                field_name
                for field_name in comparable
                if getattr(config, field_name) != getattr(sealed_config, field_name)
            ]
            if mismatched:
                raise ValueError(
                    "caller config differs from sealed task contract: "
                    + ", ".join(mismatched)
                )
        config = sealed_config
    except _VerificationFailure as exc:
        return _failure(exc)
    except Exception as exc:
        return _failure(
            _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_PERSIST_FAILED,
                f"Git delivery contract is not ready: {_redact(str(exc))}",
            )
        )

    strict_config = replace(config, required_mode=True)
    result = verify_git_delivery(strict_config, github_query=github_query)
    if not result.ok or result.receipt is None:
        return result
    try:
        persisted = kanban_db._persist_verified_git_delivery_receipt(
            conn,
            task_id,
            asdict(result.receipt),
        )
    except Exception as exc:
        return _failure(
            _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_PERSIST_FAILED,
                f"Verified Git receipt could not be persisted: {_redact(str(exc))}",
            )
        )
    if not persisted:
        return _failure(
            _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_PERSIST_FAILED,
                "Verified Git receipt does not match the task's durable delivery obligation",
            )
        )
    return result


def validate_persisted_git_delivery_receipt(
    receipt_json: str,
    candidate_digest: str,
    *,
    receipt_fingerprint: str,
    sealed_verified_at: int,
    canonical_worktree: str,
    branch: str,
    require_worktree: bool = True,
) -> GitDeliveryResult:
    """Revalidate a sealed receipt and its local worktree identity.

    This is the final, read-only fence used immediately before a Kanban card
    becomes ``done``.  It does not repeat the remote GitHub query; instead it
    proves that the immutable receipt still hashes to the stored digest and
    that the exact clean canonical worktree is still on the receipt's branch
    and HEAD. Cleanup retry may set ``require_worktree=False`` only after the
    original fenced removal already succeeded; digest and task identity remain
    mandatory while the owned branch deletion is retried. The original
    :func:`verify_git_delivery` remains the authority that proved the merged PR
    and successful required checks.

    The SHA-256 fingerprints here detect partial/corrupt local state; they are
    not an authentication boundary against an actor with arbitrary SQLite
    write access. Active completion therefore always refreshes the remote proof
    before calling this local fence. After cleanup, this function preserves an
    internally coherent historical receipt only.
    """

    try:
        raw = json.loads(receipt_json)
        if not isinstance(raw, Mapping):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt is not a JSON object",
            )
        if (
            set(raw) != _RECEIPT_FIELDS
            or raw.get("schema_version") != _RECEIPT_SCHEMA_VERSION
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has an unknown schema",
            )
        verified_at_text = str(raw.get("verified_at") or "")
        try:
            verified_at = datetime.fromisoformat(verified_at_text)
        except ValueError as exc:
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has an invalid verification time",
            ) from exc
        if (
            verified_at.tzinfo is None
            or verified_at.utcoffset() != timezone.utc.utcoffset(verified_at)
            or abs(verified_at.timestamp() - int(sealed_verified_at)) > 2.0
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt verification time is not sealed",
            )
        canonical_receipt = json.dumps(
            dict(raw),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_receipt_fingerprint = hashlib.sha256(
            canonical_receipt.encode("utf-8")
        ).hexdigest()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", receipt_fingerprint or "")
            or expected_receipt_fingerprint
            != str(receipt_fingerprint or "").lower()
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt fingerprint does not match",
            )
        checks = raw.get("checks")
        artifacts = raw.get("artifacts")
        if (
            not isinstance(checks, list)
            or not checks
            or any(
                not isinstance(check, Mapping)
                or not str(check.get("name") or "")
                or str(check.get("state") or "").upper() != "SUCCESS"
                for check in checks
            )
            or not isinstance(artifacts, list)
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has invalid checks or artifacts",
            )
        receipt_worktree_text = str(raw.get("canonical_worktree") or "").strip()
        task_worktree_text = str(canonical_worktree or "").strip()
        if not receipt_worktree_text or not task_worktree_text:
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has no canonical worktree identity",
            )
        if require_worktree:
            receipt_worktree = _canonical_path(Path(receipt_worktree_text))
            task_worktree = _canonical_path(Path(task_worktree_text))
        else:
            # After ``git worktree remove`` the path intentionally no longer
            # exists while a conditional branch deletion may still need a
            # retry. Preserve the already-sealed absolute identity without
            # requiring that deleted path to resolve again.
            receipt_worktree = Path(receipt_worktree_text).expanduser().resolve(
                strict=False
            )
            task_worktree = Path(task_worktree_text).expanduser().resolve(
                strict=False
            )
        receipt_branch = str(raw.get("branch") or "")
        task_branch = str(branch or "")
        if (
            _path_key(receipt_worktree) != _path_key(task_worktree)
            or receipt_branch != task_branch
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt no longer matches the task identity",
            )
        head_sha = str(raw.get("head_sha") or "").lower()
        head_remote_raw = raw.get("head_remote_sha")
        head_remote_sha = (
            str(head_remote_raw).lower() if head_remote_raw is not None else None
        )
        head_remote_disposition = str(raw.get("head_remote_disposition") or "")
        base_remote_sha = str(raw.get("base_remote_sha") or "").lower()
        merge_sha = str(raw.get("merge_sha") or "").lower()
        stored_digest = str(candidate_digest or "").lower()
        remote_proof_digest = str(raw.get("remote_proof_digest") or "").lower()
        head_remote_is_coherent = (
            head_remote_disposition == _HEAD_REMOTE_PRESENT
            and head_remote_sha == head_sha
            and bool(head_remote_sha and _HEX_OID.fullmatch(head_remote_sha))
        ) or (
            head_remote_disposition == _HEAD_REMOTE_DELETED_AFTER_MERGE
            and head_remote_sha is None
        )
        if (
            not _HEX_OID.fullmatch(head_sha)
            or not head_remote_is_coherent
            or not re.fullmatch(r"[0-9a-f]{40}", base_remote_sha)
            or set(base_remote_sha) == {"0"}
            or not _HEX_OID.fullmatch(merge_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_digest)
            or str(raw.get("candidate_digest") or "").lower() != stored_digest
            or not re.fullmatch(r"[0-9a-f]{64}", remote_proof_digest)
        ):
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has invalid object IDs or digest",
            )
        try:
            pr_number = int(raw.get("pr_number"))
        except (TypeError, ValueError) as exc:
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery receipt has an invalid PR number",
            ) from exc
        candidate_payload = {
            "schema": 2,
            "canonical_worktree": str(receipt_worktree),
            "branch": receipt_branch,
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "head_repository": str(raw.get("head_repository") or "").casefold(),
            "base_repository": str(raw.get("base_repository") or "").casefold(),
            "base_branch": str(raw.get("base_branch") or ""),
            "pr_number": pr_number,
            "pr_url": str(raw.get("pr_url") or ""),
            "checks": checks,
            "artifacts": artifacts,
        }
        if _digest(candidate_payload) != stored_digest:
            raise _VerificationFailure(
                GitDeliveryErrorCode.CANDIDATE_CHANGED,
                "Persisted Git delivery receipt digest no longer matches its facts",
            )
        expected_remote_proof_digest = _remote_proof_digest(
            candidate_digest=stored_digest,
            verified_at=verified_at_text,
            head_sha=head_sha,
            head_remote_sha=head_remote_sha,
            head_remote_disposition=head_remote_disposition,
            base_remote_sha=base_remote_sha,
            merge_sha=merge_sha,
        )
        if remote_proof_digest != expected_remote_proof_digest:
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted Git delivery remote proof no longer matches its facts",
            )
        if not require_worktree:
            return GitDeliveryResult(
                ok=True,
                code=GitDeliveryErrorCode.OK,
                message="Persisted Git delivery receipt and task identity are unchanged",
                candidate_digest=stored_digest,
            )
        fence_config = GitDeliveryConfig(
            repo_path=task_worktree,
            canonical_worktree=task_worktree,
            expected_branch=task_branch,
            head_remote="origin",
            expected_head_remote_url="https://invalid.local/head",
            base_remote="origin",
            expected_base_remote_url="https://invalid.local/base",
            head_repository="sealed/head",
            base_repository="sealed/base",
            base_branch="sealed-base",
            pr_number=pr_number,
        )
        _, current_branch, current_head = _verify_worktree(
            fence_config, require_clean=True
        )
        if current_branch != receipt_branch or current_head != head_sha:
            raise _VerificationFailure(
                GitDeliveryErrorCode.CANDIDATE_CHANGED,
                "Canonical worktree changed after the Git delivery receipt was sealed",
                expected_head=head_sha,
                actual_head=current_head,
            )
        base_probe = _git(
            fence_config,
            "cat-file",
            "-e",
            f"{base_remote_sha}^{{commit}}",
            allowed_returncodes=frozenset({0, 1, 128}),
        )
        if base_probe.returncode != 0:
            raise _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                "Persisted base remote object is unavailable",
            )
        for ancestor in (head_sha, merge_sha):
            ancestry = _git(
                fence_config,
                "merge-base",
                "--is-ancestor",
                ancestor,
                base_remote_sha,
                allowed_returncodes=frozenset({0, 1}),
            )
            if ancestry.returncode != 0:
                raise _VerificationFailure(
                    GitDeliveryErrorCode.RECEIPT_INVALID,
                    "Persisted remote ancestry is inconsistent",
                )
        return GitDeliveryResult(
            ok=True,
            code=GitDeliveryErrorCode.OK,
            message="Persisted Git delivery receipt and canonical worktree are unchanged",
            candidate_digest=stored_digest,
        )
    except _VerificationFailure as exc:
        return _failure(exc)
    except Exception as exc:  # fail closed at the public boundary
        return _failure(
            _VerificationFailure(
                GitDeliveryErrorCode.RECEIPT_INVALID,
                f"Persisted Git delivery receipt could not be fenced: {_redact(str(exc))}",
            )
        )


__all__ = [
    "ArtifactEvidence",
    "CheckEvidence",
    "GitDeliveryConfig",
    "GitDeliveryErrorCode",
    "GitDeliveryReceipt",
    "GitDeliveryResult",
    "build_git_delivery_config_from_contract",
    "query_github_pull_request",
    "validate_persisted_git_delivery_receipt",
    "verify_and_persist_git_delivery",
    "verify_git_delivery",
]
