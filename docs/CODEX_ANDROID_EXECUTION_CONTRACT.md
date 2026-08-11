# CODEX Android Execution Contract — Hermes Project Ops

## Contract Metadata

- Contract Version: 1
- Contract Revision: 3
- Mode: BUILD
- Risk: HIGH
- Workspace: `C:/Users/maiko/Projetos/Hermes Agent Project Ops`
- Android root: `android/`
- Branch: `feature/project-ops-core`
- Backend authority commit: `4fc64b8d356a19ae28cefaab3f57ae9cf83c9523`
- Upstream Android commit: `c4de4226c52e1c4e0c17b31760b8d87912ca2dec`
- Target device: Samsung SM-S918B, Android 16/API 36, arm64-v8a, 1440x3088, density 600

## Requested Outcome

Produce a derivative-owned native Kotlin/Jetpack Compose Android client that exposes Project → Topic → Chat → Board → Drawer against the already-authenticated Hermes Dashboard/Gateway, opens the exact server-owned `session_id`, preserves backend-owned identity/source, builds a debug APK, installs alongside any stock Hermes app, and passes real-device smoke validation.

## In Scope

- Entire imported `android/` source only as required by the modular derivation.
- `android/UPSTREAM.md`, inherited MIT `LICENSE`, derivative README/provenance.
- Derivative package/application/deep-link identity.
- A `projectops` model/repository/viewmodel/UI seam using the inherited REST and Gateway clients.
- Navigation from the native product shell to Project Ops and from a topic to its existing conversation/session.
- Unit/contract/UI tests, Gradle locks/build, APK metadata/signature/checksum.
- Host JDK/Gradle/ADB commands and target package install/smoke.

## Out of Scope

- Backend changes after commit `4fc64b8d3`.
- Installing the stock/upstream Hermes APK as a substitute.
- Flutter, WebView portal embedding, BFF, second task database, PTY, PostgreSQL, multi-tenancy, SSO or enterprise RBAC.
- Reusing upstream `applicationId`, custom URI scheme, release signing identity or trademarks as derivative ownership.
- Root, bootloader, OEM unlock, factory reset, account/permission changes or unrelated device data.
- Provider/model calls during build and smoke.

## Source and environment facts

- Imported source is MIT and contains 329 files from exact upstream archive SHA-256 `d8764e5ee5d4d12247861ba447e0368a2fc596377045f1a6b10ab021146330a4`.
- Upstream package is `com.nousresearch.hermes`; debug adds `.debug`.
- Target derivative package must be `com.maikolb.hermesprojectops`; debug may add `.debug`.
- Kotlin 2.1.20, AGP 8.9.2, Gradle 8.11.1 with pinned distribution hash, compile/target SDK 36, min SDK 28.
- Host JDK is user-local Temurin 21.0.12+8 at `C:/Users/maiko/AppData/Local/Programs/Temurin/jdk-21.0.12+8`, downloaded from official Adoptium release and checksum-verified; `java.exe` Authenticode signer is Eclipse Foundation.

## Frozen backend contract

### REST

All calls use inherited authenticated Dashboard cookies/session handling and current backend/profile scoping.

- `GET /api/plugins/kanban/projects` → `{ "projects": [...] }`
- `GET /api/plugins/kanban/boards` → `{ "boards": [...], "current"?: string }`
- `GET /api/plugins/kanban/board?board={encodedBoardSlug}` → `{ "columns": [...], "latest_event_id": number }`
- `GET /api/plugins/kanban/tasks/{encodedTaskId}?board={encodedBoardSlug}` → `{ "task": {...}, "comments": [...], "runs": [...], "events": [...] }`
- Project identity: `id`, `name`, optional workspace/folder metadata.
- Board identity: `slug`, `name`, `project_id`.
- Topic/task identity: `id`, `title`, `status`, `project_id`, nonblank `session_id`; comments/runs/events are drawer evidence when returned.
- Android must not infer project/session from timestamps, titles or local storage.

### Gateway

- Resume the server-owned transcript with `session.resume` using `stored_session_id=<task.session_id>`, selected profile and `source=project_ops`.
- Subscribe with `session.subscribe` using the returned live runtime id.
- Existing conversation sends raw user text through the inherited prompt path; the backend determines Project Ops identity attribution and source.
- The client must not prefix participant identity, downgrade source, invent session IDs or start a second session for an existing topic.
- On selection change/disconnect, stale subscriptions must be released by inherited lifecycle behavior.

## P0 UX contract

- Add a first-class Project Ops destination to the native shell; do not relabel Chats/Command Center.
- Phone: progressive Topics / Chat / Board switcher, with Project and Board selectors and one visible pane at a time.
- Tablet: adaptive multi-pane layout may show Topics + Chat + Board concurrently when existing adaptive primitives permit.
- Topics lists only tasks belonging to the selected project/board and exposes loading, empty and authenticated error states.
- Chat opens/resumes the exact selected task session and preserves the existing conversation renderer/composer/approvals.
- Board shows status summary and a task drawer/detail containing at least status, assignee, session, comments/runs/evidence when available.
- No PTY or terminal pane.
- Back/relaunch must reconcile against server-authoritative projects/tasks/session before enabling mutations.

