# Hermes durable Kanban event delivery

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-EVENT-DELIVERY-20260905
- Mode: REPAIR
- Risk Level: HIGH
- Machine Runtime Authority: none: bounded owner-authorized repair in the existing consumer.
- Acceptance Authority: Maikol
- Base: 7d5fa695c0a75cafd8c8c8ef4a03e328a57c1c03 in the existing checkout.

## Requested Outcome
Retain Kanban event delivery until confirmed, recover an interrupted delivery in the existing consumer and avoid a second handoff of work already durably accepted. Maikol explicitly said Faça after the recommendation to fix the loss window instead of adding a general board watchdog.

## Acceptance Criteria
- AC-001: Claiming alone never consumes an event; interrupted claims are recoverable and stale acknowledgements cannot consume newer work.
- AC-002: Passive notification and agent handoff have separate durable progress; a failed or interrupted handoff remains pending without resending a confirmed notification.
- AC-003: A push handoff is acknowledged only after a durable turn checkpoint; repeated delivery of the same accepted handoff does not create another turn. Existing checkpoint recovery owns interrupted execution.
- AC-004: Focused tests demonstrate interruption boundaries. Publish the scoped exact Git revision and activate the immutable candidate on the verified VPS if the affected services can be safely restarted; otherwise record the concrete pending activation condition.

## In Scope
- `tui_gateway/server.py`
- `tests/tui_gateway/test_kanban_notify_poller.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tui_gateway/server.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/tui_gateway/test_kanban_notify_poller.py`
- `hermes_cli/kanban_db.py`
- `gateway/kanban_watchers.py`
- `gateway/wake.py`
- `gateway/platforms/base.py`
- `agent/turn_checkpoint.py`
- `tests/hermes_cli/test_kanban_notify.py`
- `tests/hermes_cli/test_kanban_notify_durable.py`
- `tests/gateway/test_kanban_notifier_wake_only_ordering.py`
- `tests/gateway/test_kanban_notifier_durable.py`
- `tests/gateway/test_wake_delivery.py`
- `tests/gateway/test_kanban_whiteboard_notifications.py`
- `tests/agent/test_turn_checkpoint.py`
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `docs/kanban-event-delivery.md`
- `docs/event-delivery-evidence/`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban_db.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/kanban_watchers.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/wake.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/platforms/base.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/agent/turn_checkpoint.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/hermes_cli/test_kanban_notify.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/hermes_cli/test_kanban_notify_durable.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_notifier_wake_only_ordering.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_notifier_durable.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_wake_delivery.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/gateway/test_kanban_whiteboard_notifications.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/agent/test_turn_checkpoint.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md.scope.json`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/kanban-event-delivery.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/event-delivery-evidence/`
- `work/whiteboard_*.py`
- `outputs/kanban-whiteboard/`
- Existing fork branch fix/nfos-whiteboard-20260905 and exact new immutable release on srv1918217.
- Consistent affected database backup, entrypoint and the two affected Hermes gateway services, with idle preflight and health readback.

## Out of Scope
General board sweep/watchdog, autonomous repair of the sixteen blocked cards, DOV checkout policy, Vigilia frontend or authentication, NFO-Homolog-Lab, P0-4a, retention, VACUUM, concurrent changes, new worktrees, broad suites and multiagent rounds.

## Failure Signal / Repro
- Evidence: `docs/event-delivery-evidence/baseline.json`
Isolated real SQLite reproduction on the exact base: one event claimed, zero sent, zero pending after reopening. The notifier also acknowledges notify+wake text before a best-effort wake; handle_message queues a volatile background turn.

## Root-Cause Hypothesis
Claim and acknowledgement share last_event_id. Crash recovery never executes the rewind handler. A successful text notification is incorrectly treated as acknowledgement of the independent agent handoff.

## Claim Discipline
No historical incident attribution beyond inspected evidence. Delivery deduplication does not imply exactly-once arbitrary external tool effects. Do not claim all blocked cards autonomously resolved or production fixed before exact activation readback.

## Forbidden Actions
No global scripts/gates, hook disablement, existing-release edits, task bulk changes, visible windows, forced interruption of live workers, local Titan restart or secret output.

## Validation Plan
Focused real SQLite interruption, concurrent claim, stale acknowledgement, separate text/wake progress and checkpoint receipt tests. Use existing hidden per-file runner and isolated homes. Recheck exact Git base and clean scoped diff. Prepare immutable exact-SHA release, back up affected databases, check idle target, update entrypoint, restart only affected services and verify process, release and transport health. Roll back pointer on failed health without overwriting new user data.

## Loop Control
A controlled micro-loop is not required because this is one bounded fix with focused tests in the existing implementation and no autonomous task repair loop.
Stop the affected operation on concurrent edits, target mismatch, failed health or a requirement for a new subsystem; report the smallest working slice and concrete remaining requirement.

## Validation Evidence
```json
{"schemaVersion":1,"checks":[]}
```

## Status
- Contract preflight: validated
- Implementation: complete locally; exact publication and activation pending
- Validation: focused local delivery checks passed; target check pending
- Completion: in progress
