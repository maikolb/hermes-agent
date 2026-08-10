# CODEX Execution Contract — Project Ops Session Link

## Contract Metadata

- Mode: BUILD
- Risk: medium — persistence/linkage contract on the authenticated Kanban API
- Workspace: `C:/Users/maiko/Projetos/Hermes Agent Project Ops`
- Branch: `feature/project-ops-core`
- Base SHA: `49c632310dd6877302e8dfa92e740b0ceddb97b8`
- Writer: Codex, single writer
- Reviewer/orchestrator: Hermes/AOF
- Max implementation loops: 3
- Provider/network/spend: forbidden
- Commit/push/release: forbidden to Codex; Hermes performs these only after validation

## Requested Outcome

Allow an authenticated Kanban dashboard client to create a task linked to an existing Hermes session by passing `session_id` to `POST /api/plugins/kanban/tasks`.

## Failure Signal / Repro

`hermes_cli.kanban_db.create_task()` already accepts and persists `session_id`, and serialized tasks already expose it. `plugins/kanban/dashboard/plugin_api.py::CreateTaskBody` does not declare `session_id`, and the route does not pass it to `kanban_db.create_task()`. A client request containing `session_id` therefore succeeds but silently persists `NULL` under Pydantic's current extra-field behavior.

## Root-Cause Hypothesis

The session link was added to the Kanban domain for gateway-originated tasks but was not threaded through the dashboard create-task DTO and adapter call. This is a missing edge adapter field, not a missing database/domain feature.

## In Scope

- `plugins/kanban/dashboard/plugin_api.py`
- `tests/plugins/test_kanban_dashboard_plugin.py`
- this contract

## Out of Scope

- database migrations or changes to `hermes_cli/kanban_db.py`;
- Projects semantics or `project_id` changes;
- session creation, task/session saga, repair job, writer lease, Done/Reopen;
- dashboard frontend, Desktop, Android or Telegram;
- authentication middleware behavior;
- model tools, prompts, skills, profiles or gateway lifecycle;
- dependency changes;
- operational Hermes checkout or active profiles;
- commit, push, PR, release, provider/network calls.

## Forbidden Actions

All out-of-scope writes and actions above are forbidden. Codex must stop instead of widening the allowlist, changing dependencies, using provider/network access, touching the operational checkout, or committing/pushing/releasing.

## Required Behavior

1. `CreateTaskBody` accepts `session_id: Optional[str] = None`.
2. The route forwards `payload.session_id` to `kanban_db.create_task()`.
3. A request with an opaque non-empty session ID returns the same value in `response.task.session_id`.
4. A read-back from the Kanban DB returns the same value.
5. Omitting `session_id` preserves current `NULL` behavior.
6. No endpoint-level normalization or format restriction is added; the core domain continues to own semantics.
7. Existing `project_id`, idempotency, workspace, assignee and dispatcher-warning behavior remain unchanged.

## TDD Requirement

Add a focused regression test that fails on the base SHA because the API drops `session_id`, then passes after the two production wiring changes. The test must assert both API response and DB read-back, not merely Pydantic schema shape.

## Validation Plan

```bash
python -m pytest \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/plugins/test_kanban_board_project_api.py \
  tests/hermes_cli/test_kanban_project_link.py \
  -q
python -m compileall -q plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin.py
git diff --check
```

After the focal loop, Hermes will run the broader Kanban/plugin test slice and inspect the exact diff before staging.

## Stop / Escalation

Stop without editing outside the allowlist if:

- the regression cannot be reproduced on the base behavior;
- the fix requires a migration/domain change;
- a test exposes auth/session validation requirements not represented in current code;
- another writer modifies the workspace;
- any command requests credentials, network, provider access or external UI.

## Readiness Ceiling

This commit can reach `validated-local`. It does not implement the Project Ops saga, portal, Android client, writer leases or production release.

## Completion Evidence Required

- exact changed-file list;
- focused regression name and base failure explanation;
- green outputs;
- `git diff --check`;
- final diff summary;
- no untracked or unrelated changes;
- stable commit SHA and remote read-back after Hermes commits/pushes.

## Status

- Contract: validated by the global AOF execution-contract validator.
- Baseline: 34 focused tests passed in an isolated `.venv` with `PYTHONPATH` empty.
- Implementation: adapter wiring and focused regression applied locally.
- Local validation: 35 focused tests passed; compileall and `git diff --check` passed.
- Controller green: passed under Hermes after correcting command-path quoting in the external harness invocation; repository code required no corrective change.
- Commit/push/release: pending Hermes closeout.
