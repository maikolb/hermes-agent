# Historical workspace protocol execution record

This is preserved implementation evidence from the isolated workspace-protocol worktree. It is not an active execution-contract authority; the repository authority remains `docs/EXECUTION_CONTRACT.md`.

## Contract Metadata
- Mode: BUILD_THEN_VERIFY
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Projetos\hermes-workspace-api-worktree`
- Updated At: 2026-08-12T19:43:00-03:00

## Requested Outcome
- Add a backward-compatible public JSON-RPC/WebSocket contract for persistent conversation sessions, turns, active-turn redirects, cancellation, status, and normalized events required by Hermes Workspace Portal.

## In Scope
- Public web server/gateway JSON-RPC registration, a focused adapter module if needed, protocol documentation, and focused tests in this isolated worktree.
- Reuse of existing Hermes agent session, redirect, callback, and event mechanisms.

## Out of Scope
- The dirty primary Hermes worktree, unrelated CLI/kanban/gateway behavior, model/provider configuration, dependency upgrades, or broad server refactors.
- Portal implementation, GitHub integration, deployment, release tagging, or changes to user credentials/configuration.

## Failure Signal / Repro
- The portal previously imported `HermesCLI`, invoked `_init_agent`, and assigned internal callbacks in `hermes_bridge.py`; `hermes serve` exposed session creation but no stable remote turn/redirect/event contract for this client.

## Root-Cause Hypothesis
- Facts: active-turn redirect and normalized internal callbacks already existed; the public server used PTY/dashboard-specific paths for chat execution.
- Assumptions: a backward-compatible JSON-RPC extension could delegate to existing primitives without changing CLI behavior.
- Chosen fix point: a narrow public protocol layer with ordered redirect buffering and contract tests.

## Forbidden Actions
- No changes outside the declared protocol surface and its tests/docs.
- No edits, reset, staging, cleanup, or commits in the primary dirty Hermes worktree.
- No breaking changes to existing `session.create`, dashboard, ACP, CLI, or Kanban contracts.
- No real model calls, secrets, placeholders, fake success, temporary keys, or config overrides.

## Validation Plan
- Analyze/lint: compile changed Python files and run existing style/static checks when available.
- Unit tests: protocol validation, idempotency, ordered redirects, cancel/status, and event envelope.
- Integration/contract tests: in-memory fake agent/session; no provider call.
- Build/install/deploy checks: `hermes acp --check` or targeted server import smoke where safe.
- Manual smoke checks: none requiring terminal UI; JSON-RPC test client only.

## Historical Status
- Contract preflight: validated with `validate_execution_contract.ps1` on 2026-08-12.
- Implementation: completed in the isolated worktree.
- Validation: focused contract suite, adjacent protocol/WebSocket suite, server regression suite, compile, import smoke, and Ruff completed.
- Completion: validated-local; target VPS/provider execution was not run in that historical task.
