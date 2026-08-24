# Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: RELEASE
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824`
- Updated At: 2026-08-24T11:52:00-03:00
- Machine Runtime Authority: none: Git and GitHub mutations are issued explicitly by the root agent; no autonomous code-edit loop is authorized

## Requested Outcome
- Confirm that the personal Hermes fork `main` contains the completed consolidation and current official upstream, remove only obsolete clean branches/worktrees whose work is already represented or deliberately superseded, and install a fail-closed repository-native automation that keeps the fork synchronized with upstream.

## Acceptance Criteria
- Fresh fetches prove `main == maikolb/main` and `origin/main` is an ancestor of that exact fork commit before any cleanup.
- The completed consolidation branch and installed integration lineage are ancestors of `main`; older non-ancestral local branches are deleted only when the existing 42/42 audit and release ledger identify their work as retained or superseded.
- Every removed worktree is clean, resolves to an exact path under `C:\Users\maiko\Projetos`, and belongs to this repository's common Git directory.
- The open quarantine PR #1 (`feature/project-ops-core`) and all four stashes remain preserved because their contents were explicitly excluded from the consolidation.
- A private control repository `maikolb/hermes-upstream-sync` owns a bounded scheduled/manual workflow with `permissions: {}`, no third-party action, explicit source `NousResearch/hermes-agent:main` and target `maikolb/hermes-agent:main`, ordinary conflict-detecting Git merge/push, and fail-closed behavior on conflict or concurrent target update.
- The write credential is a new Ed25519 deploy key dedicated to the fork. Its private half is stored only as the control repository secret `HERMES_FORK_DEPLOY_KEY`; the public fork contains no matching private secret, personal token, reusable account SSH key, or credential with access to another repository.
- Workflow syntax and merge behavior pass local validation, the change is merged into the fork through a PR, and a manual target run finishes successfully.
- After remote verification, obsolete auxiliary worktrees and local/remote delivery branches are gone. The canonical checkout moves to `main` only if no live Hermes process holds that installed tree; otherwise its exact clean runtime branch remains pinned and recoverable without changing live files.
- The Windows no-visible-UI verifier reports `VisibleWindows=0` for the real operational command path.

## In Scope
- `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824\docs\EXECUTION_CONTRACT.md`
- Isolated build/operations checkout `C:\Users\maiko\Projetos\hermes-upstream-sync-control-20260824\**`, retained clean after private control-repository publication.
- Git refs and worktree metadata in `C:\Users\maiko\AppData\Local\hermes\hermes-agent\.git\**`.
- A no-runtime safety gate for `C:\Users\maiko\AppData\Local\hermes\hermes-agent\**`; checkout transition to the already-validated fork `main` is allowed only when no live Hermes process uses that tree.
- Registered auxiliary worktrees `C:\Users\maiko\Projetos\hermes-bug-intake-20260821\**`, `C:\Users\maiko\Projetos\hermes-tool-guardrail-fix-20260821\**`, `C:\Users\maiko\Projetos\hermes-workspace-api-worktree\**`, and `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824\**`.
- Personal remote `maikolb`: PR publication/merge, creation of the private control repository, one repository-scoped write deploy key on the fork, one matching Actions secret in the control repository, workflow dispatch, and deletion of only the obsolete delivery branches proven safe by this contract.
- Official remote `origin`: fetch/read-only ancestry comparison only.

## Out of Scope
- Any edit to Hermes product code, tests, runtime configuration, profiles, credentials, data, cron jobs, gateways, or external integrations.
- Starting, stopping, restarting, deploying, canarying, or otherwise exercising the live Hermes runtime.
- Writing to `NousResearch/hermes-agent` or changing any upstream branch, setting, issue, PR, or workflow.
- Force-push, destructive reset, `git clean`, recursive shell deletion, rebase, or automatic conflict resolution.
- Deleting or modifying any stash.
- Closing, merging, rebasing, or deleting `feature/project-ops-core` / PR #1; its audited quarantine remains recoverable.
- Changing repository-wide Actions defaults, branch protection, visibility, collaborators, or any secret/credential other than the one dedicated deploy-key pair named in scope.
- Persisting or exposing the deploy key outside the private control repository's encrypted Actions secret after target validation.

## Failure Signal / Repro
- The completed merge is present at fork `main`, but four auxiliary worktrees and multiple completed/superseded branches still remain registered.
- The fork has custom commits on top of official history, so a simple fast-forward-only sync cannot keep it current after the next upstream commit; no scheduled upstream merge workflow currently exists. GitHub's native `GITHUB_TOKEN` also cannot update workflow files introduced by a future upstream delta because that conditional operation requires a permission the token cannot request.

## Root-Cause Hypothesis
- Facts: PRs #2 and #3 merged the consolidation as `1f4b3c1e5a`; current fresh refs show `main == maikolb/main`; `origin/main` is its ancestor; the primary and three obsolete auxiliary worktrees are clean, while the isolated delivery worktree contains only this contract update; PR #1 remains open and intentionally quarantined.
- Assumptions: the personal fork `maikolb/hermes-agent` is the only writable authority, while official upstream `NousResearch/hermes-agent` remains read-only.
- Chosen fix point: isolate a scheduled GitHub Actions workflow in a private control repository so upstream-synchronized workflows cannot read its fork-scoped deploy key, then remove only the delivery residue made obsolete by the confirmed main.

## Claim Discipline
- Facts already established: local and fork main are identical at `7e51ba5476`; official upstream `057dcdf236f8` is reachable from that commit; all obsolete cleanup candidates were clean and recovered before deletion. During cleanup, the active runtime branch gained unmerged commit `986c77afbd`; it was retained and protected remotely by `archive/hermes-20260824-active-runtime-986c77afbd` instead of being misclassified as obsolete.
- Inferences that still require validation: a future genuine upstream merge conflict will exercise the already-reviewed fail-closed path; manufacturing such a remote conflict is prohibited.
- Highest readiness state allowed by current evidence: released for the synchronization automation; the live Hermes runtime remains on its existing pinned checkout.
- Target readiness checklist or equivalent: both contracts green; control workflow syntax/behavior green; private repository and isolated secret verified; PR #4 merged; local/remote main confirmed; manual control run green; recovery tags and retained refs inventoried; final window verifier green.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Do not remove a worktree unless its status is clean and its resolved path matches the exact in-scope path.
- Do not delete a branch unless its release decision is documented and its recovery/retention condition is satisfied.
- Do not touch the open quarantined PR #1 branch or any stash.
- Do not start or restart Hermes to validate this Git-only maintenance task.
- Do not print, log, commit, reuse, or retain a local copy of the private deploy key after the Actions secret and target push are verified.

## Loop Control
- Controlled micro-loop is not required because no autonomous edit runner is used; the root agent will perform at most three explicit patch-validate-review iterations.
- Qualification: bounded manual spec -> build -> review -> green maintenance release.
- Maximum build/test/fix iterations: 3.
- Stop condition: workflow validation and target run are green, remote `main` is confirmed, obsolete auxiliary refs/worktrees are removed, and any branch retained solely by an active runtime is exact, clean and protected by a rollback tag.
- Escalation rule: stop before push or deletion on any unexplained commit, dirty worktree, merge conflict, failed ancestry gate, or third repeated validation failure.
- Runtime authority path: none.
- Append-only evidence path: not applicable; no autonomous loop is used, and Git/GitHub refs plus the workflow run are the delivery evidence.

## Validation Plan
- Analyze/lint: canonical validation of both execution contracts, `git diff --check`, control-workflow YAML/Bash parse, secret-isolation/host-key/permission review, and exact branch/worktree inventory.
- Unit tests: not applicable; no product code is changed.
- Integration/contract tests: local structural assertions cover exact source/target, empty native-token permissions and forbidden-operation absence; an ephemeral target branch proves the dedicated deploy key can update `.github/workflows/**` and is then deleted; a manual private-control dispatch exercises the real no-op sync, while conflict handling remains fail-closed without manufacturing a destructive remote conflict.
- Build/install/deploy checks: not applicable; install/deploy is prohibited.
- Target or environment checks: fresh fetch/`ls-remote`, deploy-key-authenticated ephemeral workflow-branch push/delete, fork evidence PR merge hash verification, and one manual private-control GitHub Actions dispatch.
- Delivery pipeline checks: PR status/mergeability plus resulting remote `main` and branch inventory.
- Manual smoke checks: Windows window verifier must report `VisibleWindows=0`; no Hermes UI/runtime smoke is authorized.

## Status
- Contract preflight: validated by the canonical global validator (phase `Preflight`) on 2026-08-24.
- Implementation: private `maikolb/hermes-upstream-sync` is published and active with target-only deploy key `161167502` and isolated secret; seven archive tags are remote; four obsolete auxiliary worktrees, nine obsolete local branches and fourteen obsolete remote branches were removed; no product code changed.
- Validation: both contracts, workflow YAML/Bash and independent structural/security review are green; ephemeral workflow-write probe `aa3febd867` was pushed/deleted; manual run `32739966897` succeeded in 14 seconds; PR #4 merged as `7e51ba5476`; local/fork main match and contain upstream; only PR #1 and four stashes remain quarantined. Live Hermes-linked processes triggered the safety gate; a concurrent unmerged commit advanced the clean runtime checkout to `986c77afbd`, so it was archived and retained rather than overwritten. Final zero-UI verification reported `VisibleWindows=0` and no shell descendants.
- Completion: completed at `released` for automatic synchronization and `validated-local` for Git cleanup. The transient closeout worktree exists only to publish this terminal record and is removed immediately after its commit reaches remote main; no Hermes runtime-release or acceptance claim is made.
