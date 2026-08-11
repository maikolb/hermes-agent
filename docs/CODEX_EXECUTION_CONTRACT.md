# AOF Execution Contract — Hermes Project Ops Phase 2 Shared Portal

## Contract Metadata

- Contract Version: 2
- Contract Revision: 8
- Mode: BUILD
- Risk Level: HIGH
- Workspace: `C:\Users\maiko\Projetos\Hermes Agent Project Ops`
- Updated At: 2026-08-10
- Machine Runtime Authority: `C:\Users\maiko\Projetos\Hermes Agent Project Ops\AGENT_LOOP_RUN.json`

## Requested Outcome

Deliver the first real Hermes Project Ops vertical slice inside the maintained `maikolb/hermes-agent` fork:

- a built-in responsive route at `/project-ops`;
- project → board/topic navigation;
- each topic/card is linked to one durable Hermes `session_id`;
- prompt submission and streamed agent events use the existing Gateway JSON-RPC protocol, never a PTY;
- multiple authenticated clients may opt into the same live session and receive the same runtime events;
- Kanban board, task detail, comments, runs and warnings use the existing kanban plugin API and database;
- no duplicate agent runtime, task store, database, BFF or enterprise authorization layer.

This contract covers the web vertical slice and the shared-session transport seam required by it. Android derivation begins only after this contract is green, committed, pushed and visually validated.

### Writer, repository and phase controls

- Mode: `BUILD`
- Branch: `feature/project-ops-core`
- Repository: `C:\Users\maiko\Projetos\Hermes Agent Project Ops`
- Product authority: `C:\Users\maiko\Projetos\Hermes Project Ops`
- Single implementation writer: Codex (`gpt-5.6-sol`, reasoning `high`)
- Hermes is orchestrator/reviewer/validator and performs commit/push only after independent local green.
- No worktree. No second active writer.
- Never modify `C:\Users\maiko\AppData\Local\hermes\hermes-agent`.

## Authorities

Read before editing:

1. repository `AGENTS.md`;
2. this contract;
3. `C:\Users\maiko\Projetos\Hermes Project Ops\docs\PRODUCT_SPEC.md`;
4. `C:\Users\maiko\Projetos\Hermes Project Ops\docs\UX_TEAM_COLLABORATION.md`;
5. `C:\Users\maiko\Projetos\Hermes Project Ops\docs\ARCHITECTURE.md`;
6. `tui_gateway/ws.py`;
7. `tui_gateway/server.py` (`write_json`, live transport registry, session teardown and live-session reuse);
8. `tui_gateway/methods_session.py` (`session.create`, `session.resume`);
9. `web/src/lib/gatewayClient.ts`;
10. `web/src/App.tsx`;
11. `plugins/kanban/dashboard/plugin_api.py`.

The repository, tests, this contract and direct validation evidence outrank chat summaries.

## User Value

A team member opens Project Ops, selects a project and a topic, sees the shared transcript and board, sends a message to the same Hermes agent, and every subscribed teammate sees the same live events. This mirrors the operational simplicity of a Telegram group/topic while adding the visual project and Kanban surfaces.

## Failure Signal / Repro

The phase is failed if any of these is true:

