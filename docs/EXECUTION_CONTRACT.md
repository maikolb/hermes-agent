# Durable worker continuation repair

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-WORKER-CONTINUATION-20260906
- Mode: REPAIR
- Risk Level: HIGH
- Machine Runtime Authority: none: existing owner-authorized srv1918217 repair, no new machine policy.
- Acceptance Authority: Maikol

## Requested Outcome
Finish the existing autonomous Kanban execution path: resume saved work without replaying a finished answer, enforce exhausted attempts, and reflect real worker state in the existing operator interfaces.

## Acceptance Criteria
- AC-001: A persisted terminal worker turn starts an executable continuation preserving its transcript, task and workspace; an interrupted operational turn resumes from its durable checkpoint.
- AC-002: A gave_up task cannot be claimed on a subsequent dispatcher tick without a recorded continuation decision, including protocol exhaustion below the generic failure count.
- AC-003: Exact affected runtime is published and read back; an existing incident task produces new execution after supported continuation and its real state is visible through Vigilia. An interruption recovery is exercised without losing prior artifacts or duplicating claims.

## In Scope
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/event-delivery-evidence/`
- `docs/REGRESSION_LOG.md`
- `hermes_cli/kanban_db.py`
- `agent/turn_checkpoint.py`
- `cli.py`
- `gateway/kanban_watchers.py`
- `tests/hermes_cli/test_kanban_blocked_sticky.py`
- `tests/hermes_cli/test_kanban_worker_continuity.py`
- `tests/agent/test_turn_checkpoint.py`
- `tests/gateway/test_kanban_notifier.py`
- `work/whiteboard_*.py`
- `work/whiteboard_*.cjs`
- `outputs/rtu-ui/`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban_db.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/agent/turn_checkpoint.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/cli.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/kanban_watchers.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/hermes_cli/test_kanban_blocked_sticky.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/hermes_cli/test_kanban_worker_continuity.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/agent/test_turn_checkpoint.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_notifier.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md.scope.json`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/event-delivery-evidence/`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/REGRESSION_LOG.md`
- Existing branch, immutable SHA release, affected NFOS service activation and supported continuation of incident cards t_d61b19ea and t_9ce8e0e3 after reading current instructions and ownership. Bounded temporary recovery fixture through the existing execution path.

## Out of Scope
New architecture, frameworks, worktrees, PRs, broad test suites, multiagent rounds, global release controls, login, permissions, hooks, unrelated business changes, NFO-Homolog-Lab, P0-4a, retention or VACUUM. Vigilia RTU UI delivery remains preserved.

## Failure Signal / Repro
- Evidence: `C:/Users/maiko/Documents/Codex/2026-09-05/quero-corrigir-a-configura-o-operacional/outputs/rtu-ui/terminal-replay-current.json`
The same persisted session returned four byte-identical answers without tools. Event 10124 gave_up was followed by promoted and claimed run 334 in the next second. DOV repeated process losses while Telegram still displayed working.

## Root-Cause Hypothesis
The protocol violation breaker can trip below the generic failure counter used by recompute_ready. Worker resume selects an old session whose deliverable checkpoint can replay its sealed answer. DOV process death and notifier state require incident-specific evidence before edits.

## Validation Plan
Reproduce the observed transitions using real database and checkpoint code; run only incident-focused tests. Verify exact runtime and process ownership on srv1918217. Publish immutable code with scoped rollback, resume the existing card through supported APIs, read new tool activity and public status, and exercise interruption recovery through an isolated temporary fixture. Preserve existing business artifacts and approvals.

## Forbidden Actions
No fabricated approval, completion or tests; no manual DB/checkpoint surgery; no credential disclosure, visible UI, local Titan restart, unrelated service restart, unauthorized login or gate, overwriting competing work.

## Claim Discipline
Only claim the evidence level actually reached. Local tests alone never establish functioning production or completed business work.

