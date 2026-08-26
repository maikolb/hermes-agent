# Hermes Runtime Convergence and Safe Promotion Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Phase: Validation
- Workspace: `C:\Users\maiko\AppData\Local\hermes\hermes-agent`
- Target Branch: `main` via a temporary PR branch deleted after merge
- Updated At: 2026-08-25T23:55:00-03:00
- Runtime Targets: Windows Titan/default and Hostinger VPS profiles `default` and `hermes-project-factory`
- Machine Runtime Authority: none: promotion uses bounded explicit Windows and Hostinger lifecycle commands with per-target rollback; no autonomous machine loop owns the change
- Authority Reconciliation: the state file selects the live PID; imported module origin/hash and the Hostinger systemd process path must corroborate its SHA before any promotion claim
- Repair Evidence Artifact: `docs/release-evidence/HERMES_RUNTIME_CONVERGENCE_20260825.md`

## Requested Outcome
Converge the working Windows and Hostinger VPS Hermes runtimes onto one immutable, tested fork `main` release without degrading sessions, profiles, worker rotation, visible activity, reasoning/progress, delivery, memory, zero-UI behavior, or Project OS boundaries.

## Acceptance Criteria
- The pre-change Windows operational baseline `aac2ff879ca44ec3a5b77269eabfa423388b566d` and VPS rollback release `986c77afbd96254977e9e5d8592e55116527c11c` are recorded with process, config and gateway-critical file evidence.
- The candidate begins from fork remote `main` `c7015aadf8b8daae20cd42f7a8956f9487ebc980` and preserves the validated worker-focus behavior.
- Runtime identity is derived from the actually imported immutable release, not an unrelated Git checkout under `HERMES_HOME`.
- Windows and both VPS profiles load the same merged `main` SHA from immutable release directories.
- `show_reasoning`, `show_commentary`, `thinking_progress`, `reasoning_effort: high`, `busy_input_mode: steer`, activity indicators and `worker_focus_handoff` remain effective where configured.
- Focused worker/activity/identity/session/delivery/Windows tests pass, followed by the repository suite and syntax/diff gates proportionate to the change.
- Canary startup proves new PID, correct imported module origins/hashes, connected configured adapters and no visible Windows UI before replacing another runtime.
- A real worker-focus cycle proves first worker, rotation to the next worker, elapsed activity, live sanitized output, and cleanup at zero workers.
- The source PR is green and merged; the temporary local and remote branch is deleted; Hermes ends with only `main` and no stash.

## In Scope
- Read-only baseline capture for the live Windows and canonical Hostinger VPS runtimes.
- An isolated candidate based on the current fork remote `main`.
- A minimal correction to runtime identity/release metadata if the existing source reproduces the false VPS SHA.
- Existing immutable release/install/restart mechanisms and bounded canary promotion.
- Focused and full validation, regression record if a source defect is changed, PR/merge and branch cleanup.
- Updating the canonical execution contract and convergence evidence.

## Out of Scope
- Deleting sessions, profiles, memories, Kanban data, credentials, media, projects or message history.
- Stashing or modifying active Nexa Factory OS, Intake Hub, AOF, Honcho or Project OS worktrees.
- Cutting Hermes over to the extracted Project OS package during this promotion.
- Removing the Hermes fork, rewriting its history, force-pushing, broad rollback, or deleting unmerged work.
- Changing bot identities, Telegram/WhatsApp routing, model choice, reasoning visibility or user-facing progress policy.
- Deploying to any VPS other than canonical Hostinger `root@187.127.60.126`.

## Failure Signal / Repro
- Hostinger services import gateway code from release `986c77af`, while both state files report `code_sha=8f6dfb90`; gateway-critical blobs prove the identities differ.
- Windows state reports `aac2ff87`, while its editable install points at a checkout later advanced to `9477bfdc`, so the process is not an immutable tree.
- Fork remote `main` is `c7015aad`; neither live target has loaded it.
- Evidence artifact: `C:\Users\maiko\AppData\Local\hermes\gateway_state.json` records the live Windows PID and stamped source identity; process/service/import/hash probes corroborate it directly.

