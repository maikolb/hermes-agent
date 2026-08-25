# Hermes NF Worker Focus Live Output Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: C:\Users\maiko\Documents\Codex\2026-08-25\corre-o-no-hermes-nf-da-2\work\hermes-agent
- Target Branch: main via PR from fix/nf-worker-focus-live-output-20260825
- Updated At: 2026-08-25T19:50:00-03:00
- Machine Runtime Authority: none: the root agent may promote only after local gates and an explicit NF idle proof; the current 2026-08-24 release remains the rollback authority
- Event Evidence: VPS NF worker log for t_c3923c5d, gateway/profile configuration, focused notifier tests, process identity, and target Telegram delivery evidence

## Requested Outcome
- When the NF principal turn ends while subscribed Kanban workers remain active, make `kanban.worker_focus_handoff: true` show the selected worker's live activity, tool progress, and available reasoning in the originating Telegram topic instead of only the static `Now following worker` card.

## Acceptance Criteria
- Focus handoff remains opt-in and inactive while the originating gateway session is running.
- Once the principal session is idle, the existing focus message includes the configured activity-indicator text and a bounded, redacted view of the current worker attempt's CLI output.
- Worker tool lines and available reasoning are reflected by editing one existing message, never by sending one message per log line.
- A retried worker shows only its latest attempt; output from older attempts is not replayed.
- Missing, unreadable, binary, oversized, or rotating worker logs fail soft and never stop the notifier tick.
- Existing multi-worker selection, rotation, retry cleanup, terminal cleanup, profile authorization, thread routing, and passive-notification behavior remain unchanged.
- No worker process, Kanban row, subscription, session, memory, credential, Telegram binding, project data, or user workspace is modified by the follower.
- Focused tests, compilation, diff checks, contract validation, PR checks, and an idle-gated target probe pass before release is claimed.

## In Scope
- `gateway/kanban_watchers.py` worker-focus rendering and bounded reads through the existing `hermes_cli.kanban_db.read_worker_log()` accessor.
- `tests/gateway/test_kanban_notifier.py` regressions for activity text, current-attempt isolation, reasoning/tool rendering, redaction, and fail-soft log reads.
- `docs/regressions/REG-2026-08-25-003.md` and this contract.
- Read-only inspection of the NF profile configuration, worker logs, Kanban state, and gateway logs.
- PR/merge to `maikolb/hermes-agent:main` and an idle-gated NF release cutover with exact rollback to the current release on a failed target check.

## Out of Scope
- Changing worker scheduling, claims, concurrency, assignees, task lifecycle, Kanban schemas, notifier subscriptions, or focus selection policy.
- Streaming raw model/provider events, adding a new IPC service, socket, daemon, database, plugin, tool, or configuration property.
- Changing NF/PF identities, tokens, Telegram routes, sessions, memory, skills, project data, worker workspaces, or user repositories.
- Modifying the default/Titan, bench-supervisor, Windows PF, Exocortex, CEOGame, or unrelated profiles.
- Restarting the NF gateway while any principal turn, Kanban worker, pending resume, or unrelated release activity is active.

## Failure Signal / Repro
- At 2026-08-25 22:17 UTC, Telegram displayed `Now following worker` for board `hermes-project-facto-786f51f34a055368--infotributos`, task `t_c3923c5d`, run 6, but displayed no worker activity, reasoning, or tool progress afterward.
- The exact worker log grew to 10,983 bytes through 22:25:58 UTC and contained many tool-progress lines plus a `Reasoning` block, proving the worker produced displayable output while the gateway showed none of it.
- Current `GatewayKanbanWatchersMixin._kanban_refresh_worker_focus()` renders only task metadata; it never calls the existing worker-log accessor or any worker output source.
- Evidence artifact: `docs/regressions/REG-2026-08-25-003.md` records the exact task, timestamp, observed output gap, and prevention surface.

