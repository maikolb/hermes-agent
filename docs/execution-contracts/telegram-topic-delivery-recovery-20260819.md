# Telegram Topic Delivery Recovery — Execution Contract

## Outcome
Prevent concurrent Telegram Topics in one forum chat from losing final replies when the polling transport temporarily degrades, while preserving the exact persisted Topic envelope during recovery.

## Scope
- `gateway/delivery_ledger.py`
- `gateway/platforms/base.py`
- `gateway/run.py`
- `plugins/platforms/telegram/adapter.py`
- focused delivery, Telegram health, and Topic isolation tests
- profile-scoped zero-UI restart/verification helper outside the repository

## Out of scope
- project code, boards, authentication, memberships, AWS, or the rejected Project Ops implementations
- unrelated modified/untracked files already present in the checkout
- artificial test messages in real project Topics

## Failure signal and root cause
- Real concurrent traffic reached Topics `4`, `6`, and `41` with distinct sessions and transcripts.
- During the Topic `41` final response, Telegram polling entered `send_path_degraded` before outbound network I/O.
- The final obligation was marked `failed`; same-process polling recovery did not run the startup-only recovery sweep, so the final reply disappeared while transient UI/status remained.
- The earlier Topic status-cache isolation was necessary but insufficient for this second delivery-loss boundary.

## Implemented contract
- Adapters explicitly signal a proven pre-network rejection with `send_attempted=false`.
- Such obligations become `deferred`, not delivered, failed, or uncertain.
- A degraded-to-healthy Telegram transition atomically claims only the live owner's deferred rows.
- Replay uses the persisted `chat_id`, `thread_id`, session key, and content.
- A repeated preflight rejection returns the row to deferred without consuming the real-attempt budget.
- Pending/deferred rows do not receive an ambiguity marker; potentially attempted rows preserve existing conservative recovery semantics.

## Validation evidence
- Focused suite against the working tree: `54 passed in 13.42s`.
- Same suite against the isolated staged Git tree: `54 passed in 16.73s`.
- `git diff --cached --check`: pass before commit.
- Code commit: `05b29a0e275668fd6fd97d1f381c27d0a7a8d997`.
- Published branch: `maikolb/hermes-agent:fix/telegram-topic-delivery-recovery-20260819`.
- Gateway restart: old PID `34760` drained; new PID `26596` is live.
- Runtime: `gateway_state=running`, Telegram `connected`, zero visible descendant windows.
- Restart recovery: one interrupted session auto-resumed.
- Target delivery: obligation `b320ed068d30dd0dc625689a` changed from `failed` to `delivered`; gateway log records recovery to `thread=41`, attempt `1`.

## Readiness
- implemented: true
- validated-local: true
- validated-runtime: true
- validated-target for the lost Topic `41` reply: true
- accepted: true — Maikol confirmed normal use is working correctly without the original user-visible mixing/disappearance. No artificial messages were injected into project Topics.

## Discovery promotions
- Updated skill: `messaging-runtime-reliability` with deferred preflight/reconnect replay semantics and regression requirements.
- Created helper: `C:/Users/maiko/AppData/Local/hermes/scripts/restart-hermes-profile-gateway-zero-ui.py`.
- Helper verification artifact: `C:/Users/maiko/AppData/Local/hermes/profiles/hermes-project-factory/restart-zero-ui-result.json` (`ok=true`).
