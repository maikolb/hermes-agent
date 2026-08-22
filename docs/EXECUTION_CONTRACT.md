# Hermes Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: FIX_ONLY
- Risk Level: HIGH
- Workspace: C:\Users\maiko\Projetos\hermes-tool-guardrail-fix-20260821
- Updated At: 2026-08-21T20:20:00-03:00
- Machine Runtime Authority: none: isolated source worktree and focused tests only; no gateway/process consumes this checkout

## Requested Outcome
- Eliminate the `ToolCallGuardrailController` redirect-state `AttributeError` without changing unrelated guard decisions or mutating/reloading the live Hermes checkout.

## Acceptance Criteria
- Construction and every `reset_for_turn()` initialize empty signature- and tool-level redirect maps.
- A structural failure records the intended redirect instead of raising `AttributeError`.
- The same affected signature/tool is redirected on its next attempt while an unrelated tool remains available.
- Reset removes prior redirects and preserves all existing counters, halt behavior, thresholds, and decision semantics.
- Focused behavioral tests pass through the repository-mandated `scripts/run_tests.sh` wrapper.
- The source candidate is committed only on the isolated branch; no live checkout Git operation or gateway reload occurs.

## In Scope
- `agent/tool_guardrails.py`.
- `tests/agent/test_tool_guardrail_strategy_redirect.py` and, only if needed, an adjacent focused behavioral test module.
- `docs/REVISION_PROTOCOL.md` and this contract in the isolated worktree.
- Local branch `codex/tool-guardrail-redirect-state` and validation evidence.

## Out of Scope
- `C:\Users\maiko\AppData\Local\hermes\hermes-agent`, all of its existing dirty files, branches, stashes, and loaded gateway processes.
- AOF route-policy architecture, search tool implementation, prompt text, tool schemas, profile configuration, model routing, compaction, channels, dependencies, or lockfiles.
- Gateway restart/reload, production traffic, release, force-push, merge/cherry-pick into the live checkout, or target-readiness claims.

## Failure Signal / Repro
- `reset_for_turn()` does not define `_redirected_signatures` or `_redirected_tools`; `after_call()` writes those fields on structural failure and `before_call()` writes the tool map on redirect decisions, producing repeated `AttributeError` log entries.

## Root-Cause Hypothesis
- Fact: the two maps are used by productive control flow but are absent from the constructor/reset path in the active revision.
- Fact: a sibling candidate implementation initializes both maps in `reset_for_turn()`, corroborating the intended state lifecycle, but no code will be copied wholesale from that tree.
- Chosen fix point: initialize both typed maps beside the other per-turn state and add behavior-level reset/redirect tests.
- No broader hypothesis is required; the failing attribute access and missing initialization are directly observable.

## Claim Discipline
- Before tests: `implemented`.
- After focused tests/compile/diff review: `validated-local` for the isolated source candidate.
- Live Hermes remains unchanged and `not-validated-target`.

## Forbidden Actions
- No checkout, switch, merge, pull, rebase, cherry-pick, reset, stash, patch, or file edit in the live Hermes checkout.
- No gateway/process reload, visible UI, production message, profile edit, dependency install, or broad refactor.
- Do not change thresholds, public tool schemas, prompt caching, or block unrelated tools.
- Do not replace tests with source-text assertions; execute the behavior.

## Loop Control
- Maximum implementation/test/fix iterations: 3.
- Green condition: focused tests reproduce the missing-state path and pass after the minimal initialization; compile and diff checks pass; no unrelated paths change.
- Escalation: stop if the test requires a live gateway, dependency change, target checkout mutation, or a semantic guard redesign.
- Runtime authority path: none.
- Append-only evidence path: test output, branch commit, and `docs/REVISION_PROTOCOL.md`.

## Validation Plan
- Add behavior tests for structural failure -> redirect, unrelated-tool availability, and reset clearing both maps.
- Run `scripts/run_tests.sh tests/agent/test_tool_guardrail_strategy_redirect.py`.
- Run Python compile for `agent/tool_guardrails.py`, `git diff --check`, and inspect the exact isolated diff.
- Validate this contract with the global execution-contract validator before implementation and at closeout.
- Compare the live checkout status before/after and require byte/status stability for all pre-existing dirty paths.

## Status
- Contract preflight: validated by the global version-2/canonical-path contract validator before implementation.
- Implementation: completed in the isolated branch; only the declared redirect-state lifecycle and focused tests changed.
- Validation: validated-local; focused wrapper suite passed 6/6 without retry, Python compile and diff checks passed.
- Completion: completed for the isolated source candidate; live cutover, gateway reload, and validated-target remain intentionally excluded and pending.
