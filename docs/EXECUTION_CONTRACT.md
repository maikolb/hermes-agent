# Hermes durable Kanban event delivery

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-CONTINUITY-20260906
- Mode: REPAIR
- Risk Level: HIGH
- Machine Runtime Authority: none: bounded owner-authorized repair in the existing consumer.
- Acceptance Authority: Maikol
- Base: 5c7cf1aca730384dafaf4a0114e0842103758802 in the existing checkout.

## Requested Outcome
Finish owner-authorized autonomous task continuity: accepted work remains recoverable after process/host interruption, blocked work reaches its existing coordinator for diagnosis, existing cards are recovered individually, and worker execution and RTU are observable through production Vigilia. Maikol explicitly requested completion on 2026-09-06.

## Acceptance Criteria
- AC-001: Existing worker/session recovery preserves task identity, persisted progress and current instructions across interruption, with a focused real-process restart test.
- AC-002: New block/failure events reach the existing coordinator durably with actionable context. Resolvable blocks resume within the authorized task; human dependencies remain explicit. Historical blockers receive one bounded diagnosis without blind bulk-unblock or duplicated tasks.
- AC-003: Exact candidate is published to the existing fork branch and activated on srv1918217 with proportional backup, rollback and readback. A real worker and its per-run RTU are observed in public Vigilia.
- AC-004: No known completed/ cancelled work is restarted, no duplicate active worker owns a card, and no broad suite, general sweeper, new queue, new security barrier or unrelated project change is introduced.

## In Scope
- `hermes_cli/kanban_db.py`
- `hermes_cli/worker_protocol.py`
- `gateway/kanban_watchers.py`
- `gateway/run.py`
- `gateway/session.py`
- `gateway/wake.py`
- `gateway/platforms/base.py`
- `agent/turn_checkpoint.py`
- `cli.py`
- `tools/kanban_tools.py`
- `locales/en.yaml`
- `tests/hermes_cli/test_kanban_worker_continuity.py`
- `tests/gateway/test_kanban_continuity.py`
- `tests/gateway/test_restart_resume_pending.py`
- `tests/gateway/test_kanban_notifier_durable.py`
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/kanban-event-delivery.md`
- `docs/event-delivery-evidence/`
- `docs/regressions/REG-2026-09-06-001.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban_db.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/worker_protocol.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/kanban_watchers.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/run.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/session.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/wake.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/platforms/base.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/agent/turn_checkpoint.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/cli.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tools/kanban_tools.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/locales/en.yaml`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/hermes_cli/test_kanban_worker_continuity.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_continuity.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_restart_resume_pending.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_notifier_durable.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md.scope.json`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/kanban-event-delivery.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/event-delivery-evidence/`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/regressions/REG-2026-09-06-001.md`
- `work/whiteboard_*.py`
- `outputs/kanban-whiteboard/`
- Existing NFOS profile continuity settings and task/notification records in srv1918217, with scoped snapshots and individual recovery decisions.
- Existing fork branch fix/nfos-whiteboard-20260905, a new exact-SHA immutable release, its environment, entrypoint and affected gateway services.

## Out of Scope
FNAT/Telegram visual redesign, Vigilia authentication, NFO-Homolog-Lab, P0-4a, retention, VACUUM, unrelated application code, new worktrees, general sweeper, new agent framework, broad suites or multiagent rounds.

## Failure Signal / Repro
- Evidence: `docs/event-delivery-evidence/continuity-preflight.json`
Current target read: zero active runs; Concursa has thirteen sticky blocked cards and three at the failure limit, ten of those sixteen have notify-only subscriptions. Existing dispatcher spawn does not request resume of prior worker session. Gateway recovery applies freshness checks. Each proposed code fix must reproduce its affected boundary before modification.

## Root-Cause Hypothesis
Incomplete operational wiring between persisted task interruption/block events, coordinator diagnosis and native session recovery. Verify actual loss or skipped recovery in focused executions; do not infer every historical blocker has one cause.

## Claim Discipline
No historical incident attribution beyond inspected evidence. Delivery deduplication does not imply exactly-once arbitrary external tool effects. Do not claim all blocked cards autonomously resolved or production fixed before exact activation readback.

## Forbidden Actions
No global release scripts, no hook disabling, no existing-release edits, no data deletion/restore over new activity, no blind bulk-unblock, no local Titan restart, no secret output, no unrequested login or external message outside the authorized project/task continuation.

