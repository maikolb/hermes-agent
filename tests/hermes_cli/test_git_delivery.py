"""Hermetic contracts for the read-only Git/GitHub delivery verifier."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import (
    noninteractive_git_env,
    windows_hidden_popen_kwargs,
)
from hermes_cli.git_delivery import (
    GitDeliveryConfig,
    GitDeliveryErrorCode,
    query_github_pull_request,
    validate_persisted_git_delivery_receipt,
    verify_and_persist_git_delivery,
    verify_git_delivery,
)


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    command_env = noninteractive_git_env(env)
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
        env=command_env,
        **windows_hidden_popen_kwargs(),
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
    )
    return result.stdout.strip()


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")


def _pr_payload(
    *,
    head_sha: str,
    merge_sha: str,
    artifact: str = "app.txt",
    files: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "data": {
            "repository": {
                "nameWithOwner": "acme/widget",
                "pullRequest": {
                    "number": 17,
                    "url": "https://github.com/acme/widget/pull/17",
                    "state": "MERGED",
                    "isDraft": False,
                    "merged": True,
                    "mergedAt": "2026-08-24T10:00:00Z",
                    "headRefName": "delivery/feature",
                    "headRefOid": head_sha,
                    "baseRefName": "main",
                    "headRepository": {"nameWithOwner": "acme/widget"},
                    "baseRepository": {"nameWithOwner": "acme/widget"},
                    "mergeCommit": {"oid": merge_sha},
                    "files": {
                        "nodes": files
                        if files is not None
                        else [{"path": artifact, "changeType": "MODIFIED"}],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "statusCheckRollup": {
                                        "state": "SUCCESS",
                                        "contexts": {
                                            "pageInfo": {"hasNextPage": False},
                                            "nodes": [
                                                {
                                                    "__typename": "CheckRun",
                                                    "name": "tests",
                                                    "status": "COMPLETED",
                                                    "conclusion": "SUCCESS",
                                                },
                                                {
                                                    "__typename": "StatusContext",
                                                    "context": "policy",
                                                    "state": "SUCCESS",
                                                },
                                            ],
                                        },
                                    }
                                }
                            }
                        ]
                    },
                },
            }
        }
    }


@pytest.fixture
def delivery_repo(tmp_path: Path):
    bare = tmp_path / "origin.git"
    repo = tmp_path / "candidate"
    integrator = tmp_path / "integrator"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "init", "-b", "main", str(repo))
    _configure_identity(repo)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "checkout", "-b", "delivery/feature")
    (repo / "app.txt").write_text("delivered\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "deliver app")
    head_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-u", "origin", "delivery/feature")
    _git(repo, "checkout", "main")
    worktree = repo / ".worktrees" / "delivery-fixture"
    _git(repo, "worktree", "add", str(worktree), "delivery/feature")

    _git(tmp_path, "init", str(integrator))
    _configure_identity(integrator)
    _git(integrator, "remote", "add", "origin", str(bare))
    _git(integrator, "fetch", "origin", "main", "delivery/feature")
    _git(integrator, "checkout", "-b", "main", "origin/main")
    _git(
        integrator,
        "merge",
        "--no-ff",
        "origin/delivery/feature",
        "-m",
        "merge delivery",
    )
    merge_sha = _git(integrator, "rev-parse", "HEAD")
    _git(integrator, "push", "origin", "main")

    # The verifier never fetches. Seed the exact base object as test setup.
    _git(worktree, "fetch", "origin", "main:refs/remotes/origin/main")

    config = GitDeliveryConfig(
        repo_path=worktree,
        canonical_worktree=worktree,
        expected_branch="delivery/feature",
        head_remote="origin",
        expected_head_remote_url=str(bare),
        base_remote="origin",
        expected_base_remote_url=str(bare),
        head_repository="acme/widget",
        base_repository="acme/widget",
        base_branch="main",
        pr_number=17,
        required_checks=("tests", "policy"),
        declared_artifacts=("app.txt",),
        timeout_seconds=10,
    )
    payload = _pr_payload(head_sha=head_sha, merge_sha=merge_sha)
    return config, payload, bare


def _changed_delivery_repo(
    tmp_path: Path,
    *,
    change: str,
) -> tuple[GitDeliveryConfig, dict, Path]:
    root = tmp_path / change
    root.mkdir()
    bare = root / "origin.git"
    repo = root / "candidate"
    integrator = root / "integrator"
    _git(root, "init", "--bare", str(bare))
    _git(root, "init", "-b", "main", str(repo))
    _configure_identity(repo)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "checkout", "-b", "delivery/feature")
    if change == "delete":
        _git(repo, "rm", "app.txt")
        declared = ("app.txt",)
        files = [{"path": "app.txt", "changeType": "DELETED"}]
    elif change == "rename":
        _git(repo, "mv", "app.txt", "renamed.txt")
        declared = ("app.txt", "renamed.txt")
        files = [{"path": "renamed.txt", "changeType": "RENAMED"}]
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(f"unknown delivery change: {change}")
    _git(repo, "commit", "-m", f"{change} app")
    head_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-u", "origin", "delivery/feature")
    _git(repo, "checkout", "main")
    worktree = repo / ".worktrees" / f"{change}-fixture"
    _git(repo, "worktree", "add", str(worktree), "delivery/feature")

    _git(root, "init", str(integrator))
    _configure_identity(integrator)
    _git(integrator, "remote", "add", "origin", str(bare))
    _git(integrator, "fetch", "origin", "main", "delivery/feature")
    _git(integrator, "checkout", "-b", "main", "origin/main")
    _git(
        integrator,
        "merge",
        "--no-ff",
        "origin/delivery/feature",
        "-m",
        f"merge {change}",
    )
    merge_sha = _git(integrator, "rev-parse", "HEAD")
    _git(integrator, "push", "origin", "main")
    _git(worktree, "fetch", "origin", "main:refs/remotes/origin/main")

    config = GitDeliveryConfig(
        repo_path=worktree,
        canonical_worktree=worktree,
        expected_branch="delivery/feature",
        head_remote="origin",
        expected_head_remote_url=str(bare),
        base_remote="origin",
        expected_base_remote_url=str(bare),
        head_repository="acme/widget",
        base_repository="acme/widget",
        base_branch="main",
        pr_number=17,
        required_checks=("tests", "policy"),
        declared_artifacts=declared,
        timeout_seconds=10,
    )
    payload = _pr_payload(head_sha=head_sha, merge_sha=merge_sha, files=files)
    return config, payload, bare


def _verify(config: GitDeliveryConfig, payload: dict):
    return verify_git_delivery(config, github_query=lambda _config: payload)


def _seal_required_contract(kb, conn, task_id: str, config: GitDeliveryConfig) -> dict:
    policy = {
        "required": True,
        "head_remote": config.head_remote,
        "expected_head_remote_url": config.expected_head_remote_url,
        "base_remote": config.base_remote,
        "expected_base_remote_url": config.expected_base_remote_url,
        "head_repository": config.head_repository,
        "base_repository": config.base_repository,
        "base_branch": config.base_branch,
        "required_checks": list(config.required_checks),
        "github_host": config.github_host,
        "timeout_seconds": config.timeout_seconds,
    }
    policy_json, policy_fingerprint = kb._canonical_delivery_document(policy)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_git_delivery SET required = 1, policy_json = ?, "
            "policy_fingerprint = ? WHERE task_id = ?",
            (policy_json, policy_fingerprint, task_id),
        )
    return {
        "pull_request": config.pr_number,
        "declared_artifacts": list(config.declared_artifacts),
    }


def _bind_config_to_task(
    kb,
    conn,
    task_id: str,
    config: GitDeliveryConfig,
    payload: dict,
) -> tuple[GitDeliveryConfig, dict]:
    """Move one fixture worktree onto Hermes' exact task-owned identity."""

    common = Path(
        _git(
            config.repo_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    repo_root = common.parent
    branch = f"project/{task_id}-delivery"
    target = repo_root / ".worktrees" / task_id
    _git(config.repo_path, "branch", "-m", branch)
    _git(config.repo_path, "push", "-u", "origin", branch)
    _git(repo_root, "worktree", "move", str(config.repo_path), str(target))
    kb._seal_materialized_worktree_ownership(
        conn,
        task_id,
        repo_root=repo_root,
        worktree=target,
        branch=branch,
    )
    task_payload = copy.deepcopy(payload)
    task_payload["data"]["repository"]["pullRequest"]["headRefName"] = branch
    return (
        replace(
            config,
            repo_path=target,
            canonical_worktree=target,
            expected_branch=branch,
        ),
        task_payload,
    )


def test_default_github_query_is_bounded_hidden_and_noninteractive(
    tmp_path: Path, monkeypatch
):
    from hermes_cli import git_delivery

    config = GitDeliveryConfig(
        repo_path=tmp_path,
        canonical_worktree=tmp_path,
        expected_branch="delivery/feature",
        head_remote="origin",
        expected_head_remote_url="https://github.com/acme/widget.git",
        base_remote="origin",
        expected_base_remote_url="https://github.com/acme/widget.git",
        head_repository="acme/widget",
        base_repository="acme/widget",
        base_branch="main",
        pr_number=17,
    )
    payload = {"data": {"repository": {"pullRequest": None}}}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(git_delivery.subprocess, "run", fake_run)
    assert query_github_pull_request(config) == payload
    argv, kwargs = calls[0]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == config.timeout_seconds
    assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
    if os.name == "nt":
        assert kwargs["creationflags"] & 0x08000000


def test_proves_exact_delivery_and_returns_typed_receipt(delivery_repo):
    config, payload, _bare = delivery_repo
    result = _verify(config, payload)

    assert result.ok is True
    assert result.code is GitDeliveryErrorCode.OK
    assert result.receipt is not None
    assert (
        result.receipt.head_sha
        == payload["data"]["repository"]["pullRequest"]["headRefOid"]
    )
    assert (
        result.receipt.merge_sha
        == payload["data"]["repository"]["pullRequest"]["mergeCommit"]["oid"]
    )
    assert result.receipt.candidate_digest == result.candidate_digest
    assert len(result.candidate_digest or "") == 64
    assert [item.name for item in result.receipt.checks] == ["tests", "policy"]
    assert result.receipt.artifacts[0].path == "app.txt"


def test_graphql_errors_fail_closed_even_when_data_is_present(delivery_repo):
    config, payload, _bare = delivery_repo
    payload = json.loads(json.dumps(payload))
    payload["errors"] = [{"message": "partial authorization failure"}]

    result = _verify(config, payload)

    assert result.ok is False
    assert result.code is GitDeliveryErrorCode.PR_QUERY_FAILED


def test_graphql_repository_identity_must_match_contract(delivery_repo):
    config, payload, _bare = delivery_repo
    payload = json.loads(json.dumps(payload))
    payload["data"]["repository"]["nameWithOwner"] = "acme/other"

    result = _verify(config, payload)

    assert result.ok is False
    assert result.code is GitDeliveryErrorCode.PR_QUERY_FAILED


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_proves_deletions_and_renames_with_tombstones(
    tmp_path: Path,
    change: str,
):
    config, payload, _bare = _changed_delivery_repo(tmp_path, change=change)
    result = _verify(config, payload)

    assert result.ok
    assert result.receipt is not None
    by_path = {artifact.path: artifact for artifact in result.receipt.artifacts}
    assert by_path["app.txt"].state == "deleted"
    assert by_path["app.txt"].sha256 is None
    assert by_path["app.txt"].git_oid is None
    if change == "rename":
        assert by_path["renamed.txt"].state == "committed"
        assert by_path["renamed.txt"].sha256 is not None
        assert by_path["renamed.txt"].git_oid is not None


def test_artifact_hash_comes_from_git_blob_not_checkout(
    delivery_repo,
    monkeypatch,
):
    config, payload, _bare = delivery_repo
    expected = hashlib.sha256(b"delivered\n").hexdigest()

    def fail_read_bytes(_path: Path) -> bytes:
        raise AssertionError("final verification must not hash checkout bytes")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    result = _verify(config, payload)

    assert result.ok
    assert result.receipt is not None
    assert result.receipt.artifacts[0].sha256 == expected


def test_merged_pr_remains_provable_after_remote_head_branch_is_deleted(
    delivery_repo,
):
    config, payload, _bare = delivery_repo
    _git(config.repo_path, "push", "origin", "--delete", "delivery/feature")

    result = _verify(config, payload)

    assert result.ok
    assert result.receipt is not None
    assert result.receipt.head_remote_sha is None
    assert result.receipt.head_remote_disposition == "deleted_after_merge"
    assert len(result.receipt.remote_proof_digest) == 64


def test_dirty_tracked_worktree_fails_closed(delivery_repo):
    config, payload, _bare = delivery_repo
    (config.repo_path / "app.txt").write_text("dirty\n", encoding="utf-8")
    result = _verify(config, payload)
    assert result.code is GitDeliveryErrorCode.DIRTY_WORKTREE


def test_untracked_worktree_fails_closed(delivery_repo):
    config, payload, _bare = delivery_repo
    (config.repo_path / "untracked.txt").write_text("x", encoding="utf-8")
    result = _verify(config, payload)
    assert result.code is GitDeliveryErrorCode.DIRTY_WORKTREE


def test_detached_head_fails_closed(delivery_repo):
    config, payload, _bare = delivery_repo
    _git(config.repo_path, "checkout", "--detach", "HEAD")
    result = _verify(config, payload)
    assert result.code is GitDeliveryErrorCode.DETACHED_HEAD


def test_wrong_branch_fails_closed(delivery_repo):
    config, payload, _bare = delivery_repo
    result = _verify(replace(config, expected_branch="delivery/other"), payload)
    assert result.code is GitDeliveryErrorCode.WRONG_BRANCH


def test_noncanonical_worktree_fails_closed(delivery_repo):
    config, payload, _bare = delivery_repo
    result = _verify(
        replace(config, canonical_worktree=config.repo_path.parent), payload
    )
    assert result.code is GitDeliveryErrorCode.NONCANONICAL_WORKTREE


def test_main_manual_and_foreign_task_worktrees_fail_closed(
    delivery_repo,
    tmp_path: Path,
):
    from hermes_cli import kanban_db as kb

    config, payload, _bare = delivery_repo
    common = Path(
        _git(
            config.repo_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    main_checkout = common.parent

    main_result = _verify(
        replace(
            config,
            repo_path=main_checkout,
            canonical_worktree=main_checkout,
            expected_branch="main",
        ),
        payload,
    )
    assert main_result.code is GitDeliveryErrorCode.NONCANONICAL_WORKTREE

    manual = tmp_path / "manual-linked-worktree"
    _git(
        main_checkout,
        "worktree",
        "add",
        "-b",
        "manual/foreign",
        str(manual),
        _git(config.repo_path, "rev-parse", "HEAD"),
    )
    manual_result = _verify(
        replace(
            config,
            repo_path=manual,
            canonical_worktree=manual,
            expected_branch="manual/foreign",
        ),
        payload,
    )
    assert manual_result.code is GitDeliveryErrorCode.NONCANONICAL_WORKTREE

    with kb.connect_closing(tmp_path / "foreign-task.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Foreign worktree identity",
            workspace_kind="worktree",
            workspace_path=str(config.repo_path),
            branch_name=config.expected_branch,
        )
        request = _seal_required_contract(kb, conn, task_id, config)
        assert kb.request_review(conn, task_id, git_delivery_request=request)
        foreign = verify_and_persist_git_delivery(
            conn,
            task_id,
            config,
            github_query=lambda _config: pytest.fail(
                "foreign task identity must fail before GitHub"
            ),
        )
        assert foreign.code is GitDeliveryErrorCode.NONCANONICAL_WORKTREE


def test_wrong_remote_fails_closed_and_redacts_credentials(delivery_repo):
    config, payload, _bare = delivery_repo
    secret_url = "https://ghp_SUPERSECRET123456@github.com/acme/widget.git"
    result = _verify(replace(config, expected_head_remote_url=secret_url), payload)
    assert result.code is GitDeliveryErrorCode.WRONG_REMOTE
    assert "SUPERSECRET" not in repr(result)


def test_local_head_must_equal_exact_remote_branch_sha(delivery_repo):
    config, payload, _bare = delivery_repo
    _git(config.repo_path, "commit", "--allow-empty", "-m", "not pushed")
    result = _verify(config, payload)
    assert result.code is GitDeliveryErrorCode.REMOTE_SHA_MISMATCH


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda pr: pr.__setitem__(
                "headRepository", {"nameWithOwner": "other/widget"}
            ),
            GitDeliveryErrorCode.PR_WRONG_HEAD_REPOSITORY,
        ),
        (
            lambda pr: pr.__setitem__("number", 18),
            GitDeliveryErrorCode.PR_WRONG_NUMBER,
        ),
        (
            lambda pr: pr.__setitem__("headRefName", "delivery/other"),
            GitDeliveryErrorCode.PR_WRONG_HEAD_BRANCH,
        ),
        (
            lambda pr: pr.__setitem__("headRefOid", "1" * 40),
            GitDeliveryErrorCode.PR_WRONG_HEAD_OID,
        ),
        (
            lambda pr: pr.__setitem__(
                "baseRepository", {"nameWithOwner": "other/widget"}
            ),
            GitDeliveryErrorCode.PR_WRONG_BASE_REPOSITORY,
        ),
        (
            lambda pr: pr.__setitem__("baseRefName", "develop"),
            GitDeliveryErrorCode.PR_WRONG_BASE_BRANCH,
        ),
        (
            lambda pr: pr.__setitem__("isDraft", True),
            GitDeliveryErrorCode.PR_DRAFT,
        ),
        (
            lambda pr: pr.__setitem__("merged", False),
            GitDeliveryErrorCode.PR_NOT_MERGED,
        ),
    ],
)
def test_pr_identity_and_state_are_exact(delivery_repo, mutation, expected):
    config, payload, _bare = delivery_repo
    changed = copy.deepcopy(payload)
    mutation(changed["data"]["repository"]["pullRequest"])
    result = _verify(config, changed)
    assert result.code is expected


def test_required_check_must_be_present(delivery_repo):
    config, payload, _bare = delivery_repo
    changed = copy.deepcopy(payload)
    nodes = changed["data"]["repository"]["pullRequest"]["commits"]["nodes"]
    nodes[0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = []
    result = _verify(config, changed)
    assert result.code is GitDeliveryErrorCode.REQUIRED_CHECK_MISSING


def test_required_mode_rejects_an_empty_check_contract(delivery_repo):
    config, payload, _bare = delivery_repo
    result = _verify(
        replace(config, required_mode=True, required_checks=()),
        payload,
    )
    assert result.code is GitDeliveryErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"expected_branch": "-unsafe"}, "expected_branch"),
        ({"base_branch": "bad..branch"}, "base_branch"),
        ({"head_remote": "--upload-pack=malicious"}, "head_remote"),
        ({"base_remote": "bad..remote"}, "base_remote"),
    ],
)
def test_branch_and_remote_ref_inputs_are_validated_before_use(
    delivery_repo,
    changes,
    field,
):
    config, payload, _bare = delivery_repo
    result = _verify(replace(config, **changes), payload)
    assert result.code is GitDeliveryErrorCode.CONFIG_INVALID, field