1. two WebSocket clients subscribing to the same runtime do not both receive the same session event;
2. one event is delivered twice to the same transport;
3. disconnect leaves a closed transport registered as an observer;
4. ordinary TUI/Desktop clients change behavior without opting into shared-session subscription;
5. `/project-ops` submits through `/api/pty`, `/api/chat` or terminal emulation;
6. a task/topic is created without a durable `session_id` link;
7. retries create duplicate task/topic rows despite the same idempotency key;
8. the page uses mocked projects, tasks, transcripts or run state in production code;
9. the web build or focused Python/TypeScript tests fail;
10. any file outside the allowlist is modified;
11. tests call a live LLM/provider or incur spend;
12. the operational Hermes checkout or the Android phone is modified in this phase.
13. disconnect/subscribe interleaving can leave a live observer behind `_detached_ws_transport` or let the orphan reaper remove its runtime;
14. a client-provided author prefix can impersonate another authenticated member;
15. non-loopback token/insecure mode receives the local `Owner` fallback;
16. topic creation can create duplicate active tasks for one idempotency key, or an ambiguous/reloaded retry cannot recover the durable session created by the same operation;
17. selecting a non-launch dashboard profile still reads/writes that profile's `projects.db` or `state.db` through the launch profile;
18. a stale board/topic request overwrites the current selection or leaks an observer subscription.
19. two `session.create` calls with one Project Ops `creation_key` can register different live runtimes for the same profile/session;
20. `session.resume` can replace a persisted Project Ops source with a client-controlled source and bypass backend attribution;
21. the active-task UNIQUE idempotency index or its deduplication is rolled back when the initialization connection closes;
22. TTL/LRU eviction can close a session while `_session_observers` still contains a live observer;
23. an explicit missing/invalid profile silently falls back to the launch profile's `state.db`;
24. clearing the selected topic can leave an in-flight open subscribed and restore stale state.
25. the documented `profile=current` launch-profile alias is rejected as a missing named profile, preventing isolated/current-profile UI operation.
26. hydrated mobile Board content expands the grid's implicit `auto` row beyond the 844px viewport, clipping the bottom switcher inside an overflow-hidden page.
27. even with an explicit minmax grid row, `/project-ops` is mounted in a generic `display:block;height:auto` route wrapper, so the page still has no bounded flex height.

## Root-Cause Hypothesis

The core already has almost every required primitive:

- first-class Hermes projects and project tree RPCs;
- Kanban boards/tasks with `project_id`, `session_id` and `idempotency_key`;
- Gateway `session.create`, `session.resume`, `prompt.submit` and streaming events;
- authenticated dashboard identity plus RFC 8252 native auth and WS tickets;
- responsive React/Vite dashboard and authenticated fetch helpers.

The missing collaboration seam was transport fan-out. The first implementation added opt-in observers, but independent review on 2026-08-10 returned `request-changes` and established four additional root causes:

1. disconnect promotion/detach and observer subscription mutate related state in separate lock windows; subscribe also does not promote a detached runtime, so the existing orphan timer can reap a session that acquired a live observer;
2. the SPA serializes identity into arbitrary prompt text while the authenticated WS ticket identity is discarded by `_ws_auth_reason`; therefore the backend has no authoritative author binding;
3. topic create stores its operation only in a React ref and `kanban_db.create_task` uses a non-unique check-before-transaction idempotency index; crash, ambiguous response and concurrent retry are not recoverable;
4. `session.create/resume` already support `profile`, but Project Ops omits it, and project DB access in the Kanban plugin runs under the launch profile. Kanban boards themselves remain intentionally root-shared and must not be incorrectly converted to per-profile storage.

Post-green review proved six residual root causes: create claims the deterministic
durable key only after unconditionally registering a fresh runtime; resume trusts
the request source instead of the persisted source; the migration opens a write
transaction without committing it before the initialization connection closes;
TTL/LRU predicates ignore observers; `_profile_home()` overloads launch-profile
and invalid-profile as the same `None`; and the React no-selection branch neither
invalidates the open sequence nor unsubscribes the previous runtime.
Target QA additionally proved `_validated_session_profile` documents launch/current
but passes the literal `current` through named-profile lookup instead of resolving it
to the launch profile.
Post-fix CDP bounds proved the single-row responsive grid uses an implicit `auto` row:
the mobile board reached y=1318 and pushed navigation to y=1372 while document height
remained 844, so neither content nor switcher was reachable.
The first containment patch loaded successfully but semantic CDP remained red; an
ephemeral proof applying the existing Chat/Docs full-height wrapper classes to the
Project Ops route reduced the grid to 405.75px, made Board internally scrollable and
placed navigation at y=760.5–814.

