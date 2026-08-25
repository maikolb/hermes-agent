# Hermes Runtime Repair Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Target Branch: integrate/local-runtime-v2-20260820
- Updated At: 2026-08-24T19:30:00-03:00
- Machine Runtime Authority: none: a controlled autonomous micro-loop is not required because each profile repair is a bounded reversible config or package operation with an explicit probe and no automatic retries
- Event Evidence: focused tests, Honcho API probes, per-profile write/retrieve probes, service status, and sanitized gateway logs

## Requested Outcome
- Restore persistent Honcho memory for every active Hermes profile on Windows and the VPS, fix profile-aware Honcho status resolution, and deploy the validated repair without changing bot routing or Telegram ownership.

## Acceptance Criteria
- One self-hosted Honcho stack is reachable only through localhost on the VPS and an SSH localhost tunnel on Windows.
- Honcho text generation and embeddings use local models on the existing KVM8; no Honcho Cloud subscription or paid model API is used.
- `honcho-ai` is installed in both active Hermes runtimes.
- The six Windows profiles and three VPS profiles use explicit, stable workspace, human-peer, and AI-peer mappings.
- Every profile can write a unique conclusion/message and retrieve it through the real Honcho SDK path.
- `hermes honcho status --all` resolves the same profile host keys as runtime configuration.
- Existing Telegram gateways, bot tokens, topic bindings, sessions, and project state remain unchanged unless a bounded restart is needed to load the validated memory configuration.

## In Scope
- `plugins/memory/honcho/cli.py`
- Focused Honcho CLI tests under `tests/`
- `docs/EXECUTION_CONTRACT.md`
- `docs/regressions/REG-2026-08-25-001.md`
- Windows Hermes profile `config.yaml` and `honcho.json` files
- VPS Hermes profile `config.yaml` and `honcho.json` files
- Self-hosted Honcho, its local inference dependency, the localhost SSH tunnel, and the existing per-profile Honcho configuration
- Active Windows and VPS Hermes Python environments

## Out of Scope
- New memory provider architecture, live config inheritance between profiles, changing Telegram bot identities, changing project repositories, or enabling inactive VPS profiles.
- Public exposure of Honcho, deletion of existing sessions/memories, credential rotation, or destructive database migration.

## Failure Signal / Repro
- Evidence artifact: `C:\Users\maiko\AppData\Local\hermes\profiles\hermes-project-factory\logs\gateway.log` records the live profile's Honcho connection failures.
- Windows profile logs contain repeated connection refusals to `localhost:8500`; no listener exists there.
- VPS profile logs report that `honcho-ai` is not installed; no listener exists on `localhost:8500`.
- The current `--all` status path composes profile host keys with a dot while runtime resolution uses the canonical underscore form.

## Root-Cause Hypothesis
- Facts: all configured profiles point to a nonexistent backend; the VPS runtime lacked the client SDK; one profile has a stale/mis-scoped host mapping; ExoCortex does not currently select the Honcho provider; the status CLI uses a different host-key format from runtime resolution; and the KVM8 has sufficient free memory and disk for a bounded local model stack.
- User decision: Honcho must be self-hosted without Honcho Cloud or paid model APIs.
- Chosen repair: deploy the official Honcho Docker stack and a local OpenAI-compatible inference service on the KVM8, bind both to localhost, align explicit profile mappings, install the SDKs, and correct the status-key composition with a focused regression test.

## Claim Discipline
- `implemented` means code/config/runtime exists.
- `validated-local` requires focused tests and Windows SDK write/retrieve.
- `validated-target` requires write/retrieve for all nine profiles plus healthy VPS service and tunnel after restart.
- `released` requires the Hermes source repair to be pushed and the validated runtime/config installed.
- `accepted` requires Maikol's real interaction with the agents.

## Forbidden Actions
- Do not delete, overwrite, or regex-rewrite existing Hermes session databases or memories.
- Do not change Telegram bot tokens, topic bindings, gateway ownership, or activate additional VPS bots.
- Do not expose a local Honcho service on a public interface or print secrets in logs/output.
- Do not reset, stash, discard, force-push, or overwrite unrelated work.
- Do not launch visible Windows UI.

## Loop Control
- A controlled autonomous micro-loop is not required because this repair uses one bounded change and one explicit verification per profile; failures stop for changed evidence instead of iterating autonomously.
- Maximum repair iterations: 3 per failing acceptance path.
- Green condition: all nine profile probes pass and the status-key regression is green.
- Escalation: stop on missing required provider credential, destructive schema change, repeated identical failure twice without new evidence, or a required user-controlled authentication step.
- Retry requires a changed config, code delta, runtime state, or new diagnostic evidence.

## Validation Plan
- Run focused Hermes Honcho tests through `scripts/run_tests.sh` in the supported runtime.
- Validate self-hosted Honcho and local inference health from the VPS and through the Windows localhost SSH tunnel.
- Execute sanitized SDK write/retrieve probes for all six Windows and three VPS profiles.
- Compare gateway/service status and configuration hashes before and after any restart.
- Run `git diff --check` and the canonical execution-contract validator before finalization.

## Status
- Contract preflight: green on all three canonical contracts.
- Implementation: self-hosted stack, local inference, Windows tunnel, SDKs, profile mappings, ExoCortex provider selection, and status-key repair installed.
- Validation: 68 focused tests green; health is green; all six Windows and three VPS identities passed SDK write/retrieve; the derived-memory queue completed 16/16 work units and produced 14 conclusions, including successful semantic retrieval of the self-hosted decision.
- Completion: self-hosted Honcho repair is complete at `validated-target`; user interaction acceptance remains pending, and Vercel GitHub-App validation is separately deferred until Bernardo can supply the email code.