def test_malformed_check_pagination_fails_as_structured_result(delivery_repo):
    config, payload, _bare = delivery_repo
    changed = copy.deepcopy(payload)
    nodes = changed["data"]["repository"]["pullRequest"]["commits"]["nodes"]
    nodes[0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = "invalid"
    result = _verify(config, changed)
    assert result.ok is False
    assert result.code is GitDeliveryErrorCode.REQUIRED_CHECK_MISSING


def test_required_check_must_be_uniquely_green(delivery_repo):
    config, payload, _bare = delivery_repo
    changed = copy.deepcopy(payload)
    nodes = changed["data"]["repository"]["pullRequest"]["commits"]["nodes"]
    nodes[0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]["conclusion"] = (
        "FAILURE"
    )
    result = _verify(config, changed)
    assert result.code is GitDeliveryErrorCode.REQUIRED_CHECK_NOT_GREEN


def test_declared_artifacts_must_exactly_match_pr_files(delivery_repo):
    config, payload, _bare = delivery_repo
    changed = copy.deepcopy(payload)
    changed["data"]["repository"]["pullRequest"]["files"]["nodes"].append({
        "path": "undeclared.txt"
    })
    result = _verify(config, changed)
    assert result.code is GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH


def test_merge_object_must_be_ancestor_of_exact_base_remote(delivery_repo):
    config, payload, _bare = delivery_repo
    tree = _git(config.repo_path, "rev-parse", "HEAD^{tree}")
    unrelated = _git(config.repo_path, "commit-tree", tree, "-m", "unrelated root")
    changed = copy.deepcopy(payload)
    changed["data"]["repository"]["pullRequest"]["mergeCommit"]["oid"] = unrelated
    result = _verify(config, changed)
    assert result.code is GitDeliveryErrorCode.MERGE_NOT_ANCESTOR


def test_squash_result_without_task_head_ancestry_is_rejected(
    delivery_repo,
    tmp_path: Path,
):
    config, payload, bare = delivery_repo
    head_sha = _git(config.repo_path, "rev-parse", "HEAD")
    merge_sha = payload["data"]["repository"]["pullRequest"]["mergeCommit"]["oid"]
    base_sha = _git(config.repo_path, "rev-parse", f"{merge_sha}^1")
    squasher = tmp_path / "squasher"
    _git(tmp_path, "init", str(squasher))
    _configure_identity(squasher)
    _git(squasher, "remote", "add", "origin", str(bare))
    _git(squasher, "fetch", "origin")
    _git(squasher, "checkout", "-b", "main", base_sha)
    _git(squasher, "cherry-pick", head_sha)
    squash_sha = _git(squasher, "rev-parse", "HEAD")
    _git(squasher, "push", "--force", "origin", "main")
    _git(
        config.repo_path,
        "fetch",
        "origin",
        "+main:refs/remotes/origin/main",
    )
    changed = copy.deepcopy(payload)
    changed["data"]["repository"]["pullRequest"]["mergeCommit"]["oid"] = squash_sha

    result = _verify(config, changed)

    assert result.code is GitDeliveryErrorCode.HEAD_NOT_ANCESTOR


def test_head_and_merge_tree_must_have_identical_path_state_and_blob(
    delivery_repo,
    tmp_path: Path,
):
    config, payload, bare = delivery_repo
    advancer = tmp_path / "wrong-merge-tree"
    _git(tmp_path, "init", str(advancer))
    _configure_identity(advancer)
    _git(advancer, "remote", "add", "origin", str(bare))
    _git(advancer, "fetch", "origin", "main")
    _git(advancer, "checkout", "-b", "main", "origin/main")
    (advancer / "app.txt").write_text("wrong merged bytes\n", encoding="utf-8")
    _git(advancer, "add", "app.txt")
    _git(advancer, "commit", "-m", "advance with wrong tree")
    wrong_merge_sha = _git(advancer, "rev-parse", "HEAD")
    _git(advancer, "push", "origin", "main")
    _git(config.repo_path, "fetch", "origin", "main:refs/remotes/origin/main")

    changed = copy.deepcopy(payload)
    changed["data"]["repository"]["pullRequest"]["mergeCommit"]["oid"] = wrong_merge_sha
    result = _verify(config, changed)

    assert result.code is GitDeliveryErrorCode.PR_ARTIFACT_MISMATCH


def test_prior_candidate_digest_must_still_match(delivery_repo):
    config, payload, _bare = delivery_repo
    result = _verify(config, payload)
    assert result.ok
    repeated = _verify(
        replace(config, expected_candidate_digest=result.candidate_digest), payload
    )
    assert repeated.ok
    stale = _verify(replace(config, expected_candidate_digest="0" * 64), payload)
    assert stale.code is GitDeliveryErrorCode.CANDIDATE_CHANGED


def test_candidate_digest_is_stable_when_base_branch_advances(
    delivery_repo,
    tmp_path: Path,
):
    config, payload, bare = delivery_repo
    first = _verify(config, payload)
    assert first.ok

    advancer = tmp_path / "base-advancer"
    _git(tmp_path, "init", str(advancer))
    _configure_identity(advancer)
    _git(advancer, "remote", "add", "origin", str(bare))
    _git(advancer, "fetch", "origin", "main")
    _git(advancer, "checkout", "-b", "main", "origin/main")
    _git(advancer, "commit", "--allow-empty", "-m", "advance base")
    _git(advancer, "push", "origin", "main")
    _git(config.repo_path, "fetch", "origin", "main:refs/remotes/origin/main")

    repeated = _verify(
        replace(config, expected_candidate_digest=first.candidate_digest),
        payload,
    )
    assert repeated.ok
    assert repeated.candidate_digest == first.candidate_digest


def test_verified_receipt_idempotently_unlocks_same_worktree_card(
    delivery_repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import kanban_db as kb

    config, payload, _bare = delivery_repo
    db_path = tmp_path / "kanban.db"
    completed_hooks: list[str] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, *_args, **_kwargs: completed_hooks.append(event),
    )
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="Deliver exact candidate",
            workspace_kind="worktree",
            workspace_path=str(config.repo_path),
            branch_name=config.expected_branch,
        )
        config, payload = _bind_config_to_task(
            kb, conn, task_id, config, payload
        )
        delivery_request = _seal_required_contract(kb, conn, task_id, config)

        assert kb.request_review(
            conn,
            task_id,
            summary="implementation ready",
            git_delivery_request=delivery_request,
        )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

        first = verify_and_persist_git_delivery(
            conn,
            task_id,
            config,
            github_query=lambda _config: payload,
        )
        repeated = verify_and_persist_git_delivery(
            conn,
            task_id,
            config,
            github_query=lambda _config: payload,
        )
        assert first.ok and repeated.ok
        verified_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "delivery_verified"
        ]
        assert len(verified_events) == 1
        regressed_payload = copy.deepcopy(payload)
        rollup = regressed_payload["data"]["repository"]["pullRequest"][
            "commits"
        ]["nodes"][0]["commit"]["statusCheckRollup"]
        rollup["state"] = "FAILURE"
        rollup["contexts"]["nodes"][0]["conclusion"] = "FAILURE"
        assert kb.complete_task(
            conn,
            task_id,
            summary="stale remote proof",
            git_delivery_github_query=lambda _config: regressed_payload,
        ) is False
        assert kb.get_task(conn, task_id).status == "review"
        assert "kanban_task_completed" not in completed_hooks
        assert kb.complete_task(
            conn,
            task_id,
            summary="verified",
            git_delivery_github_query=lambda _config: payload,
        ) is True
        assert kb.get_task(conn, task_id).status == "done"
        assert completed_hooks.count("kanban_task_completed") == 1


