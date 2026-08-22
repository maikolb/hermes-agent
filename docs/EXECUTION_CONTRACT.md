# Hermes Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: IMPLEMENT
- Risk Level: HIGH
- Workspace: C:\Users\maiko\Projetos\hermes-aof-route-runtime-20260821
- Updated At: 2026-08-21T22:00:00-03:00
- Machine Runtime Authority: none: operator-driven bounded foreground source, test, integration, and cutover sequence; no autonomous, scheduled, resumable, concurrent, or unattended agent loop
- Discovery Route Authority: C:\Users\maiko\.codex\GLOBAL_DISCOVERY_PROMOTIONS.json

## Requested Outcome
- Preserve Hermes as an independent runtime while integrating the proven guard-state repair into the current branch and validating that its existing plugin narrow waist can enforce the external AOF route authority before dispatch and observe outcomes afterward.

## Acceptance Criteria
- `ToolCallGuardrailController` initializes and resets redirect state deterministically; one structural failure redirects only the equivalent signature/tool and leaves declared alternatives usable.
- A standalone plugin installed from AOF, not policy logic embedded in Hermes core, is discovered through a real temporary `HERMES_HOME` and receives exactly one `pre_tool_call` and one terminal `post_tool_call` per dispatch path.
- A matched AOF deny/redirect decision prevents the underlying tool dispatcher from running and returns bounded alternatives to the agent.
- A non-match and an unrelated tool preserve existing behavior byte-for-byte at the dispatch boundary.
- Hook/plugin errors do not expose secrets or corrupt unrelated tool calls; deterministic safety-policy authority corruption is reported as a bounded block only for the configured policy surface.
- Focused tests run through `scripts/run_tests.sh`, compile/diff checks pass, and the final current-branch integration is clean before any live checkout update or reload.

## In Scope
- `docs/EXECUTION_CONTRACT.md`, `docs/REVISION_PROTOCOL.md`, `agent/tool_guardrails.py`, its focused tests, and narrowly relevant generic plugin-dispatch tests or documentation if the existing seam needs conformance coverage.
- Git commit/merge of the validated isolated candidate into `integrate/local-runtime-v2-20260820` after AOF adapter tests are green.
- Read-only inspection of the live checkout and controlled reload through its existing hidden task/launcher after idle and rollback checks.

## Out of Scope
- Copying AOF registry schemas, route matching, precedence, or policy content into Hermes core.
- Modifying model-visible tool schemas, prompts, channels, credentials, providers, business state, unrelated config, or the plugin framework architecture.
- Refactoring the agent loop, adding dependencies, force-pushing, rewriting history, or reloading an active profile.

## Failure Signal / Repro
- `ToolCallGuardrailController.before_call()` references `_redirected_signatures` and `_redirected_tools` although construction/reset did not guarantee those fields.
- Without a loaded AOF plugin, `search_files` reaches the normal dispatcher before the cross-session promotion authority can deny or redirect it.

## Root-Cause Hypothesis
- Facts: the redirect maps are missing from the controller's reset invariant; Hermes already invokes `resolve_pre_tool_block()` before dispatch and emits `post_tool_call` after terminal outcomes.
- Assumptions: no Hermes core feature is required for AOF semantics; source integration should be limited to the independent guard regression and conformance tests for the existing plugin boundary.
- Chosen fix point: restore the controller invariant, exercise real plugin discovery/dispatch, and leave all AOF policy evaluation in the separately versioned adapter.

## Claim Discipline
- Isolated source/tests: at most `validated-local`.
- Current live checkout commit integration: `released-source`, not runtime validation.
- Each profile: `validated-target` only after a new runtime loads the plugin and passes actual block/outcome/health/zero-UI probes.

## Forbidden Actions
- No live source mutation or reload before the isolated branch is green and recovery state is recorded.
- No visible console/browser/editor/dialog, direct `.cmd`/`.bat` launcher, credentials in output, or reload while work is active.
- No duplicated AOF policy implementation in Hermes.
- No broad formatting/refactor, dependency changes, force-push, destructive reset, or deletion of user state.

## Loop Control
- Controlled micro-loop qualification: not required because this is a single-writer foreground implementation with deterministic gates; each profile reload is a separately reconciled one-shot action after idle proof.
- Maximum iterations: 3 per focused gate.
- Green condition: focused controller tests, real plugin discovery/dispatch integration, affected Hermes suite, compile, diff, idle/readiness checks, hidden reload, health, target policy probe, and `VisibleWindows=0`.
- Escalation: stop if hook order is not truly pre-dispatch, the live branch diverged unexpectedly, a profile cannot drain, or rollback identity cannot be proven.

## Validation Plan
- Analyze/lint: `py_compile`, `git diff --check`, exact diff and import-boundary review.
- Unit tests: constructor/reset invariants, structural redirect equivalence, alternatives remain usable, no cross-tool contamination.
- Integration/contract tests: use `scripts/run_tests.sh`; temporary real `HERMES_HOME` plugin discovery; actual dispatcher is not called on block and is called once on non-match; post hook records terminal outcome.
- Build/install/deploy checks: clean isolated commit, merge into current integration branch, target plugin checksum/config fingerprint, rollback commit/config backups.
- Manual smoke checks: profile idle/drain proof, hidden canonical reload, PID/health/channel check, target pre-dispatch/outcome probes, zero-visible-UI verifier.

## Status
- Contract preflight: validated for this isolated current-base worktree.
- Implementation: redirect-state initialization/reset and replay gates are ported onto the current integration base; AOF policy semantics remain outside Hermes core.
- Validation: focused 6/6 suite and disposable real-plugin dispatch are green; current-branch integration and target profile reload remain pending.
- Completion: pending.