The minimal repair keeps the existing stores and protocols: make observer transition/reaper checks atomic; propagate verified WS ticket/local-loopback identity to the transport and derive the transcript prefix server-side; make both durable session creation and active task creation idempotent by one stable operation key; persist the pending operation client-side only as recovery state, never authority; and scope only `projects.db`/`state.db` operations while preserving shared Kanban boards.

## Claim Discipline

- Facts already established: the Gateway, project tree, Kanban APIs, authenticated dashboard identity and native-auth seams exist; Phase 1 remotely linked task creation to `session_id` at commit `b7225abbd35721d66cf6fa402808d63f1bc8f2b9`.
- Inferences that still require validation: opt-in observer fan-out can preserve existing singular-client behavior; the built-in React page can combine Gateway and Kanban APIs without a new backend.
- Highest readiness state allowed by current evidence: `specified`.
- Target readiness checklist or equivalent: implementation, focused Python/TypeScript tests, production web build, contract validation, external green, independent review, commit/push/read-back, and visual desktop/mobile validation.

## Forbidden Actions

- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Do not modify the operational Hermes checkout, Android device, provider credentials, deployment configuration or public infrastructure.
- Do not call live LLM providers or incur spend in tests.
- Do not commit or push from the Codex writer lane.
- Do not add PostgreSQL, Redis, BFF, multi-tenancy, enterprise RBAC or a second task/session/runtime store.

## Loop Control

- Qualification: controlled loop required because this changes shared-session transport semantics and a production UI route.
- Maximum initial build/test/fix iterations: 4 (exhausted).
- Post-green independent-review repair: one supervised focal execution; no automatic retry loop.
- Target-QA alias repair: one direct focal execution limited to `profile=current` plus regression.
- Target-QA mobile containment repair: one direct focal execution limited to the Project Ops grid row plus regression.
- Target-QA route-height repair: one direct focal execution limited to the `/project-ops` wrapper in `App.tsx` plus regression.
- Stop condition: `PROJECT_OPS_PHASE2_GREEN_PASS` or any contract/scope/security blocker.
- Escalation rule: stop after repeated identical failure, missing authority, scope conflict, provider requirement or unauthorized file change; Hermes reviews evidence before any restart.
- Runtime authority path: `C:\Users\maiko\Projetos\Hermes Agent Project Ops\AGENT_LOOP_RUN.json`.
- Append-only evidence path: `C:\Users\maiko\AppData\Local\hermes\evidence\project-ops-phase2-aof-events.jsonl`.
- Controller iteration log: `C:\Users\maiko\AppData\Local\hermes\evidence\project-ops-phase2-spec-loop.jsonl`.

## In Scope

### A. Shared-session Gateway seam

1. Add an RPC such as `session.subscribe` that:
   - requires a valid live runtime `session_id`;
   - binds `current_transport()` as an observer of that runtime;
   - is idempotent for the same transport;
   - returns a small structured acknowledgement.
2. Add an optional unsubscribe RPC only if needed for deterministic tests/UI cleanup; disconnect cleanup remains mandatory regardless.
3. Fan session events to:
   - the primary session transport;
   - every opted-in observer;
   - each distinct transport at most once.
4. A failed observer write must not prevent delivery to healthy peers.
5. WebSocket disconnect must unregister the transport from every observer set before/while existing session teardown runs.
6. Existing global broadcasts and ordinary singular-session behavior remain unchanged.

### B. Project Ops web route

1. Add built-in route and navigation item `/project-ops` to the existing React dashboard.
2. Reuse the existing dashboard layout, theme tokens, auth handling, mobile sidebar and shared components.
3. Load real project/board/task data from `/api/plugins/kanban/*`.
4. Project selection scopes the visible boards; board selection loads `/board?board=<slug>`.
5. A task with `session_id` opens as a topic:
   - resume its durable session through Gateway JSON-RPC;
   - subscribe the current WS transport to the returned runtime;
   - render persisted transcript and subsequent streaming events.