## Root-Cause Hypothesis
- Fact: `worker_focus_handoff` tracks claimed workers and sends/edits a static focus message after `_is_session_running()` becomes false.
- Fact: dispatcher workers run as detached CLI subprocesses and already write their visible tools/reasoning to a per-task log through `read_worker_log()`'s canonical path.
- Fact: the focus implementation neither reads nor renders that log; its tests assert only the static counter/title behavior.
- Chosen repair: extend the existing focus-message edit loop to render a bounded, current-attempt-only, force-redacted projection of the existing worker log plus the configured activity-indicator text.

## Claim Discipline
- `implemented` means the source, regression record, and focused tests contain the bounded follower behavior.
- `validated-local` requires focused tests, compilation, diff checks, and both execution-contract validators.
- `validated-target` requires an idle-gated NF cutover, exact loaded-release identity, healthy gateway, unchanged profile/board invariants, and a real principal-to-worker Telegram probe showing activity plus worker output.
- `released` requires merged `main` and the NF service running the exact merged release.
- `accepted` requires Maikol's natural use or explicit confirmation after the target probe.

## Forbidden Actions
- Do not read or forward `.env`, credentials, raw provider payloads, full transcripts, historical worker attempts, or unbounded logs.
- Do not add a second worker-output transport or bypass the existing profile authorization/thread-routing chokepoints.
- Do not delete, rewrite, repair, or migrate Kanban/session/memory/project data.
- Do not restart or promote while the NF has an active principal turn, worker, pending resume, or another release operation.
- Do not stash, force-push, reset unrelated work, mutate the live checkout in place, or delete a branch before proving its tip is contained in merged `main`.

## Loop Control
- A controlled autonomous micro-loop is not required because this repair has one bounded source patch, one focused test path, and one idle-gated release probe with an immediate immutable-release rollback.
- Maximum implementation/test/fix iterations: 3 for the focused worker-follow path.
- Green condition: the red test reproduces the missing output, the fix passes all focused gates, the PR merges normally, and the idle-gated target probe shows the configured activity tag plus bounded worker output without duplicate messages or invariant drift.
- Rollback: if the candidate gateway fails readiness, changes worker/board/profile invariants, or the target probe does not show live output, switch the NF service back to `/usr/local/lib/hermes-agent.release-20260824-986c77af` and record the failed evidence without retrying unchanged code.
- Escalation: stop on a third repeated failure, any need for schema/state/credential mutation, inability to prove idle, or a solution that requires new IPC/architecture.

## Validation Plan
- Add focused unit/integration coverage for configured `Trabalhando` text, tool/reasoning projection, latest-attempt isolation, forced secret redaction, bounded rendering, and unreadable/missing-log fail-soft behavior.
- Run the worker-focus/notifier tests plus directly adjacent activity/display tests, `py_compile`, `git diff --check`, and both canonical execution-contract validators.
- Inspect the final diff against the declared paths and verify no profile/config/state files changed.
- Push the branch, open a PR, wait for required checks, merge normally, and prove branch ancestry before cleanup.
- On the VPS, prove NF principal/worker/pending-resume idle, build/install the exact merged commit as a new immutable release, restart only `hermes-gateway@hermes-project-factory`, and verify process/source/config identity and health.
- Run one bounded real Telegram principal-to-worker probe; confirm one focus message shows the configured activity text and subsequent worker tool/reasoning output, then verify no duplicate flood, no gateway errors, and unchanged Kanban/profile invariants.

## Status
- Contract preflight: complete; this task's updated contract passed both canonical validators before the product-code edit.
- Implementation: complete for the bounded source path, regression tests, and incident-prevention record.
- Validation: `validated-local` with 51 focused/adjacent tests, compilation, Ruff, diff checks, and both canonical contract validators; PR and target validation remain pending.
- Completion: not claimed; merge, idle-gated NF release, and the real Telegram principal-to-worker probe remain pending.
