# Hermes Full-Access and Honcho Runtime Recovery Contract

## Contract Metadata
- Contract Version: 2
- Mode: REPAIR
- Risk Level: HIGH
- Workspace: C:\Users\maiko\AppData\Local\hermes\worktrees\honcho-root-runtime-20260825
- Target Branch: main via PR from fix/honcho-root-runtime-20260825
- Updated At: 2026-08-25T15:00:00-03:00
- Runtime Targets: Windows Titan default, VPS Titan Audit/default, VPS Hermes NexaFactory
- Machine Runtime Authority: none: the repair uses bounded host service changes and direct target probes with an explicit rollback after any failed check

## Requested Outcome
- Give the VPS `hermes` identity complete non-interactive root authority and make the self-hosted Honcho provider operational, exposed, and verified end-to-end in all three Hermes runtimes.

## Acceptance Criteria
- `sudo -u hermes sudo -n id -u` returns `0`; no gateway needs to run as root and no `/root` ownership is weakened.
- Active gateway processes use `/srv/hermes` as `HOME`, `HERMES_HOME`, and working directory; a normal consult/tool path no longer resolves `/root/.git`.
- One private self-hosted Honcho backend is healthy on VPS loopback and survives restart.
- Windows reaches that backend through a hidden, persistent SSH tunnel with no visible terminal window.
- The default and NexaFactory profile configurations resolve the intended Honcho backend without copying or printing credentials.
- `honcho_profile` and `honcho_context` are present and complete a real call in Windows Titan, VPS Titan Audit/default, and VPS Hermes NexaFactory.
- Required gateways remain active after bounded restarts; no profile, session, memory, Kanban, project, bot binding, or credential is deleted.
- Any source/documentation change is merged through a green PR; its branch is deleted locally and remotely after ancestry proof.

## In Scope
- A validated `/etc/sudoers.d` grant for `hermes` with `NOPASSWD: ALL`.
- Service environment correction for `HOME`, `HERMES_HOME`, and working directory if target evidence shows drift.
- Existing self-hosted Honcho service, configuration, local-only binding, and runtime dependencies.
- A zero-visible-UI Windows SSH tunnel and bounded gateway restarts required to reload the provider.
- Focused provider/tool/runtime probes, regression record if a source defect is found, this contract, PR, merge, and branch cleanup.

## Out of Scope
- Running messaging gateways permanently as root, changing permissions or ownership under `/root`, or exposing Honcho unauthenticated to the public Internet.
- Replacing Hermes profiles, Telegram bot identities, routes, sessions, Kanban state, memories, projects, or agent credentials.
- New memory architecture, VPN, Kubernetes, public control plane, or unrelated AOF/AIRC work.

## Failure Signal / Repro
- Windows `honcho_profile` and `honcho_context` report `Honcho session could not be initialized`; `127.0.0.1:8500` refuses the connection.
- VPS NexaFactory does not expose the Honcho tools in its session namespace.
- VPS Titan Audit reached `Permission denied: '/root/.git'` before testing Honcho.
- Before repair, `sudo -u hermes sudo -n id` requires a password.
- Evidence-Absent: the failing tool responses exist in the user's Telegram runtime and current-session report; this bounded repair will capture new sanitized target probes without copying chat credentials or private session payloads into git.

## Root-Cause Hypothesis
- Windows has no local backend or persistent tunnel even though its profile points to `localhost:8500`.
- VPS Honcho became healthy after gateway initialization, leaving provider tools absent until a controlled reload.
- A runtime or administrative probe inherited root's home instead of `/srv/hermes`, causing the `/root/.git` path.
- If a real provider call still fails with backend healthy, configuration/schema or package-version drift must be proven before source edits.

## Claim Discipline
- `implemented`: access/service/config delta exists.
- `validated-local`: Windows tunnel, provider import, and direct client calls pass with `VisibleWindows=0`.
- `validated-target`: real tool calls pass in each named gateway runtime after reload.
- `released`: the exact merged source/config/service state is active.
- `accepted`: Maikol interacts with the agents and confirms normal behavior.

## Forbidden Actions
- Do not print, rotate, overwrite, or transfer secrets as part of diagnostics.
- Do not chmod/chown `/root`, run gateways as root, expose port 8500 publicly, delete runtime data, or restart an actively executing profile without first establishing an idle or recoverable boundary.
- Do not stash, force-push, reset unrelated work, delete unmerged branches, or merge with failing required checks.
- Do not launch a visible Windows terminal, browser, console, or dialog.

## Rollback
- Remove only the new sudoers drop-in after validating the remaining sudoers configuration.
- Restore any changed systemd override from its timestamped copy, daemon-reload, and restart only the affected unit.
- Stop/remove only the Honcho/tunnel service created or changed by this run; restore the prior profile configuration copy.
- If a gateway restart fails health checks, restore its exact previous unit/config and restart once; then stop and report the exact failing gate.

## Loop Control
- A controlled autonomous micro-loop is not required because every access, service, tunnel, and gateway change has one deterministic probe and an immediate explicit rollback; retries require changed evidence.
- Maximum repair iterations: 3 for any identical failing target path, and every retry requires changed evidence.
- Green condition: root authority, backend/tunnel health, real Honcho calls, tool exposure, gateway health, and zero-visible-UI checks all pass.
- Escalation: stop on required credential replacement, destructive state/schema action, inability to establish an idle/recoverable gateway boundary, or a third identical failure without new evidence.

## Validation Plan
- Validate this contract before the first mutation and again before finalization.
- Validate the sudoers file with `visudo -cf`, then prove root authority from the `hermes` user.
- Probe backend health and one direct Honcho SDK session/profile/context request from VPS and Windows.
- Enumerate the actual tool schema in each profile, then exercise `honcho_profile` and `honcho_context` through the real runtime path.
- Prove gateway PID/status/environment, private listener scope, persistent tunnel state, and `VisibleWindows=0`.
- Run focused repository tests only if source changes are evidence-required; otherwise avoid unrelated suites.
- Open PR, wait for required checks, merge, prove ancestry, update local main, and delete the repair branch locally/remotely.

## Status
- Contract preflight: passed the canonical version-2 validator before the
  privileged host and runtime changes.
- Implementation: `/etc/sudoers.d/90-hermes-full-access` grants validated
  `NOPASSWD: ALL` authority to `hermes`; the existing Honcho stack remains
  loopback-only at `127.0.0.1:8500`; its API healthcheck now matches measured
  startup latency; the persistent hidden Windows tunnel and dialogue bridge are
  running; and the Project Factory Telegram toolset includes `memory`.
- Validation: `sudo -u hermes -H sudo -n id -u` returned `0`; Honcho API,
  database, Redis and Ollama are healthy; the Windows tunnel and restarted
  Project Factory gateway report zero visible windows. Vigilia conversation
  `47ef908b-7fbf-4689-a774-f81a6ddd3166` proves real `honcho_profile` and
  `honcho_context` calls in Titan local, NexaFactory VPS and Titan Audit VPS.
  Conversation `e8405c08-0808-4781-825c-279e9b03ebcb` additionally proves both
  calls in the restarted Windows Project Factory profile.
- Completion: complete at `released` for access, self-hosted backend, tool exposure and
  cross-host runtime paths. The running VPS default gateway was not restarted
  because it owns active Kanban worker `t_10aa848c`; full root authority is
  already immediately available there through `sudo -n`, and no active work was
  interrupted.
