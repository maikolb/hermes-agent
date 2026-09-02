# Install and Update E2E Node CA Repair

## Contract Metadata
- Contract Version: 3
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Documents\Codex\2026-09-02\a\work\hermes-agent-node-ca`
- Target Branch: `fix/install-e2e-node-ca`
- Updated At: 2026-09-02T23:00:00Z
- Machine Runtime Authority: none: feature-branch GitHub Actions only, with no merge or release authority
- Authorisation: Maikol requested investigation, the smallest evidence-backed correction, and a PR.

## Requested Outcome
- Restore the installer legs of Install & Update E2E by making Node trust the sandbox proxy certificate authority.

## Acceptance Criteria
- AC-001: All five failed installer artifacts identify the same Node dependency failure and the sandbox certificate mismatch explains the exact boundary.
- AC-002: The sandbox gives Node the CA that signs its intercepted HTTPS certificates, without changing production installer behavior.
- AC-003: The exact feature branch passes a real installer E2E leg from a sampled release tag.
- AC-004: Focused syntax, diff, contract, and repository checks pass.

## Failure Signal / Repro
- Scheduled run 33687295780 passed all update legs and failed installer legs from v2026.3.12, v2026.4.8, v2026.5.16, v2026.6.19, and v2026.8.3.
- Every initial legacy installer logged an npm failure but continued. Every current installer re-run stopped at the now-fatal root `npm install`.
- The proxy logs recorded repeated TLS EOFs during those npm attempts.
- Evidence artifact: `internal/ops/evidence/install-e2e-node-ca-20260902.log`.

## Root-Cause Hypothesis
- Fact: `proxy.py` generates per-host certificates signed by `/work/certs/ca.pem`.
- Fact: curl, Python, and Git are configured to trust `/work/certs/ca.pem`.
- Fact: Node alone receives `NODE_EXTRA_CA_CERTS=/work/certs/real-ca.pem`, which is the proxy's upstream trust bundle and does not sign the certificates Node receives.
- Fact: older installers masked this npm TLS failure; commit 6a198f8a12 correctly made missing Node dependencies fatal, exposing the existing sandbox defect.
- Chosen fix point: change the sandbox-only Node trust path from `real-ca.pem` to `ca.pem`.

## In Scope
- `scripts/sandbox/stage2-run.sh`
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/regressions/REG-2026-09-02-002.md`
- `internal/ops/evidence/install-e2e-node-ca-20260902.log`

## Out of Scope
- Weakening the installer's fatal npm behavior.
- Skipping Node dependencies in the installer route.
- Changing release tags, update logic, proxy forwarding, or production CA handling.
- Merge, release, or deployment.

## Forbidden Actions
- Do not turn the required npm install back into a warning.
- Do not disable TLS verification.
- Do not claim target validation from unit or static inspection alone.
- Do not merge or release from this run.

## Claim Discipline
- `implemented` means the sandbox-only CA mapping is corrected on the branch.
- `validated-local` requires syntax, diff, and contract checks plus a real GitHub Actions installer E2E on the exact commit.
- `validated-target` is not claimed because no released workflow or merged main run is part of this task.

## Loop Control
- Qualification: a controlled micro-loop is not required because the repair is one sandbox-only environment mapping with one existing real E2E gate.
- Maximum build/test/fix iterations: two.
- Stop condition: the live job fails outside the diagnosed TLS boundary, another route regresses, or a broader trust redesign is required.
- Escalation rule: preserve the one-line candidate and report the new log evidence without widening scope.
- Runtime authority path: feature-branch GitHub Actions only.
- Append-only evidence path: `internal/ops/evidence/install-e2e-node-ca-20260902.log`.

## Validation Plan
- Preserve the five downloaded installer artifacts and correlate their failure boundary.
- Validate shell syntax and the exact one-line diff.
- Push the feature branch and dispatch one real installer E2E leg from a release tag.
- Record the run URL, conclusion, exact commit, and final checks in the evidence artifact.
- Validate this contract in Final phase before delivery.

## Validation Evidence
```json
{
  "schemaVersion": 1,
  "checks": []
}
```

## Status
- Contract preflight: completed
- Implementation: in progress
- Validation: pending live feature-branch E2E
- Completion: in progress
