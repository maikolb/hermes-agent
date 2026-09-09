# Delivery recovery, 2026-09-09

Owner request: compare the earlier delivery flow with the current VPS Hermes,
excluding model changes, and repair the obstacles to delivery within 10 usage
points. Evidence baseline: the DOV and Concursa Kanban, Telegram, Git and
production comparison in Nexa Factory OS output/delivery-comparison-20260908.

## Acceptance

1. Reconcile historical blockers against the actual running source before changes.
2. Provide a supported, recoverable repair for idle NFOS cards bound to an
   incorrect repository. Keep the same card, history, source workspace, spec,
   decisions and external-effect receipts. Never fabricate a PR or deployment.
3. Reject a stale source identity, an unconfigured destination, a live executor,
   an unrelated target worktree, or modification of another task from a worker.
4. Persist the repair before filesystem creation; commit the new task binding
   and ownership together. Retry after interruption without losing files or
   creating duplicate worktrees. Keep the original status; repair is not delivery.
   Correct missing executor and mistaken report/operation/code classification
   through a maintainer command. Preserve old specs; a changed delivery type
   requires a matching new spec revision before effects or completion. An
   explicitly requested canonical checkout retains exclusive-writer leases.
5. Validate retained-workspace isolation, session recovery, report-only closure,
   specification binding and homologation/staging with real Git and SQLite tests.
6. Apply confirmed repairs to the affected VPS cards through this supported path,
   preserving explicit suspensions and concrete external dependencies. Verify the
   installed code, resulting repository identity and preserved evidence.
7. Allow reconsideration of a technical impediment when candidate/homologation
   binding is incomplete. Preserve both identities and require the existing
   homologation and publication approvals before external delivery effects.
8. Prevent recurrence at retained-card dispatch: compare the inherited Git
   repository with the configured project repository before reusing or creating
   a checkout. Repair a proven mismatch through the existing recoverable path,
   preserve source files/history, and resume the same repair after interruption.
   A previous isolation receipt must not bless the wrong repository forever.

No model, provider, product scope, customer data, permission boundary, existing
stop instruction or AOF disabled state is changed by this recovery.