@pytest.mark.parametrize("mutation", ["branch", "dirty"])
def test_worktree_change_after_receipt_blocks_completion(
    delivery_repo,
    tmp_path: Path,
    mutation: str,
):
    from hermes_cli import kanban_db as kb

    config, payload, _bare = delivery_repo
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Fence exact candidate",
            workspace_kind="worktree",
            workspace_path=str(config.repo_path),
            branch_name=config.expected_branch,
        )
        config, payload = _bind_config_to_task(
            kb, conn, task_id, config, payload
        )
        delivery_request = _seal_required_contract(kb, conn, task_id, config)
        assert kb.request_review(
            conn, task_id, git_delivery_request=delivery_request
        )
        verified = verify_and_persist_git_delivery(
            conn,
            task_id,
            config,
            github_query=lambda _config: payload,
        )
        assert verified.ok

        if mutation == "branch":
            _git(config.repo_path, "checkout", "-b", "wrong/branch")
            expected_code = "wrong_branch"
        elif mutation == "dirty":
            (config.repo_path / "late-change.txt").write_text(
                "changed after receipt\n", encoding="utf-8"
            )
            expected_code = "dirty_worktree"
        assert kb.complete_task(
            conn,
            task_id,
            summary="stale receipt",
            git_delivery_github_query=lambda _config: payload,
        ) is False
        assert kb.get_task(conn, task_id).status == "review"
        blocked = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "completion_blocked_delivery"
        ]
        assert blocked
        assert blocked[-1].payload["code"] == expected_code


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("schema_version", 999),
        ("verified_at", "2099-01-01T00:00:00+00:00"),
        ("head_remote_sha", None),
        ("head_remote_sha", "0" * 40),
        ("head_remote_disposition", "deleted_after_merge"),
        ("base_remote_sha", "f" * 40),
        ("base_remote_sha", "0" * 40),
        ("remote_proof_digest", "0" * 64),
    ],
)
def test_full_receipt_tamper_is_detected_and_fresh_remote_proof_repairs_it(
    delivery_repo,
    tmp_path: Path,
    field: str,
    tampered_value: object,
):
    from hermes_cli import kanban_db as kb

    config, payload, _bare = delivery_repo
    with kb.connect_closing(tmp_path / f"tamper-{field}.db") as conn:
        task_id = kb.create_task(
            conn,
            title="Seal the complete delivery receipt",
            workspace_kind="worktree",
            workspace_path=str(config.repo_path),
            branch_name=config.expected_branch,
        )
        config, payload = _bind_config_to_task(
            kb, conn, task_id, config, payload
        )
        delivery_request = _seal_required_contract(kb, conn, task_id, config)
        assert kb.request_review(
            conn,
            task_id,
            git_delivery_request=delivery_request,
        )
        verified = verify_and_persist_git_delivery(
            conn,
            task_id,
            config,
            github_query=lambda _config: payload,
        )
        assert verified.ok
        sealed = conn.execute(
            "SELECT candidate_digest, receipt_json, receipt_fingerprint, "
            "verified_at "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        original = json.loads(sealed["receipt_json"])
        original_value = original[field]
        tampered = dict(original)
        tampered[field] = tampered_value
        tampered_json = json.dumps(
            tampered, sort_keys=True, separators=(",", ":")
        )
        tampered_fingerprint = hashlib.sha256(
            tampered_json.encode("utf-8")
        ).hexdigest()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_git_delivery SET receipt_json = ?, "
                "receipt_fingerprint = ? WHERE task_id = ?",
                (
                    tampered_json,
                    tampered_fingerprint,
                    task_id,
                ),
            )

        stale = validate_persisted_git_delivery_receipt(
            tampered_json,
            sealed["candidate_digest"],
            receipt_fingerprint=tampered_fingerprint,
            sealed_verified_at=sealed["verified_at"],
            canonical_worktree=str(config.canonical_worktree),
            branch=config.expected_branch,
        )
        assert stale.code is GitDeliveryErrorCode.RECEIPT_INVALID

        # Completion always re-queries the remote authority. The fresh result
        # replaces the whole receipt + fingerprint atomically before the local
        # completion fence runs, so adulterated evidence cannot survive.
        assert kb.complete_task(
            conn,
            task_id,
            summary="fresh proof",
            git_delivery_github_query=lambda _config: payload,
        ) is True
        repaired = conn.execute(
            "SELECT candidate_digest, receipt_json, receipt_fingerprint, "
            "verified_at "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        repaired_receipt = json.loads(repaired["receipt_json"])
        if field in {"verified_at", "remote_proof_digest"}:
            assert repaired_receipt[field] != tampered_value
        else:
            assert repaired_receipt[field] == original_value
        assert repaired["receipt_fingerprint"] == hashlib.sha256(
            repaired["receipt_json"].encode("utf-8")
        ).hexdigest()
        repaired_fence = validate_persisted_git_delivery_receipt(
            repaired["receipt_json"],
            repaired["candidate_digest"],
            receipt_fingerprint=repaired["receipt_fingerprint"],
            sealed_verified_at=repaired["verified_at"],
            canonical_worktree=str(config.canonical_worktree),
            branch=config.expected_branch,
            require_worktree=False,
        )
        assert repaired_fence.ok


def test_failed_worktree_cleanup_stays_pending_and_dispatch_retries(
    delivery_repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import kanban_db as kb

    config, payload, _bare = delivery_repo
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    original_cleanup = kb._cleanup_worktree_workspace
    original_cleanup_git = kb._cleanup_git

    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="Retry exact cleanup",
            workspace_kind="worktree",
            workspace_path=str(config.repo_path),
        )
        common = Path(
            _git(
                config.repo_path,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        )
        owner_repo = common.parent
        worktree = common.parent / ".worktrees" / task_id
        cleanup_branch = f"project/{task_id}-cleanup"
        kb.set_branch_name(conn, task_id, cleanup_branch)
        _git(
            config.repo_path,
            "worktree",
            "add",
            "-b",
            cleanup_branch,
            str(worktree),
            config.expected_branch,
        )
        kb._seal_materialized_worktree_ownership(
            conn,
            task_id,
            repo_root=owner_repo,
            worktree=worktree,
            branch=cleanup_branch,
        )
        _git(worktree, "push", "-u", "origin", cleanup_branch)
        cleanup_config = replace(
            config,
            repo_path=worktree,
            canonical_worktree=worktree,
            expected_branch=cleanup_branch,
        )
        cleanup_payload = copy.deepcopy(payload)
        cleanup_payload["data"]["repository"]["pullRequest"][
            "headRefName"
        ] = cleanup_branch
        delivery_request = _seal_required_contract(
            kb, conn, task_id, cleanup_config
        )
        assert kb.request_review(
            conn, task_id, git_delivery_request=delivery_request
        )
        verified = verify_and_persist_git_delivery(
            conn,
            task_id,
            cleanup_config,
            github_query=lambda _config: cleanup_payload,
        )
        assert verified.ok
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (os.getpid(), task_id),
            )
        def _raise_cleanup(*_args, **_kwargs):
            raise RuntimeError("simulated remover crash")

        monkeypatch.setattr(kb, "_cleanup_worktree_workspace", _raise_cleanup)

        assert kb.complete_task(
            conn,
            task_id,
            summary="delivered",
            git_delivery_github_query=lambda _config: cleanup_payload,
        ) is True
        pending = conn.execute(
            "SELECT cleanup_state, cleanup_attempts, cleanup_last_error, "
            "cleanup_repo_path, cleanup_fingerprint "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert pending["cleanup_state"] == "pending"
        assert pending["cleanup_attempts"] == 0
        assert pending["cleanup_last_error"] is None
        assert Path(pending["cleanup_repo_path"]).resolve() == owner_repo.resolve()
        assert pending["cleanup_fingerprint"]
        assert worktree.is_dir()

        # The completing worker still owns the worktree, so the immediate
        # cleanup did not run. Once that exact process identity is gone, the
        # existing dispatcher tick owns the retry.
        monkeypatch.setattr(kb, "_process_identity_matches", lambda *_args: False)
        kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: None,
            max_spawn=0,
            reconcile_orphans=False,
        )
        pending = conn.execute(
            "SELECT cleanup_state, cleanup_attempts, cleanup_last_error, "
            "cleanup_repo_path "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert pending["cleanup_state"] == "pending"
        assert pending["cleanup_attempts"] == 1
        assert "simulated remover crash" in pending["cleanup_last_error"]
        assert Path(pending["cleanup_repo_path"]).resolve() == owner_repo.resolve()
        assert worktree.is_dir()

        monkeypatch.setattr(kb, "_cleanup_worktree_workspace", original_cleanup)

        def _fail_branch_delete(repo, *args, **kwargs):
            if args[:2] == ("update-ref", "-d"):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "simulated branch delete failure",
                )
            return original_cleanup_git(repo, *args, **kwargs)

        monkeypatch.setattr(kb, "_cleanup_git", _fail_branch_delete)
        kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: None,
            max_spawn=0,
            reconcile_orphans=False,
        )
        branch_pending = conn.execute(
            "SELECT cleanup_state, cleanup_attempts, cleanup_last_error "
            "FROM task_git_delivery WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert branch_pending["cleanup_state"] == "pending"
        assert branch_pending["cleanup_attempts"] == 2
        assert "simulated branch delete failure" in branch_pending["cleanup_last_error"]
        assert not worktree.exists()

        monkeypatch.setattr(kb, "_cleanup_git", original_cleanup_git)
        kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: None,
            max_spawn=0,
            reconcile_orphans=False,
        )
        completed = conn.execute(
            "SELECT cleanup_state, cleanup_attempts, cleanup_last_error "
            "FROM task_git_delivery "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert completed["cleanup_state"] == "complete", completed[
            "cleanup_last_error"
        ]
        assert completed["cleanup_attempts"] == 2
        assert not worktree.exists()
        branch_probe = kb._cleanup_git(
            config.repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{cleanup_branch}",
        )
        assert branch_probe.returncode == 1
