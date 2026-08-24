# Execution Contract — Telegram Topic Board Lifecycle

## Contract Metadata
- Mode: CHANGE_THEN_VERIFY
- Risk Level: high
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Updated At: 2026-08-18
- Iteration Budget: 3 implementation attempts

## Requested Outcome
- When Telegram reports that a bound project Topic was closed, mark that binding closed, archive the canonical board without deleting or moving its data, and remove it from active board discovery when no open binding or other active project still needs it.
- When Telegram reports that the Topic was reopened, reactivate the same project and board without creating a replacement board.

## In Scope
- `plugins/platforms/telegram/adapter.py`
- `gateway/project_router.py`
- `gateway/run.py`
- `hermes_cli/kanban_db.py`
- Focused tests under `tests/gateway/` and `tests/hermes_cli/`.
- `C:\Users\maiko\Projetos\claude-codex-live-view\server.py` plus its focused discovery test, because Vigília is the real active-board consumer.
- This task-specific contract.

## Out of Scope
- Management Topics, other messaging platforms, task/card semantics, authentication, UI, deployment architecture, hard deletion, legacy archive migration, and the rejected Hermes Project Ops implementations.
- Pre-existing unrelated working-tree changes.

## Failure Signal / Repro
- The Telegram adapter registers `FORUM_TOPIC_CREATED` but not `FORUM_TOPIC_CLOSED` or `FORUM_TOPIC_REOPENED`.
- The gateway recognizes only `telegram_forum_topic_created` and therefore leaves a closed Topic's project and board active indefinitely.

## Root-Cause Hypothesis
- The transport-to-domain lifecycle bridge is incomplete: close/reopen service updates are not converted into gateway metadata, and `ProjectRouter` has no persisted per-binding closed state or idempotent board lifecycle transition.
- The smallest contract-preserving fix is to emit close/reopen metadata at the Telegram adapter, persist `topic_bindings.is_closed`, and reconcile project status plus the existing board metadata `archived` flag transactionally through `ProjectRouter`.

## Forbidden Actions
- Do not delete or move a board directory, database, cards, comments, events, runs, logs, attachments, or workspace.
- Do not archive the management Topic's synthetic board slug.
- Do not archive a board still required by another open binding or active project.
- Do not create a replacement board while reopening or processing a duplicate close event.
- Do not reset, discard, stage, or commit unrelated working-tree changes.
- Do not open visible UI or use prohibited Hermes Project Ops roots, ports, variables, databases, routes, launchers, or protocols.

## Validation Plan
- RED: focused adapter/router/gateway/Kanban tests fail before implementation.
- GREEN: focused tests cover close, duplicate close, reopen, management exclusion, shared bindings, shared boards, rollback on board metadata failure, hidden active listing, and no agent session for service events.
- Regression: affected gateway/project-router/Kanban suites pass.
- Static: `python -m py_compile` on changed Python files and `git diff --check` on the isolated delta.
- State isolation: exercise lifecycle against temporary `HERMES_HOME` and SQLite databases; verify the same board DB hash/path survives close and reopen.
- Target: restart only `hermes-project-factory` through its canonical hidden launcher; verify new live PID, connected Telegram gateway, and zero visible windows. Do not close a real user Topic as an automated test.

## Status
- Contract preflight: validated-local
- Implementation: implemented
- Validation: validated-local — 17/17 regressões novas; 134 testes afetados; 1 teste Vigília; `py_compile`; `git diff --check`; SQLite/router integrity `ok`; mesmo SHA-256 do board antes/depois.
- Target readiness: validated-target — Hermes PF PID 29860 via Scheduled Task, Telegram `connected`, `is_closed` migrated, SQLite `ok` with 0 FK errors; Vigília PID 33096 via native WMI→`pythonw`, `/api/kanban/boards` HTTP 200, both process trees with zero visible windows.
