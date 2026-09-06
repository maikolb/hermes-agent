# Hermes durable Kanban event delivery

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-ALERT-INTEGRITY-20260906
- Mode: REPAIR
- Risk Level: HIGH
- Machine Runtime Authority: none: bounded owner-authorized repair in the existing consumer.
- Acceptance Authority: Maikol
- Base: 029770242618ee23a317cfdee70ae61846c6e701 in the existing checkout.

## Requested Outcome
Correct the screenshot-confirmed regression: internal recovery instructions must not be published as worker failures; ready-card alerts must use the current queue episode and a measured cause, without duplicate messages. Preserve autonomous work and prove t_f1ce5125 execution plus target behavior.

## Acceptance Criteria
- AC-001: A recently unblocked old card does not inherit its creation age; an unresolved alert is not emitted every poll; lack of a measured impediment is not reported as a proven dispatcher failure.
- AC-002: Administrative reassessment remains a durable coordinator wake but produces no worker-exit trace or leaked instructions. Genuine blocked-worker notices retain their actual cause.
- AC-003: Focused tests pass and the exact immutable release is published, activated and read back, preserving current tasks and their persisted work.
- AC-004: Target t_f1ce5125 has a real worker/run with RTU or a measured current impediment; no new framework, broad suite, auth change, unrelated project change or extra recovery batch.