6. Creating a topic:
   - creates a Gateway session with source `project_ops`, project cwd and title;
   - creates the Kanban task using `project_id`, `session_id` and a stable client idempotency key;
   - never dispatches an autonomous worker merely because the collaborative topic exists.
7. Prompt submission:
   - uses `prompt.submit`;
   - includes stable visible member attribution derived from `/api/auth/me` when gated;
   - uses the local Owner identity fallback only in loopback mode;
   - persists attribution in the transcript in a deterministic Telegram-style prefix that the UI parses into an author label;
   - never accepts an arbitrary production identity when an authenticated Session is available.
8. Render at minimum:
   - project rail or selector;
   - topic/task list;
   - shared chat transcript with user/assistant distinction and streaming state;
   - composer with send and disabled/busy handling;
   - board summary grouped by status;
   - task detail drawer with status, assignee, comments, runs/warnings and read-only evidence.
9. Responsive behavior:
   - desktop supports simultaneous navigation/chat/board/detail where space permits;
   - mobile uses progressive disclosure without horizontal overflow;
   - composer and active topic remain usable at Android viewport widths.
10. Empty, loading, auth failure, disconnected WS and API error states must be explicit.

### C. Tests And Evidence

1. Focused Python tests for subscription, deduplication, fan-out, unsubscribe/disconnect cleanup, bad session id and non-subscriber compatibility.
2. Focused TypeScript tests for:
   - author-prefix encode/decode;
   - Gateway event reducer;
   - project/board/topic selection;
   - idempotent topic-create payload;
   - responsive/empty/error states at component level where practical.
3. Existing related Gateway WS, Kanban plugin and task-session tests stay green.
4. `web` production build succeeds.

### D. Mandatory Review Corrections

1. Disconnect/subscribe/reaper:
   - detach or promote under the same `_sessions_lock` used by subscription;
   - subscribing to a detached runtime promotes a live transport before the reaper predicate can claim teardown;
   - the reaper revalidates both primary transport and observer membership atomically;
   - a deterministic interleaving test must fail on the reviewed implementation and pass after the fix.
2. Authoritative identity:
   - WS ticket consumption retains verified `user_id`, `provider` and display name on the WebSocket transport;
   - loopback token mode may synthesize only `Owner/local-owner`;
   - non-loopback token/insecure mode has no Owner fallback and Project Ops prompt submission fails closed without authoritative identity;
   - Project Ops clients send raw prompt text; the backend creates the deterministic attribution prefix and ignores any client claim of another author.
3. Idempotent topic-create saga:
   - one stable operation key is persisted before the first side effect and survives reload/retry;
   - `session.create` with source `project_ops`, `persist=true` and the same operation key returns or identifies the same durable session after ambiguous response/restart;
   - active Kanban tasks enforce one row per non-null idempotency key at the database layer and concurrent losers read back the winner;
   - existing archived-task semantics remain backward compatible;
   - pending/failed creation is visible and retryable, and successful reconciliation clears pending client recovery state.
4. Profile scope and stale work:
   - selected dashboard profile is sent to `session.create/resume`;
   - access to per-profile `projects.db`, including project validation during task creation, runs in the selected profile;
   - root-shared Kanban board/task storage stays shared;
   - board loads use sequence/abort protection and stale topic opens unsubscribe any runtime they attached.

## Out of Scope

- Android clone, APK generation or phone installation in this phase;
- app-store publishing or production deployment;
- PostgreSQL, Redis, BFF or second task/session database;
- multi-tenancy, SSO administration, enterprise RBAC or per-project ACL;
- roles beyond inherited authenticated member plus local Owner fallback;
- billing, payment, organization/departments or enterprise audit productization;
- replacing existing Desktop/TUI/Telegram clients;
- autonomous worker dispatch, writer lease redesign or Kanban lifecycle redesign;
- file upload, voice, reactions, edit/rewind and rich Markdown beyond what is needed for the vertical slice;
- broad refactor of `tui_gateway/server.py`, `web/src/App.tsx` or Kanban plugin internals;
- provider calls, model benchmarking or network spend in tests.

