# Hermes Native-Media Durable Delivery Repair Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Canonical Runtime Checkout: `C:\Users\maiko\AppData\Local\hermes\hermes-agent`
- Isolated Candidate Clone: `C:\Users\maiko\AppData\Local\hermes\scratch\fix-ceogame-delivery`
- Target Branch: `main` via `fix/media-checkpoint-exact-text`
- Updated At: 2026-08-25
- Active Target Before Repair: shared Windows checkout at `f67c919480`, CeoGame gateway PID 19636
- Accepted Baseline: `maikolb/main` at `c7015aadf8`
- Machine Runtime Authority: none: this bounded repair uses deterministic tests, a single profile-scoped restart, and direct Telegram/checkpoint/ledger readback rather than an autonomous implementation loop

## Requested Outcome
Restore the CeoGame Telegram bot so a generated response containing native image attachments is delivered as text plus images, without weakening exact-once text delivery or changing unrelated profiles.

## Acceptance Criteria
- The exact user message at 22:03 BRT remains present in the CeoGame session source.
- A response containing `MEDIA:` directives is resealed to the exact post-extraction text before a delivery obligation is bound.
- Text delivery, native image delivery, ledger completion, and checkpoint digest/status all succeed in regression coverage.
- Stale or already-bound checkpoint rewrites remain fail-closed.
- The stranded CeoGame checkpoint is repaired only after proving no Telegram send and no ledger row occurred.
- Only the CeoGame gateway is restarted for target validation; no visible Windows UI is created.
- The recovered text and three original PNGs are delivered to Telegram topic `2` and confirmed by transport receipts/readback evidence.

## In Scope
- `agent/turn_checkpoint.py`
- `gateway/run.py`
- `gateway/platforms/base.py`
- `tests/gateway/test_delivery_ledger_producer.py`
- `docs/regressions/REG-2026-08-25-004.md`
- This execution contract
- The exact CeoGame checkpoint and gateway process required for recovery

## Out of Scope
- Telegram bindings, bot identity, credentials, model/provider settings, sessions other than the affected CeoGame session, other Hermes profiles, Project Ops implementations, architecture, authentication, and unrelated working-tree changes.

## Failure Signal / Repro
- Telegram update `620925243` was received and the model generated a 458-character reply in 8.4 seconds, but the gateway logged `delivery obligation content does not match its checkpoint digest`; no text or image reached the topic.
- Automatic recovery repeated the same failure because the checkpoint had been bound without a ledger row.
- The durable incident evidence and exact prevention surface are recorded in `docs/regressions/REG-2026-08-25-004.md`.

## Root-Cause Hypothesis
- Fact: the checkpoint sealed the full response containing three `MEDIA:` directives.
- Fact: platform extraction reduced the text obligation to 241 characters.
- Fact: `record_obligation` rejected the SHA-256 mismatch after the checkpoint had been bound; no ledger row or network send existed, and attachments were suppressed.
- Confirmed root cause: the durable text boundary was sealed before platform media extraction instead of to the exact text actually passed to the transport.

## Claim Discipline
- `implemented` means the isolated candidate contains the shared reseal helper, platform-boundary call, and regression test.
- `validated-local` requires focused and adjacent tests, Ruff, diff checks, and the AOF execution-contract validator.
- `validated-target` requires a new CeoGame gateway PID, connected Telegram adapter, zero visible descendant windows, successful text/image receipts, and checkpoint/ledger delivery readback.
- `released` requires the committed candidate integrated into the canonical runtime checkout and loaded by the restarted CeoGame gateway.
- `accepted` requires natural user confirmation or later normal use after target validation.

## Forbidden Actions
- Do not weaken or disable the durable delivery ledger.
- Do not restart another Hermes profile or mutate another session, binding, credential, workspace, or bot.
- Do not discard, stash, reset, or overwrite unrelated modified/untracked files in the canonical checkout.
- Do not send a fabricated substitute response or regenerate the three existing image artifacts.
- Do not use either rejected Hermes Project Ops implementation or introduce new authentication, services, databases, routes, or protocols.

## Loop Control
- A controlled autonomous micro-loop is not required because this repair has one deterministic failing delivery boundary, one bounded source change, one focused regression, and one profile-scoped target probe with exact rollback.
- Maximum implementation/test iterations: 3.
- Green condition: local gates pass and the recovered CeoGame turn reaches delivered text plus three native image receipts with matching checkpoint/ledger state.
- Stop condition: any evidence of a prior external send, divergent checkpoint/ledger ownership, failure to preserve unrelated work, or inability to isolate the CeoGame restart.
- Rollback: revert the repair commit, restore the checkpoint backup, and restart only CeoGame.

## Validation Plan
- Run focused delivery/checkpoint tests and adjacent completion/media tests.
- Run Ruff, `git diff --check`, and the canonical AOF execution-contract validator.
- Preserve and compare the live checkout's unrelated modified/untracked files before integration.
- Commit and push the isolated candidate; integrate only the candidate commit into the canonical checkout with unrelated changes preserved.
- Repair the stranded checkpoint using the proven no-send/no-ledger evidence.
- Restart only `hermes-ceogame` through the canonical zero-UI gateway restart script.
- Verify new PID, Telegram connection, zero visible descendant windows, delivery receipts, checkpoint `delivered`, and ledger `delivered`.

## Status
- Preflight: complete.
- Implementation: complete in isolated candidate.
- Validation: `validated-local`; 92 focused/adjacent tests passed, Ruff passed, and diff check passed.
- Completion: not claimed; contract validation, commit/integration, profile-scoped restart, and Telegram target validation remain pending.
