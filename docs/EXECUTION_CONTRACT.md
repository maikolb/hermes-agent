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
Pending direct validation of the incident-shaped paths and exact active runtime.

## Status
In progress. Production runtime repair and direct recovery validation remain pending.