## Architectural Boundaries

1. `projects.db`, `kanban.db` and `state.db` remain authoritative in their existing domains.
2. One topic links to one durable session; clients share the runtime rather than cloning it.
3. Observer subscription is opt-in and transport-local; no behavior change for clients that never subscribe.
4. Session event delivery deduplicates transport identity.
5. Closing an observer connection does not close the shared session unless existing primary-session lifecycle rules independently require it.
6. Dashboard auth remains the HTTP/WS security boundary. Do not expose new public routes.
7. Web API calls use `fetchJSON`/`authedFetch`; WebSocket auth uses the existing ticket/token helper.
8. No production mocks, localStorage-only task authority or hard-coded sample project data.
9. UI changes extend the built-in React app; do not edit generated `web/dist` or plugin minified bundles.
10. Preserve existing public API response fields and add only backward-compatible fields/RPCs.

## Allowed Files

Codex may modify or create only:

- `docs/CODEX_EXECUTION_CONTRACT.md`
- `AGENT_LOOP_RUN.json`
- `.aof/runtime/schemas/agent-loop-event.schema.json`
- `tui_gateway/server.py`
- `tui_gateway/methods_session.py`
- `tui_gateway/ws.py`
- `tui_gateway/methods_prompt.py`
- `hermes_state.py`
- `hermes_state_common.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/web_server.py`
- `hermes_cli/dashboard_auth/routes.py`
- `hermes_cli/dashboard_auth/ws_tickets.py`
- `plugins/kanban/dashboard/plugin_api.py`
- `tests/tui_gateway/test_session_subscribe.py`
- `tests/tui_gateway/test_project_ops_identity.py`
- `tests/test_tui_gateway_ws.py` only if an existing fixture must be extended
- `tests/hermes_cli/test_dashboard_auth_ws_auth.py`
- `tests/hermes_cli/test_dashboard_auth_ws_tickets.py`
- `tests/hermes_cli/test_kanban_db.py`
- `tests/hermes_state/test_project_ops_creation_key.py`
- `tests/plugins/test_kanban_dashboard_plugin.py`
- `tests/plugins/test_kanban_board_project_api.py`
- `web/src/App.tsx`
- `web/src/lib/api.ts`
- `web/src/lib/gatewayClient.ts`
- `web/src/lib/projectOps.ts`
- `web/src/lib/projectOps.test.ts`
- `web/src/pages/ProjectOpsPage.tsx`
- `web/src/pages/ProjectOpsPage.test.tsx`
- files under `web/src/pages/project-ops/`

Any other file requires stopping and updating this contract before editing.

## Validation Plan

- Analyze/lint: `git diff --check` plus TypeScript/Vite build diagnostics.
- Unit tests: focused Python fan-out tests and focused TypeScript reducer/payload/component tests.
- Integration/contract tests: existing Gateway WS, Kanban plugin and task-session suites.
- Build/install/deploy checks: production web build; Android install is explicitly deferred to a later contract.
- Target or environment checks: local dashboard visual smoke in desktop and Android-sized browser viewports after commit/push.
- Delivery pipeline checks: staged-tree secret scan, commit/push to `origin/feature/project-ops-core`, remote SHA read-back.
- Manual smoke checks: project selection, topic creation, session resume, two-client event convergence, board/detail rendering and error states.

### Exact commands

### Python focused

