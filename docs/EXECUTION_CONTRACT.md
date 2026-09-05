# NFOS Kanban Whiteboard Implementation

## Contract Metadata
- Contract Version: 3
- Contract ID: NFOS-WHITEBOARD-20260905
- Mode: REPAIR
- Risk Level: HIGH
- Machine Runtime Authority: none: user authorized this fixed single-writer implementation with focused regressions and target readback, without autonomous multi-agent loops.
- Acceptance Authority: Maikol
- Base: c2bbb6bec5bb78803904fa0636f2b7618c957031, descended from efa3b7835e34600890a952b4f89d84d08e7290fe, in the existing assignee-publish-worktree

## Requested Outcome
One durable request, one responsible executor, truthful task/run state, proportional completion and recovery of the affected Concursa and DOV cards through the existing Hermes.

## Acceptance Criteria
- AC-001: All creation paths preserve task role, repository need, delivery type and current instruction; activity records are never dispatched.
- AC-002: Decomposition respects latest instructions and completed work; repeated operational failures do not create new graphs.
- AC-003: Taking, transferring, completing and recovering work keep task, run and claim consistent; stale attempts cannot finish new ones.
- AC-004: Reports finish with evidence without PR; code keeps project delivery checks.
- AC-005: Board and notification show queued, actually executing, waiting, checking and delivered truthfully.
- AC-006: Existing affected cards are reconciled individually with preserved results and history.
- AC-007: Scoped tests and target readback demonstrate the published candidate; acceptance remains Maikol's.

## In Scope
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/agent/agent_init.py`
- `agent/agent_init.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tools/delegate_tool.py`
- `tools/delegate_tool.py`
- `docs/EXECUTION_CONTRACT.md`
- `docs/EXECUTION_CONTRACT.md.scope.json`
- `work/whiteboard_*.py`
- `outputs/kanban-whiteboard/**`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban_db.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban_decompose.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/gateway/kanban_watchers.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tools/kanban_tools.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tools/delegation_kanban.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tools/principal_turn_mirror.py`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/plugins/kanban/dashboard/**`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/tests/**`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/EXECUTION_CONTRACT.md.scope.json`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/docs/kanban-whiteboard.md`
- `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree/hermes_cli/kanban.py`
- hermes_cli/kanban_db.py
- hermes_cli/kanban_decompose.py
- gateway/kanban_watchers.py
- tools/kanban_tools.py
- tools/delegation_kanban.py
- tools/principal_turn_mirror.py
- plugins/kanban/dashboard/
- Focused Kanban tests and docs, this contract and its manifest
- Existing project-scoped AOF adapter/validator failure handling with controls preserved
- Concursa and DOV task data, consistent backups, exact release preparation, entrypoint and affected VPS services
- Local outputs/kanban-whiteboard evidence and one-shot project recovery artifacts

## Out of Scope
NFO-Homolog-Lab, P0-4a, retention, VACUUM, unrelated dirty checkout work, new worktrees, rebuilding existing PRs/commits, new global scripts or release gates, model/capacity changes, Titan local restart.

## Failure Signal / Repro
- Evidence: C:/Users/maiko/Documents/Codex/2026-09-05/quero-corrigir-a-configura-o-operacional/outputs/activation/active-behavior-readback.json
- Target inspection: t_449a8a34 report hit delivery_review_required then acquired five worktree children; t_af3159a0 archived with running attempt; DOV literal unassigned and executed activity mirror; AOF terminal TimeoutExpired.
- Evidence-Absent: these exact live incident excerpts were inspected read-only in the planning turn and will be captured with the implementation evidence before recovery mutations.

## Root-Cause Hypothesis
Confirmed independent paths omit durable delivery classification and bypass common lifecycle updates. Decomposition treats recurrence triage as unfinished implementation and ignores current human clarification. AOF timeout incident is confirmed; exact expensive validator phase requires measurement before changing behavior.

## Claim Discipline
Only direct tests justify validated-local; active process and user-route evidence justify validated-target/released. Existing artifacts are reused, never counted as proof of this candidate.

## Forbidden Actions
No credentials in output, global permission/hook disablement, retired release scripts, editing existing releases, bulk completion of unverified work, duplicate external actions, unrelated checkout edits, visible UI or forced interruption of live workers.

## Loop Control
A controlled autonomous micro-loop is not required: this is a fixed single-writer implementation with at most three focused fix/test iterations per affected behavior and no delegated execution. Pause only the affected phase on new business decisions, incompatible concurrent work or non-proportional validation requirements. At thirty active minutes report actual delivered slice and remaining cost before expanding.

## Validation Plan
Use canonical per-file Python test runner with hidden Windows subprocesses and temporary HERMES_HOME, restricting tests to changed behavior and adjacent contract checks. The shell wrapper performs repository-wide compilation, which conflicts with the user's explicit focused-test limit. Validate contract before source edits and at closeout. Record baseline and diff, preserve prior commit provenance, prepare immutable SHA candidate, snapshot affected data/config, atomically update entrypoint after idle check, restart only affected VPS services and allow measured cold-start time. Roll back entrypoint on failed health; do not restore data over new user activity.

## Validation Evidence
```json
{"schemaVersion":1,"checks":[]}
```

## Status
Backend implemented; focused native lifecycle, HTTP and notifier checks passed locally. Reused RTU binding delta from fa0f1e6ff0e5f475e015dba836103f2b46a60a93 without merging its ancestry. Lux delta coordinated with its existing owner thread. Production activation and individual card recovery remain pending.
