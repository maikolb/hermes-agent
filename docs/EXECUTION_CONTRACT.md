# NFOS P0-4a Local Candidate Execution Contract

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-PROD-REPAIR-20260903-P0-4A-LOCAL-v3
- Mode: REPAIR
- Risk Level: CRITICAL
- Canonical Checkout: `C:/Users/maiko/AppData/Local/hermes/hermes-agent`
- Target Branch: `fix/nfos-p0-4a` from fresh `maikolb/main`
- Accepted Base: `b89c5ca8af68e36a40af163c34da3af4532fc480`
- Machine Runtime Authority: `internal/ops/P0-4A_AGENT_LOOP_RUN.json`
- Scope Manifest: `docs/EXECUTION_CONTRACT.md.scope.json`
- Owner and Acceptance Authority: Maikol
- Single-Writer Executor: Codex
- Independent Reviewer: Claude
- Production Authorization: absent

## Requested Outcome
Implement and validate the smallest complete local P0-4a candidate for NFOS database safety, bounded backup, non-destructive archive planning, compact storage and profile-scoped checkpoint maintenance, without mutating any VPS, production database, service, symlink or release.

## Acceptance Criteria
- AC-001: The branch is based exactly on fresh `maikolb/main` and includes the three accepted production-to-main commits.
- AC-002: Every SQLite connection keeps `synchronous=FULL`; writer, pooled-reader and maintenance busy timeouts are role-specific and covered against the 20 s routine, 60 s transcript and 0.5 s activity paths.
- AC-003: Applicable connections read back `wal_autocheckpoint=4000`, with no durability downgrade.
- AC-004: Backup uses one bounded SQLite backup call, publishes by atomic replace only after success, preserves source mode where supported and leaves no final partial file on failure.
- AC-005: Archive eligibility excludes open and pinned sessions, compression ancestors of every retained session, recursive delegate closure, referenced shared prompts, disk transcript dependencies and session IDs referenced by external Kanban databases.
- AC-006: Source deletion is unavailable unless transparent list, resume, search and context retrieval across active and archive databases is implemented and proven. If the bounded candidate does not implement full retrieval, it must remain copy-only.
- AC-007: The existing compact-storage path executes no more than one VACUUM per invocation and a repeated already-compact run does not execute an unnecessary VACUUM.
- AC-008: The checkpoint maintenance surface is profile-scoped, bounded, lock-aware and not scheduled or activated by this branch.
- AC-009: A reproducible local runtime supply-chain document identifies a Python build linked to SQLite 3.50.0 or newer, including authoritative source URLs, artifact hashes and verification commands. No VPS installation occurs.
- AC-010: Focused and adjacent regression tests, changed-file lint/type checks, compile checks, contract/runtime/scope validators and `git diff --check` pass.
- AC-011: No test shows loss of context, memory, answer quality, role alternation or performance. Any statistically meaningful regression blocks `validated-local`.
- AC-012: The candidate diff changes only declared scope, configured GitHub authentication is available, and publication remains ordered after every local gate is green. Push, PR creation and remote readback are delivery actions performed only after this local contract reaches Final.
- AC-013: Production remains unchanged and readiness cannot exceed `validated-local`.

## Failure Signal / Repro
- Approved G0 evidence: `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/evidence/P0-4a/PREFLIGHT.md`.
- G0 observed a 4,602,044,416-byte `state.db`, SQLite 3.45.1, 22 bounded-window lock lines and 7 lease-refresh failures on the NFOS target.
- Current main supports only a partial database pragma set, uses writer timeout 1 s and pooled-reader timeout 5 s, and has application retry budgets of 20 s routine, 60 s transcript and 0.5 s activity.
- Existing archive is soft-hide and existing prune is destructive without a same-schema retrieval layer.
- Existing compact-storage already owns a VACUUM, so a second maintenance VACUUM would duplicate gigabyte-scale work.
- These are historical/current-baseline facts from approved G0 evidence. This local contract does not refresh or mutate the target.