```bash
PYTHONPATH=src;tests .venv/Scripts/python.exe -m pytest \
  tests/tui_gateway/test_session_subscribe.py \
  tests/tui_gateway/test_project_ops_identity.py \
  tests/test_tui_gateway_ws.py \
  tests/hermes_cli/test_dashboard_auth_ws_auth.py \
  tests/hermes_cli/test_dashboard_auth_ws_tickets.py \
  tests/hermes_cli/test_kanban_db.py::test_concurrent_idempotent_creates_return_one_active_winner \
  tests/hermes_cli/test_kanban_db.py::test_active_idempotency_index_survives_reopen \
  tests/hermes_state/test_project_ops_creation_key.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/plugins/test_kanban_board_project_api.py \
  tests/hermes_cli/test_kanban_project_link.py -q
```

### Web focused

```bash
npm --prefix web run test -- \
  src/lib/projectOps.test.ts \
  src/pages/ProjectOpsPage.test.tsx
```

### Web production build

```bash
npm --prefix web run build
```

### Structural validation

```bash
.venv/Scripts/python.exe -m compileall \
  tui_gateway/server.py \
  tui_gateway/methods_session.py \
  tui_gateway/methods_prompt.py \
  tui_gateway/ws.py \
  hermes_state.py \
  hermes_state_common.py \
  hermes_cli/kanban_db.py \
  hermes_cli/web_server.py \
  hermes_cli/dashboard_auth/routes.py \
  hermes_cli/dashboard_auth/ws_tickets.py \
  plugins/kanban/dashboard/plugin_api.py

git diff --check

"C:/Users/maiko/AppData/Local/Microsoft/WindowsApps/pwsh.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass \
  -File "C:/Users/maiko/Projetos/AgentOperatingFramework/framework/runtime/validate_execution_contract.ps1" \
  -RepoRoot "C:/Users/maiko/Projetos/Hermes Agent Project Ops" \
  -ContractPath "docs/CODEX_EXECUTION_CONTRACT.md"
```

The PowerShell validator command must be preflighted through `windows-no-external-ui-guard.py` before execution.

## Acceptance Criteria

1. A focused test creates one live session, attaches two fake transports with `session.subscribe`, emits one event and proves both receive the same frame exactly once.
2. A transport that is both primary and observer receives one copy.
3. Disconnect cleanup removes observer membership and later emits do not write to it.
4. Unknown session subscription returns a JSON-RPC error and creates no registry state.
5. Existing WS tests stay green.
6. `/project-ops` appears in built-in navigation and renders without a plugin bundle.
7. No `/api/pty` call exists in Project Ops production source.
8. Project Ops uses a real `GatewayClient`, real Kanban endpoints and real task `session_id` values.
9. Topic creation sends stable `idempotency_key`, `project_id` and `session_id` fields.
10. Selecting a linked task resumes/subscribes to that session and hydrates persisted messages.
11. Two UI client models fed the same fan-out events converge on the same transcript state in tests.
12. Web build and focused Python/TypeScript suites pass.
13. Diff stays inside the allowed files and has no secret material.
14. A deterministic disconnect/subscribe/reaper interleaving retains the live runtime and promotes the observer.
15. A forged `[name|user_id]` client prefix is rendered under the verified transport identity, not the claimed identity.
16. Gated WS tickets propagate verified identity; Owner fallback succeeds only for a loopback token connection and fails closed for non-loopback Project Ops submission.
17. Two concurrent `create_task` calls with the same idempotency key produce one active row and both return its id.
18. Repeating `session.create` with the same Project Ops operation key after an ambiguous response resolves to the same durable session.
19. Reloaded pending topic creation reconciles session + task without creating a duplicate and exposes a retry state while incomplete.
20. Selecting a non-launch profile reads its projects and creates/resumes its session while continuing to use the shared Kanban board.
21. Stale board responses cannot overwrite the current board, and stale topic opens unsubscribe their observer.
22. Same-profile concurrent/retried Project Ops creation returns one live runtime and schedules one agent build.
23. Resume uses persisted `source=project_ops` as authority, so a client-supplied legacy source cannot bypass attribution.
24. TTL and LRU predicates are false while any live observer is registered.
25. Closing and reopening a migrated Kanban DB retains the partial UNIQUE index and its canonical winner.
26. An explicit invalid or missing profile returns a deterministic RPC error and creates no launch-profile session.
27. Topic → no-selection invalidates pending opens and unsubscribes any previous/newly-resolved runtime.
28. Successful task creation records `task_session_linked`; permanent task-create failure closes the runtime as `orphaned_create` and preserves the operation for deterministic retry.
29. Explicit `profile=current` resolves to the launch profile, while other missing/invalid explicit profiles still fail closed.
30. At 390x844 after task-detail hydration, Topics/Chat/Board remain inside the viewport; the visible panel owns vertical overflow and the bottom switcher stays reachable.
31. `App.tsx` mounts `/project-ops` through the same bounded min-h-0 flex route-host contract used by other full-height surfaces, without changing unrelated routes.

