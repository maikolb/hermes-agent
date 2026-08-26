# Hermes Runtime Convergence Evidence — 2026-08-25

## Scope and authority

This record covers only the Windows Titan/default runtime and the canonical Hostinger VPS at `187.127.60.126`, profiles `default` and `hermes-project-factory`. It does not authorize changes to Ceogame, Exocortex, AOF, Honcho, Intake Hub, Nexa Factory OS, Project OS data, sessions, credentials, bot identities, or channel routing.

Readiness terms in this record are literal:

- `implemented`: source or release material exists;
- `validated-local`: isolated tests and local canary passed;
- `validated-target`: the real configured target passed its probes;
- `released`: every declared target loads the exact merged `main` SHA;
- `accepted`: Maikol confirmed normal user-visible behavior.

## Preserved baselines

| Target | Observed live baseline | Direct evidence | Rollback artifact |
| --- | --- | --- | --- |
| Windows default | `aac2ff879ca44ec3a5b77269eabfa423388b566d` | `gateway_state.json`, live PID/process command line, and process creation time | `C:\Users\maiko\AppData\Local\hermes\runtime-releases\hermes-agent-aac2ff879ca44ec3a5b77269eabfa423388b566d` |
| Hostinger default | release tree `986c77afbd96254977e9e5d8592e55116527c11c` | systemd `ExecStart`, live process executable, release `.git/HEAD`, and Git-blob comparison of gateway-critical files | `/usr/local/lib/hermes-agent.release-20260824-986c77af` |
| Hostinger `hermes-project-factory` | release tree `986c77afbd96254977e9e5d8592e55116527c11c` | systemd `ExecStart`, live process executable, release `.git/HEAD`, and Git-blob comparison of gateway-critical files | `/usr/local/lib/hermes-agent.release-20260824-986c77af` |

No baseline process, profile, session store, Kanban store, memory, credential, or bot routing was changed during discovery.

## Repository and runtime reconciliation

- Fork repository: `maikolb/hermes-agent`.
- Fork remote `main` at candidate selection: `c7015aadf8b8daae20cd42f7a8956f9487ebc980`.
- Official Nous upstream main observed during the audit: `f751a8c5467c41500e505d90cb0eb8b70929080f` at the first comparison; a later fetch advanced the tracking ref and does not change this candidate receipt.
- Fork versus upstream at the first comparison: 83 commits ahead, 45 behind, 193 changed paths, merge base `52bcfb47`.
- Local canonical checkout before promotion: `9477bfdc0c7668dcf753b6d979c4a289d730dc06`, behind fork remote `main`, with unrelated untracked `intake-final/` and `intake-validation/` preserved.
- No stash existed or was created.

The Windows process reported `aac2ff87`, but its editable installation resolved through a checkout that later advanced to `9477bfdc`; the running process therefore did not have an immutable on-disk source boundary.

The Hostinger processes imported from release `986c77af`, while both state files advertised `8f6dfb90`. The release predates the current imported-release identity resolver and preserved a stale state value. The gateway-critical `gateway/run.py` and `gateway/kanban_watchers.py` blobs match `986c77af`, so `8f6dfb90` is not the loaded source identity.

## Worker-focus and Project OS boundary

- Base first-worker/next-worker focus rotation was introduced by `8801464b` and is present in the Hostinger rollback release `986c77af`.
- The merged fork candidate `c7015aad` includes the later worker-focus projection from `d1e3fbb8`: elapsed `Trabalhando ha ...`, bounded redacted live tool/reasoning output, next-worker rotation, and cleanup when no worker remains.
- Tests at the baseline and candidate establish first-worker selection, edit-in-place rotation to the next worker, deletion at zero workers, and restart rehydration.
- Project OS does not implement the worker-focus rendering functions. This behavior remains a Hermes gateway/channel presentation concern; the extracted Project OS package is not cut over by this change.

## Configuration invariants

Read-only checks confirmed the intended user-facing policy on the declared profiles:

- `show_reasoning: true`;
- `show_commentary: true`;
- `thinking_progress: true`;
- `reasoning_effort: high`;
- `busy_input_mode: steer`;
- activity indicators enabled/configured where supported;
- `worker_focus_handoff: true` on the specialized PF/NF profile.

This convergence does not change those values.

## Immutable candidate

Candidate release:

`C:\Users\maiko\AppData\Local\hermes\runtime-releases\hermes-agent-c7015aadf8b8daae20cd42f7a8956f9487ebc980`

The build was materialized from the exact Git archive with `.hermes_build_sha` set to the full candidate SHA. Verification established:

- build marker equals `c7015aadf8b8daae20cd42f7a8956f9487ebc980`;
- `gateway/run.py` and `gateway/kanban_watchers.py` hashes equal their Git blobs;
- runtime identity resolves as `c7015aadf8b8daae20cd42f7a8956f9487ebc980` with source `build-file`;
- candidate module origins resolve inside the immutable release directory.

The rollback package was built and verified with the same method for `aac2ff879ca44ec3a5b77269eabfa423388b566d`.

## Local validation

Focused validation against the immutable candidate completed with:

`195 passed, 17 skipped, 5 subtests passed in 123.73s`

Covered surfaces included runtime/build identity, Kanban worker notifier/focus, activity rendering, busy-session steering, delivery and turn checkpoints, queued primary work, compaction continuity, Telegram, WhatsApp, Windows process broker, zero-visible-UI, code execution, and terminal execution.

The repository-wide Windows runner discovered 3,292 files (approximately 33,816 tests) and stopped on two ACP files with host-environment failures:

- IOCP invalid-handle behavior (`WinError 6`);
- unprivileged symlink/POSIX-path assumptions (`WinError 1314`).

Both exact files produced the same failure signatures against the immutable known-good Windows baseline and the immutable candidate. They are baseline-equivalent Windows test-infrastructure incompatibilities, not evidence of a candidate regression. They are not waived as a release gate: the repository Linux and OS-specific GitHub lanes must pass before target promotion.

## Isolated Windows canary

An isolated local-only candidate canary used:

- `HERMES_HOME`: `C:\Users\maiko\AppData\Local\hermes\runtime-canary-c7015aad`;
- loopback-only API: `127.0.0.1:18987`;
- no real bot credentials, profiles, or production session stores;
- base `pythonw.exe`, not a visible console launcher.

Observed receipt:

- PID `96596`;
- imported identity `c7015aadf8b8daae20cd42f7a8956f9487ebc980`;
- API/session store healthy;
- `GET /health` returned status `ok`, platform `hermes-agent`, version `0.20.5`;
- visible-window enumeration returned an empty set.

The canary was then stopped by exact PID after confirming zero active agents and zero descendants. No real gateway was restarted.

## Gates still required before release

- Green GitHub repository and OS-specific CI on the exact PR head.
- Normal PR merge, followed by an immutable release built from the exact merged `main` SHA.
- Idle/recoverable-state proof immediately before each real restart.
- Windows target: new PID, exact imported SHA/module origins, unchanged profile/session/config invariants, connected configured adapters, zero visible windows, and channel smoke/readback.
- Hostinger target: one-profile canary first, then the other profile only after green; exact release path/SHA, systemd health, unchanged state stores/config, adapters, and channel smoke/readback.
- Real PF/NF worker-focus cycle showing elapsed activity, live bounded output, rotation to the next worker, and cleanup at zero workers.
- User acceptance in normal use.

## Current readiness

`validated-local` for the immutable `c7015aad` candidate. No production release claim is made in this revision.