## Root-Cause Hypothesis
- Large legacy dual-FTS storage amplifies write work and maintenance cost.
- Connection-local busy waits are not configured by role; a global 30 s wait would conflict with the deliberate short writer retry loop and activity deadline.
- SQLite 3.45.1 is inside the affected WAL-reset version range identified by the approved evidence.
- Existing deletion semantics do not preserve compression lineage, delegate closure, shared prompt/transcript dependencies or cross-database Kanban references.
- A safe repair must preserve FULL, use role-aware contention settings, make backup bounded, keep deletion disabled without transparent retrieval, and reuse exactly one existing compact-storage VACUUM.

## In Scope
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/archive/execution-contracts/CEOGAME_NATIVE_MEDIA_DURABLE_DELIVERY_20260825.md`
- `docs/archive/execution-contracts/FAILED_COMPACTION_SAFETY_STOP_20260902.md`
- `docs/runtime-supply-chain/NFOS_SQLITE_RUNTIME.md`
- `internal/ops/P0-4A_AGENT_LOOP_RUN.json`
- `internal/ops/P0-4A_AGENT_LOOP_EVENTS.jsonl`
- `internal/ops/aof_runtime/`
- `internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md`
- `hermes_state.py`
- `hermes_cli/config_defaults.py`
- `hermes_cli/main.py`
- `hermes_cli/sessions_cmd.py`
- `hermes_cli/backup.py`
- `scripts/nfos_state_maintenance.py`
- `scripts/nfos_checkpoint_job.py`
- `tests/state/test_nfos_database_pragmas.py`
- `tests/hermes_state/test_nfos_archive_safety.py`
- `tests/hermes_cli/test_nfos_archive_cli.py`
- `tests/hermes_cli/test_backup.py`
- `tests/scripts/test_nfos_state_maintenance.py`
- `tests/scripts/test_nfos_checkpoint_job.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/evidence/P0-4a/LOCAL_IMPLEMENTATION_VALIDATION.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/change-packets/P0-4a/G1_CHANGE_PACKET.md`

## Out of Scope
- Any SSH, VPS, production DB, service, process, systemd, symlink, release or credential operation.
- Repointing `/usr/local/bin/hermes` or implementing global release activation.
- Homolog, `/lux`, worker-capacity activation, dispatcher, lease redesign or principal-worker parity.
- `synchronous=NORMAL` or any durability downgrade.
- Deleting sessions without complete dependency closure and transparent operational retrieval.
- New core model tools, broad refactors, new databases, daemons or schedulers.
- Removing FTS triggers as part of P1-1.

## Forbidden Actions
- Do not contact or mutate production.
- Do not build or install a runtime on the VPS.
- Do not restart, stop, start or signal any service or process.
- Do not repoint any symlink or edit an immutable release.
- Do not read or expose credentials, message bodies or personal data.
- Do not stash, reset, discard or overwrite unrelated work.
- Do not push or open a PR while any local gate is red.
- Do not expose a destructive archive flag unless transparent retrieval and the complete protected closure are tested.

## Claim Discipline
- `implemented` means the candidate branch contains the bounded local source and tests.
- `validated-local` requires every objective local gate in this contract plus exact diff/scope validation.
- `validated-target` requires later approved evidence from the NFOS production interface and cannot be claimed here.
- `released` and `accepted` are impossible under this contract.
- Internal tests do not substitute for the later Telegram canary.

## Loop Control
- Controlled micro-loop is required because this is a high-risk, multi-file state-management repair.
- Runtime authority path: `internal/ops/P0-4A_AGENT_LOOP_RUN.json`.
- Append-only evidence path: `internal/ops/P0-4A_AGENT_LOOP_EVENTS.jsonl`.
- Maximum implementation/test iterations: 10.
- Maximum materially different environment attempts: 2.
- Retry requires a source/test delta or a fresh observation.
- Stagnation stop: the same failure with the same code and checker twice.
- Stop conditions: dirty preflight, non-fast-forward main, scope violation, runtime validator failure, need for external mutation, inability to keep deletion disabled without retrieval, unbounded backup, durability regression, performance/context/memory/quality regression or unavailable configured GitHub authentication.
- Rollback: revert only the uncommitted candidate edits by an explicit reviewed patch; never use reset, checkout-discard or stash.

## Validation Plan
- Validate this frozen contract, runtime definition and scope manifest before source edits.
- Inspect existing connection, retry, session, prompt, transcript, Kanban, backup, compact-storage and cron surfaces before choosing edit points.
- Implement the minimum role-specific pragmas while preserving writer timing and FULL.
- Add a bounded single-call backup helper with atomic publication and cleanup on failure.
- Implement dependency-closed archive planning first. Implement transparent reads only if the change remains bounded; otherwise ship copy-only archive creation with deletion mechanically unavailable.
- Reuse the existing compact-storage implementation and prove one-or-zero VACUUM behavior as appropriate.
- Add a profile-scoped checkpoint script without registering a schedule.
- Document a reproducible SQLite 3.50+ runtime source with hashes and local verification.
- Run focused tests, adjacent session/backup/compact-storage regressions, Ruff or the repository's changed-Python lint, Python compilation, scope alignment, runtime validation, contract validation and diff checks.
- Update append-only local evidence and coordination artifacts without target claims.
- Commit, push and create a PR only after all local checks pass; read back PR and CI state.

## Validation Evidence
```json
{
  "schemaVersion": 1,
  "checks": [
    {
      "criterionId": "AC-001",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Git branch and ancestry inspection",
      "target": "canonical checkout before source edits",
      "procedure": "Fetched maikolb/main, switched canonical main, fast-forwarded only, then created fix/nfos-p0-4a.",
      "expected": "Clean branch at fresh accepted main.",
      "observed": "Branch fix/nfos-p0-4a at b89c5ca8af68e36a40af163c34da3af4532fc480; prior checkout was clean.",
      "performedAtUtc": "2026-09-03T23:43:30Z",
      "artifacts": [{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-002","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Focused pragma and lock-patience tests","target":"writer, reader and maintenance SQLite connections","procedure":"Ran role readbacks and existing 20 second routine, 60 second transcript and activity-path regression coverage.","expected":"FULL with role-specific waits and unchanged application patience behavior.","observed":"FULL=2, writer=1000 ms, reader and maintenance=30000 ms; focused and patience tests passed.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-003","status":"passed","performedBy":"agent","verificationMode":"direct","method":"SQLite pragma readback","target":"all three connection roles","procedure":"Applied defaults to isolated databases and read back synchronous and wal_autocheckpoint.","expected":"FULL and 4000 pages.","observed":"Each role returned synchronous 2 and wal_autocheckpoint 4000.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-004","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Continuous-writer backup and failure tests","target":"hermes_cli.backup._safe_copy_db","procedure":"Backed up under a continuous WAL writer and injected a bounded busy failure over an existing destination.","expected":"One pages=-1 operation, verified atomic output and prior-good preservation.","observed":"Backup verified; failure removed only hidden partial and preserved the prior destination.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-005","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Archive dependency fixtures","target":"SessionDB.plan_physical_archive","procedure":"Created retained lineage, delegate, shared prompt, disk transcript and indexed or unindexed external reference fixtures.","expected":"Every dependency protected and unindexed external lookup blocks.","observed":"Only the independent ended fixture remained eligible; every required dependency was protected.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-006","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Public surface and before-after retrieval test","target":"nfos_state_maintenance archive-copy","procedure":"Inspected parser and callable API, created an archive copy, then compared active list, export and search results.","expected":"No deletion surface and unchanged active retrieval.","observed":"Deletion is absent and all compared active retrieval results were equal.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-007","status":"pending","performedBy":"agent","verificationMode":"direct","method":"Real legacy database compact-storage tests","target":"existing optimize_fts_storage VACUUM owner","procedure":"Replace the invalid fake db.vacuum counter with a real v22 legacy database test and fail closed unless optimize_fts_storage reports ok=True and vacuumed=True.","expected":"A real required compaction confirms VACUUM; vacuumed=False fails; an already-compact run requests no VACUUM.","observed":"The earlier validated-local evidence was invalidated by implementation review. Correction gates are in progress.","performedAtUtc":"2026-09-04T00:00:00Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"pending-after-corrections"}]
    },
    {
      "criterionId":"AC-008","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Profile mismatch and bounded checkpoint tests","target":"scripts/nfos_checkpoint_job.py","procedure":"Ran PASSIVE checkpoint on a matching temp profile and attempted a mismatched profile.","expected":"Bounded matching profile succeeds; mismatch fails closed; no schedule created.","observed":"Matching checkpoint completed within bound, mismatch raised, and no scheduler code changed.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-009","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Publisher API, immutable manifest and two-sided hash inspection","target":"Astral CPython 3.13.15 Linux x86_64 artifact","procedure":"Read release metadata, build manifest and publisher digest, downloaded once and independently hashed locally.","expected":"Matching SHA-256 and declared SQLite at least 3.50.0.","observed":"Both artifact hashes matched; manifest pins SQLite 3.53.1.0 and its source hash.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-010","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Focused tests, adjacent tests, Ruff, compile and diff validation","target":"declared changed Python and governance files","procedure":"Ran recorded focused and adjacent commands, validators, py_compile, Ruff and git diff check.","expected":"Objective gates green with unrelated baseline failures identified rather than hidden.","observed":"All objective gates passed; adjacent diagnostic was 305 passed, 7 skipped and 3 pre-existing Windows assertion mismatches outside the delta.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-011","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Retrieval equality and adjacent conversation-loop regression tests","target":"active list, export, search, compaction deferral and lock behavior","procedure":"Compared pre and post archive-copy read results and ran accepted-main compaction plus state regressions.","expected":"No loss or regression in covered context, persistence or timing invariants.","observed":"Read results were equal and no objective regression test failed.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-012","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Scope alignment, remote and authentication preflight","target":"candidate diff and maikolb remote","procedure":"Validated all changed paths against the manifest, inspected the maikolb URL and queried configured GitHub authentication without printing credentials.","expected":"Exact scope and publication authority ready after local Final.","observed":"26 changed files fit 21 declared scope paths; maikolb remote and authenticated account are configured.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    },
    {
      "criterionId":"AC-013","status":"passed","performedBy":"agent","verificationMode":"direct","method":"Command and scope audit","target":"external target boundary","procedure":"Reviewed executed commands and changed paths for SSH, service, database, release or symlink operations.","expected":"No production contact or mutation and no target-validation claim.","observed":"No SSH or production command was issued; all writes are local candidate or coordination artifacts.","performedAtUtc":"2026-09-03T23:43:30Z","artifacts":[{"path":"internal/ops/evidence/P0-4A_LOCAL_VALIDATION.md","sha256":"3b560a02a1c7bfb9cc14c5e06f747ba3d6753da066d597c1c7f2968607190ed1"}]
    }
  ]
}
```

## Status
- Contract preflight: must be rerun with the canonical external AOF runtime after corrections.
- Runtime Definition and scope alignment: must be regenerated and rerun after removal of the vendored runtime.
- Implementation: correction work is in progress, with archive deletion disabled.
- Validation: the earlier `validated-local` claim is invalidated. Current readiness is `implemented`.
- Completion: pending correction gates and required CI.
- Publication: PR #79 exists; correction commits are not yet pushed.
- Production: unchanged and forbidden.
- Readiness ceiling after all local gates and required CI pass: `validated-local`.