## Validation Plan
Use focused native per-file tests and isolated state for interruption experiments. Read current target and configuration before change. Reuse all prior work. Back up only affected small operational records/configuration before mutation; avoid copying session history during downtime. Promote the exact SHA, restart only affected gateways and inspect a real resumed task plus its live RTU. Preserve business artifacts and receipts for uncertain external effects; no exactly-once claim for arbitrary external services.

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
      "method": "Focused isolated interruption tests and production readback",
      "target": "srv1918217 and public Vigilia Lux",
      "procedure": "Read exact release, services, task events, worker process identity and session-bound public log; compare consecutive log sizes.",
      "expected": "Authorized persisted work resumes through the existing dispatcher and is observable without duplication.",
      "observed": "51 focused local tests and 5 target tests passed, including real process death, session/checkpoint recovery, atomic creation and single scheduled resume.",
      "performedAtUtc": "2026-09-06T04:11:34.310458+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-release.json",
          "sha256": "7fa5fdeadf27f19b12be5af5e8359d479cfb5698069a69f939e10dae9de58c8d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-local-tests.log",
          "sha256": "7ef92d1722b05fefd19cbeb187c3622c3b3f77fa6534e52f7303b4e9cce06215"
        }
      ]
    },
    {
      "criterionId": "AC-002",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused isolated interruption tests and production readback",
      "target": "srv1918217 and public Vigilia Lux",
      "procedure": "Read exact release, services, task events, worker process identity and session-bound public log; compare consecutive log sizes.",
      "expected": "Authorized persisted work resumes through the existing dispatcher and is observable without duplication.",
      "observed": "27 historical requests persisted after individual review of reasons; two concrete human inputs retained. Native coordinator actions unblocked existing cards and native workers claimed them.",
      "performedAtUtc": "2026-09-06T04:11:34.310458+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-release.json",
          "sha256": "7fa5fdeadf27f19b12be5af5e8359d479cfb5698069a69f939e10dae9de58c8d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-local-tests.log",
          "sha256": "7ef92d1722b05fefd19cbeb187c3622c3b3f77fa6534e52f7303b4e9cce06215"
        }
      ]
    },
    {
      "criterionId": "AC-003",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused isolated interruption tests and production readback",
      "target": "srv1918217 and public Vigilia Lux",
      "procedure": "Read exact release, services, task events, worker process identity and session-bound public log; compare consecutive log sizes.",
      "expected": "Authorized persisted work resumes through the existing dispatcher and is observable without duplication.",
      "observed": "Exact SHA active in both connected gateways. Two real workers have session-bound RTU available over the public Vigilia API. DOV run 315 grew from 38562 to 217539 bytes.",
      "performedAtUtc": "2026-09-06T04:11:34.310458+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-release.json",
          "sha256": "7fa5fdeadf27f19b12be5af5e8359d479cfb5698069a69f939e10dae9de58c8d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-local-tests.log",
          "sha256": "7ef92d1722b05fefd19cbeb187c3622c3b3f77fa6534e52f7303b4e9cce06215"
        }
      ]
    },
    {
      "criterionId": "AC-004",
      "status": "passed",
      "performedBy": "agent",
      "verificationMode": "direct",
      "method": "Focused isolated interruption tests and production readback",
      "target": "srv1918217 and public Vigilia Lux",
      "procedure": "Read exact release, services, task events, worker process identity and session-bound public log; compare consecutive log sizes.",
      "expected": "Authorized persisted work resumes through the existing dispatcher and is observable without duplication.",
      "observed": "Inspected current processes have distinct PIDs, cards and runs; resumed work retains original card identities. No new component or broad test suite was added.",
      "performedAtUtc": "2026-09-06T04:11:34.310458+00:00",
      "artifacts": [
        {
          "path": "docs/event-delivery-evidence/continuity-release.json",
          "sha256": "7fa5fdeadf27f19b12be5af5e8359d479cfb5698069a69f939e10dae9de58c8d"
        },
        {
          "path": "docs/event-delivery-evidence/continuity-local-tests.log",
          "sha256": "7ef92d1722b05fefd19cbeb187c3622c3b3f77fa6534e52f7303b4e9cce06215"
        }
      ]
    }
  ]
}
```

## Status
- Contract preflight: validated
- Implementation: released as ab8710cfa5e29c7f09d35f9c84780150492fefc9
- Validation: 51 local and 5 target tests passed; two worker RTU streams verified on target
- Completion: complete for technical continuity; historical reconciliation and business tasks continue; owner acceptance pending
