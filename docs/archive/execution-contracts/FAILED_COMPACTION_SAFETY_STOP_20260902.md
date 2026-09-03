# Failed-Compaction Safety Stop

## Contract Metadata
- Contract Version: 3
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Documents\Codex\2026-09-02\hermes-compaction-safety`
- Target Branch: `fix/compaction-failure-stop`
- Updated At: 2026-09-02T17:05:00Z
- Machine Runtime Authority: none: one bounded core repair with behavior tests and no live deployment before merge
- Authorisation: Maikol requested that every Hermes stop unsafe continuation after failed compaction, preserve quality and memory, and be deployed only after direct proof.

## Requested Outcome
- When failed compaction leaves a Hermes request above its configured threshold, preserve its durable turn checkpoint and stop before another ordinary model call, without substituting a lower-quality summary or weakening memory.

## Acceptance Criteria
- AC-001: Current `maikolb/main` reproduces an over-threshold request blocked by the same-session compression failure cooldown that would otherwise continue to the provider.
- AC-002: The repaired path emits an actionable terminal response and makes zero provider calls for that unsafe request.
- AC-003: The existing durable turn checkpoint is retained and marked for resumable recovery before the turn closes.
- AC-004: Successful compression and requests below threshold retain their current behavior.
- AC-005: No conversation content, recent message, memory hook, compression timeout, summary model, or AOF enforcement is removed or weakened.
- AC-006: The turn cannot spin through tools or consume another model iteration while the failed-compaction cooldown blocks an over-threshold request.
- AC-007: Focused behavior tests, adjacent compression/checkpoint tests, lint, and diff checks pass on the exact branch.
- AC-008: The regression is recorded in `docs/regressions` with incident evidence, prevention, and readiness ceiling.

## Failure Signal / Repro
- `C:\Users\maiko\AppData\Local\hermes\logs\gateway-hidden-launch.log` records a preflight above 231,200 tokens, a 300-second auxiliary compression timeout, exhausted fallback authentication, continuation at approximately 266,692 tokens during cooldown, then 121-second and 303-second tool failures.
- User screenshot: `C:\Users\maiko\AppData\Local\Temp\codex-clipboard-bfd7c908-761a-480c-9f89-96b6e1ae6eb5.png`.
- Repository evidence: `internal/ops/evidence/failed-compaction-safety-stop-20260902.log`.

## Root-Cause Hypothesis
- Fact: `agent/turn_context.py` and `agent/conversation_loop.py` only warn when the same-session failure cooldown blocks compression above threshold.
- Fact: the turn checkpoint is initialized before preflight compression, and raw session history remains durable when `abort_on_summary_failure` preserves the input list.
- Fact: changing `abort_on_summary_failure` to false would substitute a deterministic fallback summary and is excluded because quality parity has not been proven.
- Chosen fix point: the existing pre-provider pressure boundary and existing turn checkpoint state, using a terminal safety stop instead of another oversized model call.

## In Scope
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `agent/turn_context.py`
- `agent/conversation_loop.py`
- `agent/turn_checkpoint.py` only if the existing checkpoint API lacks a safe terminal transition
- focused tests under `tests/agent/`
- focused tests under `tests/run_agent/`
- `docs/regressions/`
- `internal/ops/evidence/`

## Out of Scope
- Changing compression threshold, timeout, provider, model, summary prompt, fallback credentials, or `abort_on_summary_failure`.
- Discarding history, disabling memory, reducing recent-message protection, or silently accepting a fallback summary.
- Gateway auto-reset architecture, a new daemon, a new agent, or unrelated session work.
- Live profile deployment before PR CI, merge, and merged-SHA readback.

## Forbidden Actions
- Do not make an oversized provider request after the guard proves compaction is blocked.
- Do not claim continuity from a warning alone; require checkpoint state and zero provider calls.
- Do not ask the user to be the first tester.

## Claim Discipline
- `implemented` means the branch contains the guard and its regression fixture.
- `validated-local` requires direct reproduction, zero-provider-call proof, checkpoint proof, adjacent green tests, and exact diff review.
- `validated-target` requires merged source installed on a supported Hermes profile and a fresh real incident-shaped canary.
- Fleet completion requires every supported readable local and VPS profile to report the same merged source identity and pass its probe.

## Loop Control
- Qualification: a controlled micro-loop is not required because this repair has one incident-shaped fixture, one pre-provider guard, and at most two manually invoked correction iterations.
- Maximum build/test/fix iterations: two.
- Stop condition: any successful-compression regression, missing checkpoint, provider call after the guard, content loss, memory regression, or need for new architecture.
- Escalation rule: revert the candidate, preserve the red fixture, and report the missing primitive.
- Runtime authority path: none.
- Append-only evidence path: `internal/ops/evidence/failed-compaction-safety-stop-20260902.log`.

## Validation Plan
- Reproduce the current warning-and-continue branch with a real conversation-loop fixture.
- Add the minimum guard at the existing pre-provider pressure boundary.
- Prove zero provider calls, terminal response, preserved messages, and resumable checkpoint state.
- Run successful-compression, cooldown, turn-checkpoint, and current-turn media tests.
- Run repository lint for changed Python, contract validation, and `git diff --check`.
- Open PR, wait for CI, merge only when green, then deploy and canary under a separate target contract.

## Validation Evidence
```json
{
  "schemaVersion": 1,
  "checks": [
    {
      "criterionId": "AC-001", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Incident log and current-main branch inspection", "target": "pre-provider cooldown branch on maikolb/main 5e073855c8",
      "procedure": "Correlated the 266,692/231,200 cooldown incident with the branch that warned and had no return before provider dispatch.",
      "expected": "The pre-repair request remains eligible for provider dispatch.", "observed": "The incident continued into tool execution and current main contained only the warning at that boundary.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-002", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Full conversation-loop behavior test", "target": "over-threshold request with cooldown:59",
      "procedure": "Ran AIAgent.run_conversation with the incident-shaped compressor state and asserted the model client was never called.",
      "expected": "Zero provider calls and an actionable soft terminal result.", "observed": "Provider calls=0, api_calls=0, completed=false, compression_deferred=true, and the response names /compress and /new.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-003", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Checkpoint transition behavior test", "target": "existing turn checkpoint",
      "procedure": "Observed the exact transition calls during the full conversation-loop fixture.",
      "expected": "The checkpoint follows planning with compaction_deferred and a recovery action.", "observed": "The second transition set phase=compaction_deferred and next_action=run_manual_compression_then_resume_current_turn; checkpoint_preserved=true.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-004", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Below-threshold control and adjacent successful-compression suites", "target": "safe request and normal compaction paths",
      "procedure": "Ran the below-threshold cooldown control plus preflight cap, summary continuity, checkpoint compaction, and current-turn media suites.",
      "expected": "Safe requests still call the provider and successful compression stays green.", "observed": "The control called the provider once and completed; all 41 adjacent tests passed.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-005", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Changed-path and behavior inspection", "target": "exact feature worktree",
      "procedure": "Inspected the complete diff and ran the memory-adjacent summary, checkpoint, and media tests.",
      "expected": "No quality, memory, timeout, provider, model, protected-tail, or AOF weakening.", "observed": "Only conversation-loop handling, focused tests, contract, regression, and evidence changed; no excluded setting or lifecycle changed.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-006", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Provider-call and iteration-budget assertions", "target": "incident-shaped turn",
      "procedure": "Asserted no provider create call, api_calls=0, soft defer, and refunded unmade iteration.",
      "expected": "No model or tool-loop continuation is possible in the blocked oversized turn.", "observed": "The function returned from the pre-provider branch before any response or tool iteration.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-007", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Focused, adjacent, gateway, lint, compile, and diff gates", "target": "exact feature worktree",
      "procedure": "Ran 41 agent tests, 8 gateway tests, Ruff 0.15.10, py_compile, and git diff --check.",
      "expected": "All declared source gates pass.", "observed": "41+8 tests passed, Ruff and compilation passed, and diff check reported no whitespace error.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    },
    {
      "criterionId": "AC-008", "status": "passed", "performedBy": "agent", "verificationMode": "direct",
      "method": "Canonical regression record inspection", "target": "docs/regressions/REG-2026-09-02-001.md",
      "procedure": "Recorded feedback, incident evidence, reproduction, root cause, prevention, validations, exclusions, and readiness ceiling.",
      "expected": "The bug class has one repository-owned prevention artifact.", "observed": "REG-2026-09-02-001 binds the prevention to zero provider calls and preserved checkpoint state.",
      "performedAtUtc": "2026-09-02T17:35:00Z", "artifacts": [{"path":"internal/ops/evidence/failed-compaction-safety-stop-20260902.log","sha256":"906a1650e86df009827e0aa525564396f7a5efb711a9ec247246a6e63e6208b3"}]
    }
  ]
}
```

## Status
- Contract preflight: completed
- Implementation: completed in the isolated feature worktree
- Validation: validated-local with incident-shaped, control, adjacent, gateway, lint, compile, and diff evidence
- Completion: completed for source readiness; PR CI, merge, release, fleet deployment, and target canary remain pending