## In Scope
- `gateway/kanban_watchers.py`
- `tests/gateway/test_kanban_alert_integrity.py`
- `tests/gateway/test_ready_watchdog.py`
- `tests/gateway/test_worker_closeout_traces.py`
- `tests/gateway/test_kanban_whiteboard_notifications.py`
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/event-delivery-evidence/`
- `docs/regressions/REG-2026-09-06-002.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/kanban_watchers.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_alert_integrity.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_ready_watchdog.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_worker_closeout_traces.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_whiteboard_notifications.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md.scope.json`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/event-delivery-evidence/`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/regressions/REG-2026-09-06-002.md`
- `work/whiteboard_*.py`
- `outputs/kanban-whiteboard/`
- Existing fork branch, new exact-SHA release and affected VPS gateways. Controlled gateway restart using the existing interruption/session recovery, after scoped operational snapshots; never restore stale databases over current work.
- Read-only current board, worker, Telegram delivery and public Vigilia evidence.

## Out of Scope
FNAT/Telegram visual redesign, Vigilia authentication, NFO-Homolog-Lab, P0-4a, retention, VACUUM, unrelated application code, new worktrees, general sweeper, new agent framework, broad suites or multiagent rounds.

## Failure Signal / Repro
- Evidence: `docs/event-delivery-evidence/alert-incident-before.json`
Owner screenshots show internal owner-continuity instructions as Worker bloqueado and 6556-minute wait. Target events: unblocked 1788668039, alerts 1788668058 and 1788668122, claim 1788668144. The first alert followed unblock by 19 seconds; worker PID 4024658, run 300, heartbeat and session binding exist.

## Root-Cause Hypothesis
Confirmed code paths: ready age uses created_at; dedupe searches comment author watchdog while mark writes hermes; unknown probe outcome asserts dispatcher inactivity; synthetic blocked events enter worker-exit rendering and last-comment fallback exposes operator instructions.

## Claim Discipline
No historical incident attribution beyond inspected evidence. Delivery deduplication does not imply exactly-once arbitrary external tool effects. Do not claim all blocked cards autonomously resolved or production fixed before exact activation readback.

## Forbidden Actions
No global release scripts, no hook disabling, no existing-release edits, no data deletion/restore over new activity, no blind bulk-unblock, no local Titan restart, no secret output, no unrequested login or external message outside the authorized project/task continuation.

## Validation Plan
Focused tests reproduce the incident without broad suites. Keep administrative wake delivery intact. Prepare a new immutable source release, preserve existing work/session/checkpoints and restart only affected VPS gateways through native recovery. Read exact services, source hashes and t_f1ce5125 run/activity. Do not restart local Titan or repeat the historical recovery batch.

## Loop Control
A controlled micro-loop is not required because this repair uses focused tests and the existing recovery components, without a new autonomous development loop. One bounded implementation/recovery cycle, with targeted iterations for reproduced failures. Stop on target mismatch, destructive data risk, competing writer, or a material requirement for a new component. At thirty active minutes report concrete progress and remaining shortest path.

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
      "method": "Focused regression tests and current production readback",
      "target": "srv1918217 project notifier and public Vigilia",
      "procedure": "Reproduce native notification paths in isolation; verify source hashes, connected PID, preserved dispatcher and worker run log.",
      "expected": "Accurate queue and worker notices without interrupting autonomous work.",
      "observed": "Current queue age, durable per-episode alert dedupe and measured reasons passed focused local and target tests. Incident claim happened 105 seconds after unblock.",
      "performedAtUtc": "2026-09-06T05:14:18.149880+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/alert-release.json",
          "sha256": "1f2668b73d8bd28e58e5711f1333758804e7200cb83f55b08256958391ead026"
        },
        {
          "path": "docs/event-delivery-evidence/alert-local-tests.log",
          "sha256": "012e4a2940ac682ff8136793ccacc3fc3bf89532288f54b0fa73ad88d258f8ff"
        },
        {
          "path": "docs/event-delivery-evidence/alert-target-tests.json",
          "sha256": "d022eab0af20c164d38055c734b447aeb19e5e4a1a9b7df9c4c3c784cc4a6b7f"
        }
      ]
    },
    {
      "criterionId": "AC-002",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused regression tests and current production readback",
      "target": "srv1918217 project notifier and public Vigilia",
      "procedure": "Reproduce native notification paths in isolation; verify source hashes, connected PID, preserved dispatcher and worker run log.",
      "expected": "Accurate queue and worker notices without interrupting autonomous work.",
      "observed": "Native notifier test emits a brief reassessment without internal prompt; focus remains active and actual block cause wins. Durable wake path is unchanged.",
      "performedAtUtc": "2026-09-06T05:14:18.149880+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/alert-release.json",
          "sha256": "1f2668b73d8bd28e58e5711f1333758804e7200cb83f55b08256958391ead026"
        },
        {
          "path": "docs/event-delivery-evidence/alert-local-tests.log",
          "sha256": "012e4a2940ac682ff8136793ccacc3fc3bf89532288f54b0fa73ad88d258f8ff"
        },
        {
          "path": "docs/event-delivery-evidence/alert-target-tests.json",
          "sha256": "d022eab0af20c164d38055c734b447aeb19e5e4a1a9b7df9c4c3c784cc4a6b7f"
        }
      ]
    },
    {
      "criterionId": "AC-003",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused regression tests and current production readback",
      "target": "srv1918217 project notifier and public Vigilia",
      "procedure": "Reproduce native notification paths in isolation; verify source hashes, connected PID, preserved dispatcher and worker run log.",
      "expected": "Accurate queue and worker notices without interrupting autonomous work.",
      "observed": "Exact published source verified in the active project notifier; Telegram connected. Default dispatcher PID remained unchanged; the existing run log persisted and new workers started.",
      "performedAtUtc": "2026-09-06T05:14:18.149880+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/alert-release.json",
          "sha256": "1f2668b73d8bd28e58e5711f1333758804e7200cb83f55b08256958391ead026"
        },
        {
          "path": "docs/event-delivery-evidence/alert-local-tests.log",
          "sha256": "012e4a2940ac682ff8136793ccacc3fc3bf89532288f54b0fa73ad88d258f8ff"
        },
        {
          "path": "docs/event-delivery-evidence/alert-target-tests.json",
          "sha256": "d022eab0af20c164d38055c734b447aeb19e5e4a1a9b7df9c4c3c784cc4a6b7f"
        }
      ]
    },
    {
      "criterionId": "AC-004",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused regression tests and current production readback",
      "target": "srv1918217 project notifier and public Vigilia",
      "procedure": "Reproduce native notification paths in isolation; verify source hashes, connected PID, preserved dispatcher and worker run log.",
      "expected": "Accurate queue and worker notices without interrupting autonomous work.",
      "observed": "Public Vigilia returns HTTP 200 and the session-bound RTU for task t_f1ce5125 run 300 with reasoning and tools. No batch replay or extra component.",
      "performedAtUtc": "2026-09-06T05:14:18.149880+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/alert-release.json",
          "sha256": "1f2668b73d8bd28e58e5711f1333758804e7200cb83f55b08256958391ead026"
        },
        {
          "path": "docs/event-delivery-evidence/alert-local-tests.log",
          "sha256": "012e4a2940ac682ff8136793ccacc3fc3bf89532288f54b0fa73ad88d258f8ff"
        },
        {
          "path": "docs/event-delivery-evidence/alert-target-tests.json",
          "sha256": "d022eab0af20c164d38055c734b447aeb19e5e4a1a9b7df9c4c3c784cc4a6b7f"
        }
      ]
    }
  ]
}
```

## Status
- Contract preflight: validated
- Implementation: released as 6ffe7f4fbf156cb5d20e0d74641f5fa186555326 in the project notifier
- Validation: 26 local and 5 target tests passed; exact source, Telegram and RTU read back
- Completion: complete for alert integrity; business tasks continue; owner acceptance pending
