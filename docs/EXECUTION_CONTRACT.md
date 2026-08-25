# Hermes Runtime Recovery Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Target Branch: main via PR from fix/nf-delivery-visible-activity-20260825
- Updated At: 2026-08-25T10:45:00-03:00
- Machine Runtime Authority: none: the repair consists of bounded source, configuration, restart, and delivery probes with immediate rollback on any failed target check
- Event Evidence: VPS NF gateway log, profile configuration inventory, focused regression tests, process identity, and target delivery readback

## Requested Outcome
- Restore reliable Hermes delivery and the previously visible reasoning, skills, tools, and activity across active profiles, while preserving NF/PF worker parallelism and focus rotation; integrate by PR and leave the Hermes repository on `main` only.

## Acceptance Criteria
- A cached gateway agent refreshes platform, chat, thread, user, and session identity from every current message before checkpoint creation.
- The NF durable delivery checkpoint binds to the real Telegram group route instead of an empty stale route.
- The existing VPS `@hermes_nexafactory_bot` remains the NF identity and its working 2026-08-24 runtime remains the rollback baseline until a candidate passes target checks.
- Every active Windows and VPS profile explicitly shows reasoning, commentary, interim activity, skills, tool progress, thinking progress, and background-process progress on its actually configured chat platforms.
- NF and PF retain their working-time status, concurrent workers, worker-focus handoff, and focus rotation; they are not normalized into ordinary profiles.
- Exocortex receives no Telegram-specific change because it has no Telegram route in this incident.
- No bot token, chat/topic binding, session, Kanban state, memory, or project data is deleted or reassigned.
- Focused and relevant repository tests, compilation, diff checks, and the canonical execution-contract validator pass before promotion.
- A GitHub PR merges the repair into `maikolb/hermes-agent:main`; required checks are green before merge.
- Local and remote `main` resolve to the merged commit, the repair branch is deleted after ancestry proof, and no stash is created.

## In Scope
- `gateway/run.py` cached-agent per-message route refresh.
- `agent/turn_checkpoint.py` checkpoint routing from the AIAgent route fields.
- Focused regression tests under `tests/agent/` and `tests/gateway/`.
- `docs/regressions/REG-2026-08-25-002.md` and this contract.
- Backed-up display/configuration normalization for active Windows and VPS Hermes profiles.
- Bounded gateway restarts, process/config identity checks, zero-visible-window evidence, and delivery probes.
- PR, merge, ancestry proof, and cleanup of the exact repair branch.

## Out of Scope
- Treating the Concursa/Japa message as an incident; the user confirmed the wrong bot was mentioned and that runtime is a working reference.
- Replacing NF/PF identities, tokens, sessions, Kanban state, worker architecture, or project data.
- Adding Telegram to Exocortex or altering dormant platform configuration unrelated to an active route.
- New routing, memory, browser, or orchestration architecture.
- Deleting unrelated uncommitted work, using stash, force-pushing, or rewriting Git history.

## Failure Signal / Repro
- At 2026-08-25 12:15 UTC, the VPS NF composed the requested Kanban response but logged `checkpoint rejected delivery obligation binding` and suppressed its durable final.
- The failed checkpoint route contained `platform=telegram` with an empty `chat_id`, while the delivery event contained the real Telegram group route.
- The incident and prevention record exists at `docs/regressions/REG-2026-08-25-002.md`.
- Profile inventory showed implicit Telegram defaults hiding tool progress and explicit visibility drift, including reasoning disabled and an invalid background-progress value.

## Root-Cause Hypothesis
- Fact: AIAgent stores gateway route identity in `_chat_id` and `_thread_id`, while checkpoint initialization read only public field names and therefore serialized an empty route.
- Fact: reused cached agents refreshed callbacks and model settings per message but did not refresh route identity.
- Fact: visibility defaults and profile overrides drifted independently; NF/PF worker settings still exist and must be preserved.
- Chosen repair: refresh route identity on every message, read the actual AIAgent route fields at checkpoint creation, cover both paths with regression tests, and normalize only the active platform visibility settings with backups and profile-specific invariants.

## Claim Discipline
- `implemented` means the source/config delta exists.
- `validated-local` requires focused tests, compilation, diff checks, and contract validation.
- `validated-target` requires new process identity, loaded configuration evidence, zero visible windows, connected active platforms, and a real delivery probe.
- `released` requires merged `main` and installation of that exact source/config candidate.
- `accepted` requires the user's real interaction with the restored agents.

## Forbidden Actions
- Do not delete, overwrite, reset, or rewrite Hermes sessions, memories, Kanban state, project data, or bot credentials.
- Do not change bot ownership, Telegram group/topic bindings, or add a Telegram route to Exocortex.
- Do not promote the new Hermes release to the VPS before all pre-promotion checks pass; restore the 2026-08-24 symlinks immediately if any bounded target check fails.
- Do not stash, force-push, reset unrelated work, or delete a branch before proving its tip is contained in merged `main`.
- Do not launch a visible Windows terminal, browser, console, or dialog.

## Loop Control
- A controlled autonomous micro-loop is not required because each source/config/restart change has one deterministic probe and an explicit rollback; retries require changed evidence.
- Maximum repair iterations: 3 for a given failing acceptance path.
- Green condition: all local gates pass, active profile invariants are preserved, and bounded target delivery/runtime probes pass.
- Escalation: stop on credential replacement, destructive data/schema action, a third identical failure without new evidence, or any target restart that does not recover through the declared rollback.

## Validation Plan
- Run cached-agent, checkpoint, durable-delivery, display, and platform-focused tests; then compile checks, `git diff --check`, and the canonical contract validator.
- Dry-run and apply backed-up visibility changes only to each profile's active platforms; compare NF/PF worker/activity invariants before and after.
- Restart only bounded active gateways through the hidden launcher; prove new PIDs, actual executable/source/config identity, platform health, and `VisibleWindows=0`.
- Exercise a real route/delivery probe without changing bot ownership or project data; retain the 2026-08-24 VPS release until the candidate is proven.
- Open the PR, wait for required green checks, merge normally, prove ancestry, switch to merged `main`, and delete the exact repair branch locally/remotely without stash.

## Status
- Contract preflight: complete; the current bounded contract passed the canonical preflight validator.
- Implementation: complete for the source route repair and backed-up visibility normalization on Windows and VPS profiles.
- Validation: complete at validated-local with 84 focused tests, zero-visible-window execution, and active Windows gateway/AOF load probes; the VPS remains on the functional 2026-08-24 release while NF target promotion waits for idle.
- Completion: source candidate complete at validated-local; PR integration and the deliberately deferred NF restart/target acceptance are not yet claimed complete.
