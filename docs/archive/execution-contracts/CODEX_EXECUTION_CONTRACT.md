# CODEX Execution Contract

## Contract Metadata
- Mode: IMPLEMENT
- Risk Level: medium
- Workspace: `C:/Users/maiko/AppData/Local/hermes/hermes-agent`
- Updated At: 2026-08-13
- Parent contract: `docs/HERMES_PROJECT_OS_EXECUTION_CONTRACT.md`

## Requested Outcome
- Correct the confirmed management-Topic regression so `🧭 Gestão` remains a persistent ACL/routing control plane without being treated as a normal project Kanban board.

## In Scope
- `gateway/run.py`
- `tests/gateway/test_project_router_gateway.py`

## Out of Scope
- Bootstrap/profile/config/SOUL writes.
- Telegram API calls, Topics, permissions, bindings, or live smoke tests.
- Router schema or provisioning changes.
- Broad refactors, unrelated dirty files, Git commits, pushes, releases, and rejected roots.

## Failure Signal / Repro
- A resolved `ProjectContext` with `is_management=True` currently reaches `router.ensure_bound_board(project_context)` unconditionally.
- The same context injects its synthetic `board_slug` and `workdir` through `_set_session_env()`.
- `_project_context_prompt_block()` advertises the synthetic value as `authoritative_board` and tells the model to use it.
- This contradicts the specification: `🧭 Gestão` is the team control plane and must not represent a common project board.

## Root-Cause Hypothesis
- Facts: `is_management` is persisted and reaches the gateway; three consumers ignore it when ensuring a board, binding session board/workdir, and rendering prompt instructions.
- Assumptions: persistent management project identity remains necessary for ACL, binding, restart persistence, and task-local `project_topic_create`.
- Chosen fix point: branch only at those three gateway consumers; preserve router schema and management callback.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects or behavior changes for ordinary project Topics.
- No placeholders, fake values, temporary keys, or config overrides.
- Do not create a second management work queue or route ordinary Kanban operations to a management board.

## Validation Plan
- Analyze/lint: `python -m compileall -q gateway/run.py`; `git diff --check`.
- Unit tests: focused management prompt, session env, board ensure, and callback tests in `tests/gateway/test_project_router_gateway.py`.
- Integration/contract tests: project router gateway + project board routing + project Topic creation + session context nearest suites.
- Build/install/deploy checks: not applicable to this slice.
- Manual smoke checks: deferred until Telegram groups are Forums and bots have `can_manage_topics`.

## Status
- Contract preflight: pass — validated by the official execution-contract checker.
- Implementation: pass — management context remains persistent for ACL/routing and callback, but no physical board is created or injected.
- Validation: pass-local — focused RED→GREEN prevention tests, adjacent router/tool/session suites, compileall and diff-check passed.
- Completion: validated-local; target smoke remains deferred until Telegram Forum and bot permissions exist.