## Loop Control
A controlled micro-loop is not required because this bounded repair uses the existing dispatcher and checkpoint APIs with incident-specific direct verification. Use the existing implementation and shortest reversible route. Stop the affected operation on target mismatch, competing writer or destructive requirement outside authorization. Escalate only the concrete decision. No new broad audit or multiagent run.

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
      "method": "Exact runtime readback, focused incident tests and real public UI observation",
      "target": "srv1918217 Kanban worker runtime and existing Vigilia application",
      "procedure": "Read persisted incident sessions and attempts, exercise isolated process interruption, activate the exact code, resume via existing APIs and inspect public task state and new RTU.",
      "expected": "Executable continuation retains existing work and operator UI reflects measured worker state.",
      "observed": "Ten focused checks passed on the exact Linux release, including abrupt process death and checkpoint/history preservation. Real incident sessions executed new tools after terminal answers without rebuilding prior work.",
      "performedAtUtc": "2026-09-06T07:38:41.152858+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-target-checks.json",
          "sha256": "042554b1e68ab0c42fe7e64c586867f171dbb5e36d9317bd01b9319f45a7b968"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-activation.json",
          "sha256": "6d78314b82b6d60e1714aa13d758af48f48ae3a63c0b4583705b789dfe53ad76"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-browser.json",
          "sha256": "c204cf11e8b0632365b40be227ac0a621ecd5312d7ea118e82e8eecbbccdb7d2"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-activation.json",
          "sha256": "64ed7fdade95a079335a3a809f95942b064445bbbfc23cbeb7b94b145bd2d1ad"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-verification.json",
          "sha256": "70ae668ad71dd1af0c357f1fcbf6556f555fba49233934b64cb76b09692f4c2d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-tests.txt",
          "sha256": "11b84c569f06649415c34dfb17d406ad3ef87f07378621ba25f665949274ca63"
        }
      ]
    },
    {
      "criterionId": "AC-002",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Exact runtime readback, focused incident tests and real public UI observation",
      "target": "srv1918217 Kanban worker runtime and existing Vigilia application",
      "procedure": "Read persisted incident sessions and attempts, exercise isolated process interruption, activate the exact code, resume via existing APIs and inspect public task state and new RTU.",
      "expected": "Executable continuation retains existing work and operator UI reflects measured worker state.",
      "observed": "The actual next dispatch cycle after protocol exhaustion remains blocked until the existing unblock API; no retries increased or database/checkpoint surgery.",
      "performedAtUtc": "2026-09-06T07:38:41.152858+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-target-checks.json",
          "sha256": "042554b1e68ab0c42fe7e64c586867f171dbb5e36d9317bd01b9319f45a7b968"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-activation.json",
          "sha256": "6d78314b82b6d60e1714aa13d758af48f48ae3a63c0b4583705b789dfe53ad76"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-browser.json",
          "sha256": "c204cf11e8b0632365b40be227ac0a621ecd5312d7ea118e82e8eecbbccdb7d2"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-activation.json",
          "sha256": "64ed7fdade95a079335a3a809f95942b064445bbbfc23cbeb7b94b145bd2d1ad"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-verification.json",
          "sha256": "70ae668ad71dd1af0c357f1fcbf6556f555fba49233934b64cb76b09692f4c2d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-tests.txt",
          "sha256": "11b84c569f06649415c34dfb17d406ad3ef87f07378621ba25f665949274ca63"
        }
      ]
    },
    {
      "criterionId": "AC-003",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Exact runtime readback, focused incident tests and real public UI observation",
      "target": "srv1918217 Kanban worker runtime and existing Vigilia application",
      "procedure": "Read persisted incident sessions and attempts, exercise isolated process interruption, activate the exact code, resume via existing APIs and inspect public task state and new RTU.",
      "expected": "Executable continuation retains existing work and operator UI reflects measured worker state.",
      "observed": "Exact runtime and image read back, real incident continuation and public mobile activity verified. Concursa continued through a new technical diagnosis into run356; DOV now has a separately evidenced AOF blocker. Power-loss was not tested on the live VPS.",
      "performedAtUtc": "2026-09-06T07:38:41.152858+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-target-checks.json",
          "sha256": "042554b1e68ab0c42fe7e64c586867f171dbb5e36d9317bd01b9319f45a7b968"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-activation.json",
          "sha256": "6d78314b82b6d60e1714aa13d758af48f48ae3a63c0b4583705b789dfe53ad76"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-browser.json",
          "sha256": "c204cf11e8b0632365b40be227ac0a621ecd5312d7ea118e82e8eecbbccdb7d2"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-activation.json",
          "sha256": "64ed7fdade95a079335a3a809f95942b064445bbbfc23cbeb7b94b145bd2d1ad"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-verification.json",
          "sha256": "70ae668ad71dd1af0c357f1fcbf6556f555fba49233934b64cb76b09692f4c2d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-projection-tests.txt",
          "sha256": "11b84c569f06649415c34dfb17d406ad3ef87f07378621ba25f665949274ca63"
        }
      ]
    }
  ]
}
```

## Status
- Contract preflight: validated
- Implementation: scoped runtime and live-log repairs released
- Validation: direct target evidence and public UI verified; see docs/event-delivery-evidence/CONTINUATION_HANDOFF.md
- Completion: scoped corrections complete; owner acceptance pending. Full business operation remains incomplete because DOV has a separately evidenced AOF contract-selection/baseline blocker.