## Finish Line

The phase is complete only when:

1. focused Python tests pass with non-zero collection;
2. focused TypeScript tests pass with non-zero collection;
3. the web production bundle builds;
4. the repository contract validator passes;
5. the external green guard prints `PROJECT_OPS_PHASE2_GREEN_PASS`;
6. Hermes independently reviews the diff and reruns the green guard;
7. a single reversible commit is pushed to `origin/feature/project-ops-core`;
8. the exact remote SHA is read back;
9. the portal is then launched locally and visually validated in desktop and Android-sized browser viewports before Android derivation starts.

A launcher exit code, background PID, generated bundle, screenshot without interaction, or Codex self-report alone is not completion evidence.

## Phase 1 Evidence

- Previous phase commit: `b7225abbd35721d66cf6fa402808d63f1bc8f2b9`
- Remote branch: `origin/feature/project-ops-core`
- Pull request: `https://github.com/maikolb/hermes-agent/pull/1`
- Focused Phase 1 regression: 35 tests passed.
- Phase 1 added the backward-compatible `session_id` request field and database read-back test.

## Regression Checks

- `REG-2026-08-10-001` — status: `validated`; failure signal: persistent
  Project Ops `session.create` ignored `creation_key`, so an ambiguous retry
  after restart selected a different durable session (and could collide on the
  title). Root cause: the handler unconditionally used `_new_session_key()`.
  Prevention artifact: `tests/hermes_state/test_project_ops_creation_key.py`.
  Validation command: `.venv/Scripts/python.exe -m pytest tests/hermes_state/test_project_ops_creation_key.py -q`
- `REG-2026-08-11-002` — status: `validated`; failure signal: the external
  checker stopped at `required marker missing in tui_gateway/methods_prompt.py:
  project_ops`, while Project Ops accepted client-authored attribution text.
  Root cause: `prompt.submit` did not bind the prompt to the authenticated
  transport identity. Prevention artifact:
  `tests/tui_gateway/test_project_ops_identity.py`. Validation command:
  `.venv/Scripts/python.exe -m pytest tests/tui_gateway/test_project_ops_identity.py -q`

## Status

- Contract preflight: passed under PowerShell 7.6.4 with execution-contract and agent-loop Definition validators.
- Implementation: validated-local; shared lifecycle/auth/persistence and responsive Project Ops portal are implemented.
- Validation: `PROJECT_OPS_PHASE2_GREEN_PASS`; 99 Python tests; 20 web tests; production build with 2,205 modules; two-client API/WebSocket flow; desktop 1440x900 and mobile 390x844 CDP with hydrated task detail, no horizontal overflow, no console errors, internally scrollable Board and reachable bottom switcher.
- Completion: Phase 2 core/web is validated-local and published to `origin`; Android derivation may start next.
- Android/APK/install: not started; no APK or device smoke claim exists yet.
- Commit/push: core `0e10e746a8a26d17fa64d6e5d9c294842bc70c0e`; web `4ff76d7eb67bdcda866fc858f1579342a442669c`; remote branch read-back matched `4ff76d7eb67bdcda866fc858f1579342a442669c`.
