# Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: RELEASE
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824`
- Updated At: 2026-08-24T09:25:00-03:00
- Machine Runtime Authority: none: the installed Hermes runtime and all external systems remain read-only

## Requested Outcome
- Reconcile every completed local Hermes change with current upstream, preserve the user-accepted runtime behaviors, publish a clean and recoverable `main` on `maikolb/hermes-agent`, and add a fail-closed lifecycle that prevents future completed tasks from leaving unmerged branches or worktrees.

## Acceptance Criteria
- Every one of the 42 local commits is classified by behavior, files, chronological successors, supersession and release decision; no commit is accepted merely because it is newer.
- Every textual Git conflict has a recorded resolution tied to an intended behavior and a verification command.
- The current installed runtime, profiles, bindings, Kanbans and relevant logs are inventoried read-only and sanitized; no token, phone number, message body or other secret/PII is committed.
- A traceability matrix covers at least Telegram Project OS, project/topic provisioning, Kanban operations, conversational context on mention, parallel steer/workers, activity/focus display, Git/PR/merge/deploy closure, Kanban table rendering, scope/AOF guardrails, Honcho/Claude TL/Codex Loop/AIRC/Grok/ai-memory workflow, safe task phrasing, Read.ai-to-spec flow and WhatsApp raw→Hermes PF→curated→Jira intake.
- Foreign responsibilities are identified explicitly: no Titan, DarkFactory, AOF product source or external Intake Hub implementation is silently absorbed into Hermes core.
- The candidate passes syntax/static checks, focused tests for every touched subsystem, the broad repository suite available on this machine, deterministic semantic replays and failure-injection cases without reading or mutating the live Hermes home.
- Future task completion is fail-closed unless its owned branch is committed, pushed to the personal fork, represented by a PR, green, merged, verified reachable from remote `main`, and its worktree is then removed safely; incomplete/quarantined work remains preserved and reported, never auto-merged or deleted.
- Immutable rollback refs preserve the prior fork `main`, the pre-merge local integration head and every incomplete/quarantine stash before any branch deletion.
- The personal fork is the only write remote. `origin` (`NousResearch/hermes-agent`) remains read-only and no force-push is used.
- The candidate is published through a PR and GitHub checks when available. `main` may advance only after local/replay and remote gates are green and is not installed or deployed by this contract.
- All retained worktrees are clean. A worktree or branch is removed only after its exact commit is proven reachable from a confirmed remote ref.
- Final readiness is claimed precisely: Git publication can reach `validated-local`; the live runtime remains on its previous pinned version until a separately authorized target canary earns `validated-target`, `released` and `accepted`.
- The final execution-contract validation and Windows verifier report success with `VisibleWindows=0`.

## In Scope
- `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824\**`
- Read-only comparison against `C:\Users\maiko\AppData\Local\hermes\hermes-agent\**` and other registered Hermes worktrees/profiles.
- Git refs, branches, stashes and worktree metadata belonging to `maikolb/hermes-agent`, with deletion allowed only under the reachability criterion above.
- Remote `maikolb` for non-force branch push, PR, merge and hash verification.
- Remote `origin` for fetch/read-only ancestry comparison.
- `docs\EXECUTION_CONTRACT.md`
- `docs\release-evidence\HERMES_MAIN_CONSOLIDATION_20260824.md`
- The minimum core source and tests required to enforce the branch/worktree→PR→merge completion lifecycle, after its actual ownership path is identified in source.

## Out of Scope
- Installing, restarting, stopping, reconfiguring or pointing any Hermes, Titan, DarkFactory, Telegram, WhatsApp, Jira, cron, Honcho, AIRC or ai-memory runtime at the candidate.
- Mutating Telegram topics/groups, WhatsApp chats, Jira issues/projects, Kanban data, Read.ai meetings, production repositories or deployments.
- Copying source code from `C:\Users\maiko\Projetos\hermes-intake-hub` into Hermes core; the hub remains a separate project.
- Copying Titan, DarkFactory or canonical AOF implementation into Hermes core. Core may keep only generic integration contracts/guardrails whose ownership is proven.
- Applying, dropping or rewriting quarantined, incomplete or unidentified stashes without an independent acceptance decision.
- Force-push, destructive reset/checkout, `git clean`, blind recursive deletion, or deleting any branch/worktree lacking confirmed remote recoverability.
- Treating existing tests, commit age, agent assertions or a successful process exit as sufficient proof of behavior.
- Claiming the live runtime is validated, released or accepted as a result of Git-only consolidation.

## Failure Signal / Repro
- The personal fork `main` is thousands of upstream commits behind while 42 local commits span multiple feature families and several branches/worktrees; a direct merge produces conflicts in core runtime paths such as `gateway/run.py`, `plugins/platforms/telegram/adapter.py`, guardrails, Honcho and Kanban.
- Repeated intake rounds were previously called ready based on component tests but failed autonomously in the real target; therefore test-green without a semantic replay/target evidence chain is an explicitly rejected release signal.
- Completed work was left distributed across integration branches and worktrees instead of being closed through personal-fork PRs, causing uncertain ancestry and delayed integration.
- Existing evidence includes `docs/regressions/REG-2026-08-23-003.md` plus the Git graph, reflogs, registered worktrees and current conflict stages.

## Root-Cause Hypothesis
- Facts: isolation worktrees were created for concurrent work; their lifecycle did not require verified PR/merge closure; upstream advanced by thousands of commits; 42 local commits include multiple independent behaviors; current runtime has not been switched to this candidate.
- Assumptions: the user-accepted runtime behavior and current external data are the comparison baseline, but neither historical tests nor memory alone is authoritative.
- Chosen fix point: audit and reconcile behavior at the Git integration boundary, add semantic replay evidence, publish only through the personal fork, and make PR/merge/reachability/worktree cleanup a fail-closed completion contract.

## Claim Discipline
- Facts already established: the 42-commit local range was consolidated; official upstream `057dcdf236f8` was merged in full; the bounded final floor matrix passed 618 Python tests with 47 declared conditional skips and zero remaining failures; PR #2 merged as `c4daea6b51`; rollback refs are remote; the live runtime remains unchanged and clean at `66c2257b9f`.
- Inferences that still require validation: whether the Git candidate preserves every user-observed behavior in the live target and whether the separately configured external integrations remain healthy after a future deployment.
- Highest readiness state allowed by current evidence: validated-local.
- Target readiness checklist or equivalent: a separately authorized canary must deploy the pinned release, observe the natural Telegram/intake behaviors and earn `validated-target`, `released` and `accepted`; no such claim is made here.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- No use of manual intervention as proof that an autonomous workflow works.
- No resolution based only on “ours”, “theirs” or newest timestamp; behavior and evidence decide each conflict.
- No merge of Titan, DarkFactory, AOF product source or Intake Hub internals into Hermes core to make tests pass.
- No deletion of a worktree, branch or stash until its exact contents and recovery ref are proven.
- No installation or deployment of the candidate under this contract.

## Loop Control
- Controlled micro-loop is not required because this release is an explicitly reviewed Git reconciliation and no autonomous edit runner is authorized.
- Qualification: bounded manual spec→build→review→green release consolidation.
- Maximum build/test/fix iterations: 3 per independently identified failure class.
- Stop condition: all acceptance rows required for Git publication are green, remote refs/hashes are confirmed and no unresolved conflict or unexplained behavior remains.
- Escalation rule: after three failed iterations for one root cause, any unproven destructive cleanup, or any unresolved semantic conflict, stop before merging/pushing `main` and report the exact blocker.
- Runtime authority path: none; only the root agent may edit the candidate and no live-runtime mutation is authorized.
- Append-only evidence path: `docs/release-evidence/HERMES_MAIN_CONSOLIDATION_20260824.md`.

## Validation Plan
- Analyze/lint: conflict-marker scan, `git diff --check`, AST/compile checks, ancestry/range-diff, secret/path/boundary scan and per-commit/per-conflict ledger.
- Unit tests: all focused suites mapped to each retained behavior, including guardrails, compression, workers/leases, topic/project routing, activity indicator, delivery recovery, Honcho scoping, Windows zero-UI and passive intake transport.
- Integration/contract tests: repository canonical runner plus deterministic event replays for topic→project→Kanban→worker, steer/focus, completion lifecycle and raw→curated→Jira checkpoint without live egress.
- Build/install/deploy checks: package/import/build checks in the isolated worktree; install/deploy is prohibited.
- Target or environment checks: read-only source/runtime hash and configuration comparison; live target acceptance is deferred to a separately authorized canary.
- Delivery pipeline checks: push candidate to `maikolb`, open PR, inspect checks, merge without force only if green, verify remote `main` hash and rollback refs.
- Manual smoke checks: user acceptance is required later for live Telegram/intake behavior; this contract cannot claim it. Windows verifier must report `VisibleWindows=0` now.

## Status
- Contract preflight: validated on 2026-08-24 with the canonical global validator (phase `Preflight`).
- Implementation: completed on the isolated release branch; official upstream merged with one resolved test-only conflict and one evidence-backed recovery fallback.
- Validation: 618 Python tests passed, 47 conditional skips, Node bridge command exit 0, PR mergeability clean; the fork exposes no GitHub checks, so no remote CI result exists to claim.
- Publication: PR #2 merged into `maikolb/main` as `c4daea6b51`; candidate and official upstream are both proven ancestors; rollback/release tags are remote.
- Completion: completed for Git consolidation at `validated-local`; installation, runtime reload and target acceptance remain explicitly out of scope.
