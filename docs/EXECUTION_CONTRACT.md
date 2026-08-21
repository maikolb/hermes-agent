# CODEX Execution Contract

## Contract Metadata
- Mode: IMPLEMENT_THEN_VERIFY
- Risk Level: HIGH
- Workspace: C:\Users\maiko\Projetos\hermes-bug-intake-20260821
- Updated At: 2026-08-21T00:00:00-03:00

## Requested Outcome
- Implement the first safe Bug Intake milestone: exact-JID passive WhatsApp routing before Titan conversation handling, isolated durable and idempotent per-project spooling, and a second deny guard on every WhatsApp egress path.

## In Scope
- `docs/EXECUTION_CONTRACT.md`.
- `scripts/whatsapp-bridge/passive_intake.js`.
- `scripts/whatsapp-bridge/bridge.js`.
- `plugins/platforms/whatsapp/adapter.py` only for validated config-to-bridge wiring.
- `docs/whatsapp-passive-intake.md`.
- Focused JavaScript and Python tests for route validation, exact matching, replay idempotency, project isolation, fail-closed behavior, and egress denial.
- Minimal operator documentation for configuring passive routes without activating them.

## Out of Scope
- The live Hermes checkout and live runtime configuration.
- Starting, stopping, restarting, or reloading Hermes, Titan, WhatsApp, or any Windows service.
- Discovering groups through active WhatsApp calls, sending messages, marking messages read, or emitting presence.
- Jira, Confluence, Kanban, topic posting, curation-model execution, platform report forms, weekly reports, production release, and repository changes in ConcursaIA or DOVCRM.
- Real group JIDs, tokens, QR data, phone numbers, credentials, secrets, or production activation.

## Failure Signal / Repro
- The current bridge processes inbound group messages through ordinary `fromMe`, self-chat, allowlist, content extraction, and Python queue logic. It has no exact-JID pre-dispatch diversion to a project-isolated spool.
- Current HTTP egress handlers can send, edit, upload media, create polls, share locations, type, or mark read for any syntactically valid destination; they have no passive-intake destination deny guard.
- Therefore adding Titan to an intake group without this change can mix the group with ordinary Titan state or allow accidental egress.

## Root-Cause Hypothesis
- Facts: routing decisions currently happen after the transport has entered normal conversational handling, and outbound handlers know nothing about passive intake routes.
- Assumptions: Baileys group JIDs remain stable exact identifiers; configuration is available before the socket is constructed; bridge-local durable files are acceptable for the shadow-stage handoff.
- Chosen fix point: a pure validated passive-intake module initialized at bridge startup, called immediately after obtaining `remoteJid`, plus the same route registry enforced in all outbound adapters.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects or behavior changes for DMs, unregistered groups, broadcasts, or status messages.
- No edits in the live Hermes checkout and no runtime cutover from this worktree.
- No placeholders, fake values, temporary keys, real JIDs, secrets, or config overrides.
- No network calls, browser UI, QR flow, WhatsApp egress, read receipts, typing, reactions, presence, or Jira/Confluence writes.
- No raw phone, JID, token, QR, media key, or unredacted exception content in logs or filenames.
- A matched passive route must remain consumed even when persistence fails; it must never fall through into Titan.

## Validation Plan
- Analyze/lint: import the passive-intake module with Node and run syntax/static checks available in the repository.
- Unit tests: exercise strict configuration validation, exact JID routing, duplicate replay, per-project paths, pseudonymization, atomic persistence, and every egress guard.
- Integration/contract tests: prove a matched group is spooled and omitted from the Titan queue while an unmatched DM retains current behavior; prove invalid enabled configuration prevents startup.
- Build/install/deploy checks: run the repository-supported Node test suite and the relevant Python test wrapper; no deploy or live restart in this milestone.
- Manual smoke checks: inspect the diff for secret/JID leakage and run the no-visible-window verifier with `VisibleWindows=0` if the repository validation launches any long-lived process.

## Status
- Contract preflight: validated before implementation.
- Implementation: implemented in the isolated feature worktree; no live files or runtime configuration changed.
- Validation: validated-local. The passive-intake Node tests pass 5/5; the complete WhatsApp bridge Node suite passes 32/32; the focused Python adapter/config suite passes 6/6; syntax, bytecode, diff checks, and zero-visible-window verification pass.
- Completion: complete for the declared local milestone. Real JID identification, live configuration, restart, shadow observation, media-byte capture, curation, and downstream Jira/Kanban work remain separate operational milestones.

## Evidence
- Exact routing and egress tests cover the protected route plus unmatched group and DM destinations.
- Replay writes one create-if-absent record; the cleartext report and sender do not appear in the encrypted spool fixture.
- The same sender receives different pseudonyms across the ConcursaIA and DOVCRM project fixtures.
- Expanded Python regression run: 36 tests passed and one unrelated baseline test failed because its pre-existing mock omits delivery acknowledgements already required by the existing `send()` implementation; no changed hunk touches that path.
- Windows verifier: `Status=validated`, `HookShellDescendants=0`, `VisibleWindows=0`.