## Root-Cause Hypothesis
- VPS identity discovery resolves a Git repository associated with `HERMES_HOME` or working directory instead of the imported release root.
- Windows is launched from an editable development installation, allowing on-disk checkout movement after process start.
- Deployment and state stamping do not share one immutable release manifest, permitting a healthy process to advertise a different SHA.
- The worker-focus feature itself is not the root cause: its base rotation exists in `986c77af`, while `c7015aad` adds elapsed/live worker projection.

## Claim Discipline
- `implemented`: candidate and any minimal identity fix exist on the temporary branch.
- `validated-local`: focused and full local gates pass against the isolated candidate.
- `validated-target`: canary and real runtime smokes pass on Windows and Hostinger.
- `released`: both targets load the exact merged `main` SHA and rollback remains available.
- `accepted`: Maikol confirms the user-visible behavior in the normal channels.

## Forbidden Actions
- No stash, force-push, destructive reset, history rewrite, secret output, visible console/browser/dialog, or unbounded recursive delete/move.
- Do not edit the live editable Windows source before it is protected by an immutable rollback/candidate boundary.
- Do not restart an active gateway until idle/recoverable state and rollback are proved.
- Do not promote after a red gate, identity mismatch, missing adapter, session-state discrepancy or unexpected config delta.
- Do not touch Ceogame or infer a VPS target from historical context; only the declared Hostinger target is authorized.

## Rollback
- Windows: restore/start the immutable package built from `aac2ff879ca44ec3a5b77269eabfa423388b566d` with unchanged profile data and verify the prior PID/config/channel state.
- VPS: repoint `/usr/local/bin/hermes` and both systemd services to `/usr/local/lib/hermes-agent.release-20260824-986c77af`, daemon-reload only if the unit changed, and restart only the failed canary/profile.
- Source: revert only the convergence PR through a normal PR if the merged source is defective; never reset unrelated history.
- Candidate/build directories may be removed only after their resolved paths are proved inside the dedicated release/staging root and rollback artifacts are retained.

## Loop Control
- A controlled autonomous micro-loop is not required because each candidate build, test, canary, lifecycle change and rollback has one bounded deterministic command and one immediate target probe; retries require changed evidence.
- Maximum three materially different repair iterations per failing gate; an unchanged third failure stops the run.
- Every retry requires new evidence or a narrower hypothesis.
- Green condition: immutable identity, focused/full tests, canaries, adapters, worker rotation/activity and zero-UI all pass.
- Escalation: stop on required credential change, destructive data/schema action, inability to establish idle/recoverable state, or any mismatch between the candidate and preserved profile/session stores.

## Validation Plan
### Phase 1 — Preflight
- Validate this contract, zero-UI guard, exact Git/remote/branch state, live PIDs, imported origins, gateway-critical hashes and configs.
- Record rollback commands and prove both baseline commits remain addressable without changing runtime.

### Phase 2 — Isolated Candidate
- Fetch remote refs without pruning, materialize `c7015aad` outside the live import path, and reproduce the identity mismatch deterministically.
- Apply only the smallest source/release metadata fix required; add focused regression tests and evidence.

### Phase 3 — Local Gates
- Run worker-focus/activity, runtime identity, gateway state, session/checkpoint, Telegram/WhatsApp delivery and Windows zero-UI tests.
- Run the repository suite appropriate to the changed surfaces, `compileall`, `git diff --check`, exact-tree and secret scans.

### Phase 4 — Target Canary and Promotion
- Confirm idle/recoverable state, create immutable releases, start one bounded canary per OS, and prove PID/origin/SHA/config/adapters/visible-window state.
- Exercise worker rotation and delivery without changing existing profile/session databases; promote the remaining profiles only after the canary is green.
- Perform native channel/readback checks and leave rollback releases intact.

### Phase 5 — PR and Closeout
- Push the temporary branch, open PR, wait for required checks, merge, prove ancestry, update local `main`, delete the temporary branch locally/remotely and verify remote `main` is the only Hermes head.
- Revalidate this contract and publish the evidence artifact with readiness no higher than directly proved.

## Status
- Preflight and isolated local canary completed. Promotion is blocked until the repository and OS-specific CI gates are green.