## Derivative identity

- `applicationId = com.maikolb.hermesprojectops` with debug suffix `.debug`.
- App label: `Hermes Project Ops`.
- Version begins at `0.1.0` / code `1`.
- Custom scheme must be derivative-owned, e.g. `hermes-project-ops`; inherited stock `hermes://` must not be claimed.
- Kotlin namespace may remain internal upstream namespace to avoid unsafe mass rename, but installed package, URI authority, provenance component name and user-facing label must be derivative-owned.
- Preserve MIT license and upstream attribution.

## Failure Signal / Repro

1. Upstream baseline build cannot be distinguished from host/toolchain failure.
2. Project Ops uses unauthenticated HTTP or duplicates auth/cookie storage.
3. Task tap creates or resumes a session different from `task.session_id`.
4. Client sends/persists caller-controlled Project Ops source/identity as authority.
5. Different projects/tasks leak selections or transcripts.
6. Empty/error/reconnect state silently renders fake data.
7. Mobile Board/detail clips the bottom switcher or horizontal content.
8. APK package collides with stock Hermes or cannot install alongside it.
9. APK provenance cannot be tied to source commit/package/version/signature/hash.
10. Build passes locally but real SM-S918B launch/navigation/session smoke is absent.

## Root-Cause Hypothesis

- The audited upstream app already owns mature authentication, Keystore, REST, JSON-RPC/WebSocket, session and conversation UI, but has no Project/Task/Kanban product entity.
- The smallest safe derivation is a modular `projectops` seam that consumes the frozen authenticated backend APIs and routes selected server-owned session IDs into the inherited conversation surface.
- Replacing auth/transport, embedding the web portal, relabeling Chats or creating a local task store would bypass existing authority and create drift.

## Implementation constraints

- One writer: Codex gpt-5.6-sol high under `HERMES_DELEGATED_CHILD_CONTEXT=1` and board `project-ops-android`.
- Small contract-preserving changes; prefer a `projectops` package and existing interfaces.
- Do not alter inherited auth/Keystore/Gateway protocol semantics except adding typed Project Ops calls/methods.
- No placeholder chat, fake API data or WebView fallback.
- No commit/push until unit/static/build gates and independent file inspection pass.
- Installation only after artifact identity gates pass.

## Forbidden Actions

- Do not install the upstream/stock APK as the requested derivative.
- Do not change or duplicate auth, Keystore, Gateway transport, source/identity authority or task/session persistence.
- Do not add a WebView fallback, local fake project/task store, placeholder transcript or invented progress.
- Do not reuse the upstream installed package ID, custom scheme or release signing identity.
- Do not type tokens, credentials or device secrets; human authentication/permission remains a hard boundary.
- Do not root, unlock, reset, alter accounts or touch unrelated phone data.
- Do not commit/push before validation or install before APK provenance gates.

## Validation Plan

1. Exact upstream baseline outcome recorded separately from derivative build.
2. Project Ops model/JSON and repository tests cover auth path, URL encoding/scoping, empty/error and authoritative IDs.
3. Navigation/route tests prove Project Ops is first-class and topic session identity is stable.
4. ViewModel/UI tests cover project/board/task selection, stale response protection and phone progressive panes.
5. Existing affected upstream tests pass.
6. `./gradlew testDebugUnitTest lintDebug detekt assembleDebug --no-daemon` passes with user-local JDK.
7. APK inspected with `apkanalyzer`/`aapt`/`apksigner`: package, version, SDK, label, permissions, debuggable/signature.
8. SHA-256 recorded; APK bytes copied only from validated build output.
9. `adb shell pm list packages` proves no derivative collision before install.

## Target smoke acceptance

- Install exact validated APK on connected `SM-S918B` via ADB.
- Read back installed package/version/path and compare package/version to APK metadata.
- Launch derivative package and prove foreground activity without crash/ANR.
- Capture screenshot and UI hierarchy after launch.
- Configure/connect only through existing app onboarding; never type credentials. If onboarding requires human token/permission, report `target-blocked-human` rather than bypassing.
- Navigate to Project Ops, select seeded QA Project Ops / Final Shared Topic, verify same server `session_id`, Topics/Chat/Board, hydrated drawer and responsive target rendering.
- Run one non-provider smoke action only if it does not send a model prompt; no provider/model use.

## Finish line

- Android source: `validated-local` after all build/artifact gates.
- APK: `validated-local` only after metadata/signature/hash readback.
- Device: `released` only after exact package install/readback.
- Product: `accepted` only after real target Project Ops flow passes; otherwise state the precise human/network/config blocker.
- Commit/push exact Android changes to `origin/feature-project-ops-core` and verify remote SHA.

## Status

- Contract preflight: validated by the workspace AOF validator.
- Upstream import: implemented from exact audited archive; not yet committed.
- Upstream baseline: `assembleDebug` PASS. Unit suite ran 370 tests: 363 passed and 7 upstream Windows failures reproduced before derivative edits (four AndroidX DataStore temp-file rename failures and three Robolectric FileProvider cache/root failures). Treat this exact set as baseline noise; no new failure is allowed.
- Android Project Ops implementation: not started.
- APK/install/device smoke: not started.
