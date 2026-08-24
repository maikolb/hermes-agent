# Hermes main consolidation — evidence ledger

This ledger separates observed facts, resolution decisions and pending validation. It is not a release claim.

## Authorities and boundaries

- Candidate worktree: `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824`.
- Read-only baseline: the installed Hermes runtime under `C:\Users\maiko\AppData\Local\hermes`.
- Write remote: `maikolb`; `origin` is upstream read-only.
- External projects remain external: Intake Hub, Titan, DarkFactory and the canonical AOF implementation are not source donors for this merge.
- No installed runtime, Telegram/WhatsApp/Jira/Kanban data, cron or deployment is mutated by this contract.

## Established Git facts

- Local clone reflog begins at 2026-06-16 14:09:08 -03:00 from `https://github.com/NousResearch/hermes-agent.git`.
- GitHub fork `maikolb/hermes-agent` was created at 2026-08-10T22:05:22Z (19:05:22 -03:00).
- At audit time the personal fork main was 3,251 upstream commits behind current `origin/main`.
- The local custom integration range contains 42 commits. Their behavior-by-behavior classification is pending completion below.
- Git/GitHub expose no durable justification for the fork creation. The repository owner supplied the missing historical account on 2026-08-24: Hermes created the fork without a direct request in order to support the Telegram Project OS and the Windows zero-visible-UI fixes. This is recorded as user testimony, distinct from Git-derived evidence; the commit chronology is consistent with it.

## Preservation priority supplied by the owner

Priority changes gate depth, not whether valid work is preserved.

1. **Primordial — Windows/runtime zero UI:** Git, CMD, terminal and other child processes must not surface visible windows.
2. **Primordial — Telegram Project OS:** teams → topics/projects → Kanban → tasks, contextual mention handling, default steer/workers, focus/activity display, auto-provision/bind/private-repo workflow and completion closure.
3. **Primordial — delivery and recovery:** context compaction, gateway interruption and power/process loss must preserve the complete active task and delivery checkpoint, resume without losing the request, and avoid duplicate or stale final delivery.
4. **Important — Honcho/memory.**
5. **Important — AOF/guardrails.**
6. **Important — Intake Hub:** preserve the generic core transport/bridge contract while the hub implementation remains in its separate repository.
7. **Important — documentation and regressions.**

All other valid behavior found in commits/runtime/Kanbans remains in scope at important priority unless evidence proves it obsolete, foreign-owned or regressive.

## Conflict resolution ledger

`Decision` describes the current working-tree resolution. `Gate` remains pending until the named tests and semantic replay are green.

| File | Decision tied to behavior | Gate |
|---|---|---|
| `agent/conversation_compression.py` | Preserve the local active-user anchor so compression cannot lose the current request; use upstream `append_message` for synthetic fallback metadata when no anchor exists. | Compression provenance, 413 and turn-checkpoint tests. |
| `agent/conversation_loop.py` | Preserve independent upstream retry/billing fields and local billing-block propagation; no field wins by side selection. | Error-surface and billing carry-through tests. |
| `agent/tool_guardrails.py` | Preserve upstream stall detection/state plus local structural-failure redirect state. Redirect must not masquerade as a hard halt. | Stall, redirect and real-halt runtime tests. |
| `gateway/config.py` | Preserve upstream multiplex allowlist behavior and local project-router configuration. | Config parsing, ACL and project-router tests. |
| `gateway/kanban_watchers.py` | Preserve notification, focus and claim event classes; completed/archive/unblock may focus, while crash/timeout do not become ordinary user success notifications. Combine garbage collection with configured agent wake/focus. | Kanban notifier, wake and focus replay. |
| `gateway/run.py` | Preserve upstream pending-message/restart delivery semantics and local explicit checkpoints; reconnect gets both initial/recovery context; pending obligations keep claim/redelivery metadata; busy steer remains media-aware; project routing occurs before session creation; activity uses one editable indicator message with elapsed time and cleanup. | Restart/drain/checkpoint, steer, project routing, activity/focus and delivery replay. |
| `gateway/session_context.py` | Preserve browser/session context variables and project/workspace binding variables together. | Session-context and project isolation tests. |
| `hermes_cli/kanban_db.py` | Persist/promote/release workspace leases before firing worker lifecycle hooks, keeping both concurrency safety and lifecycle notification. | Workspace lease and worker lifecycle tests. |
| `plugins/memory/honcho/client.py` | Keep current upstream client/cache/concurrency architecture and port only project workspace scoping through an effective workspace identity. Avoid resurrecting the superseded client-signature implementation. | Honcho client, concurrency and project isolation tests. |
| `plugins/memory/honcho/session.py` | Preserve upstream session behavior and local project workspace binding. | Honcho session and project isolation tests. |
| `plugins/platforms/telegram/adapter.py` | Preserve bounded upstream DNS-over-HTTPS/seed fallback and local explicit direct transport; avoid proxy discovery when direct mode is selected; keep a single handler registration site augmented with forum-topic lifecycle handlers; preserve polling progress/deferred recovery. | Telegram transport, close-wait, reconnect, topic lifecycle, send path and zero-visible-UI tests. |
| `run_agent.py` | Preserve upstream stall observation/result stubs and allow local redirect as a distinct action; real halts remain real halts. | Guardrail runtime and streaming halt tests. |
| `tests/gateway/test_restart_resume_pending.py` | Retain both restart/pending scenarios rather than choosing one branch's coverage. | Test file itself plus restart semantic replay. |
| `tests/gateway/test_telegram_closewait_limits_31599.py` | Use centralized gateway mocks; explicitly parameterize direct transport and cover it. This also fixes a pre-merge local defect where `direct` was referenced without being declared. | Direct and fallback transport tests. |
| `tests/gateway/test_telegram_send_path_health.py` | Use centralized mocks while retaining flood-control and raw-response coverage; remove unused branch-local imports only. | Send-path health suite. |
| `tests/tools/test_code_execution.py` | Retain coverage for interrupted-output formatting and local sandbox-source preparation. | Code execution suite. |
| `tools/code_execution_tool.py` | Preserve upstream text forwarding and Windows hidden-process kwargs without duplicate `creationflags`. | Code execution and Windows zero-UI tests. |
| `tools/mcp_oauth.py` | Instantiate the dynamically selected provider class and replace the SDK lock with an asyncio-compatible lock. | MCP OAuth and metadata/restart tests. |
| `tools/process_registry.py` | Preserve control notes, subagent attribution and bounded output trimming together. | Process registry and delegate-control tests. |

## Current static evidence

- Exact conflict-start/end marker scan: no unresolved `<<<<<<<` or `>>>>>>>` markers remain in the working tree.
- `git diff --check`: green before staging.
- Python syntax parse: 4,633 tracked Python files parsed with zero errors on the resolved working tree.
- No conflict is accepted or committed yet; the index deliberately remains unmerged until the behavioral audit is complete.
- The first focused run reported 412 passed, 25 failed and 60 skipped while using the installed runtime venv. That environment was stale for MCP 2.0 and is not release evidence. A clean candidate venv created from `uv.lock` made all 58 MCP OAuth tests green and reduced the remaining failures to Honcho tests (11), Windows process/code-execution tests (7) and guardrail runtime tests (5). This is a diagnostic result, not a green gate.
- Restart/pending coverage contributed 50 passing tests, but the primordial crash/compaction guarantee remains pending semantic failure-injection replay.
- After correcting `REG-2026-08-24-009`, the full deterministic
  compaction/checkpoint/restart/delivery matrix is 350 passed, zero failed and
  one declared Linux-only skip across 26 files in the clean candidate venv. It
  covers compression provenance, turn checkpoints, in-place compaction,
  pending restart, drain, checkpoint-to-delivery linkage, delivery ledger,
  profile/turn isolation, active-turn recovery and goal resume. Separate fresh-
  process failure injection remains an additional gate rather than being
  inferred from this matrix.
- Guardrail integration exposed one real nondeterminism: an identical failed call recorded a redirect for the entire tool, so a different call in the same concurrent batch could run or be rejected depending on thread order. Evidence tied the behavior to local commit `8b76dee159`. The candidate now records exact-call/no-progress redirects by canonical signature, leaving tool-wide escalation to the separate same-tool threshold. The focused guardrail suite is 23/23 green and the concurrent regression passed 20 consecutive runs.
- The focused Project OS/router/Kanban/steer/activity group is 297/297 green.
  Nine stale mocks and one invalid fixture were updated to the merged APIs and
  explicit wake opt-in. The run also exposed and fixed a real silent archive
  cleanup defect, recorded as `REG-2026-08-24-003`.

## Primordial recovery failure-injection findings

These are observed pre-correction facts from fresh-process/forced-exit audit,
not release claims:

- Process death after checkpoint preparation but before transcript swap
  restored the original transcript; death after real archive/compaction but
  before acknowledgement restored the compacted transcript. These positive
  controls show that the durable checkpoint can survive those compaction
  boundaries.
- A production tool effect followed by `os._exit(88)` left the effect applied
  while the checkpoint remained in `planning`, with no pending/unknown tool
  call. The attempt/result APIs were not wired to the production effect
  boundary. The isolated unknown replay guard also allowed the same unresolved
  call after its first process-local block. This is
  `REG-2026-08-24-005`.
- Re-recording a delivered stable obligation through the pre-correction
  `INSERT OR REPLACE` reset terminal proof to a retryable row. Delivery status
  updates lacked a turn/revision/content fence, so a late obligation could
  terminalize a newer checkpoint. Remote acceptance before local ACK was also
  eligible for automatic resend instead of remaining explicitly ambiguous.
  This is `REG-2026-08-24-006`.
- An ordinary text final persisted its user and assistant rows, but its turn
  checkpoint remained in `planning` without a pending exact deliverable.
  Restart therefore had no contract to skip the model and replay the exact
  post-transform final payload. This is `REG-2026-08-24-007`.

The corrected exact-final rail now uses a replay-safe durable `claimed` state:
ownership and checkpoint/capability checks occur before the row crosses into
`attempting`, and the attempt counter advances immediately before the adapter
boundary. Proven pre-network rejection or cancellation returns the row to
`deferred`; death after `claimed` is recoverable; an outcome after the local
attempt boundary remains conservatively ambiguous. Telegram exact delivery
bypasses rich/parse/topic fallbacks, and the Baileys probe accounts for UTF-16,
reply prefix and bridge limits. No exactly-once claim is made because the local
CAS and remote platform operation cannot be atomic without platform
idempotency.

Validation for this rail is `validated-local` as a subsystem, not a release
claim: the final canonical matrix reports 224 passed, one declared Linux-only
skip on Windows and zero warnings; crash-after-claim passed 20/20 separate
pytest process invocations; relevant Ruff, production `py_compile` and
`git diff --check` are green. The isolated repetitions also exposed and fixed
an import-order defect in the Windows zero-UI broker, recorded as
`REG-2026-08-24-008`. Whole-candidate, post-upstream and target canary gates
remain pending, so overall readiness stays `implemented-in-progress`.

## Commit inventory

Primary classification and chronology/succession review are complete 42/42;
final release acceptance remains gated by behavior-level validation.

The complete per-commit chronology, surfaces, succession inference and risk is
recorded in `HERMES_LOCAL_COMMIT_INVENTORY_20260824.md`.

| Primary family | Count | Commits |
|---|---:|---|
| Windows/runtime | 12 | `eb27bd0194`, `5d84d7006c`, `6f09a15c70`, `22df0f6593`, `fb3feed941`, `5ff84559eb`, `db0b476003`, `461bf11ae5`, `7d2eb9a6a4`, `5c6101a438`, `6e6dbfa763`, `04dc75530a` |
| Telegram/Project OS/Kanban | 16 | `905fbc543e`, `94f9a627a2`, `cc3a24dbf9`, `4aaa048d46`, `6e72b689a7`, `99357b3884`, `52264aac2d`, `8e724b6838`, `50a48de7a6`, `6f60b26c44`, `68d2a3b8b8`, `492fe4e01c`, `bb1f85ba3f`, `ae32722846`, `22ef28d318`, `b7398c31d1` |
| Steer/workers/focus/activity | 2 | `e18609704b`, `8801464b95` |
| Git/PR/deploy documentation | 2 | `4a1d478711`, `66c2257b9f` |
| AOF/guardrails | 4 | `7d245700be`, `a9c727bfb6`, `7ba6e8ebc8`, `a3260da9ad` |
| Intake/Jira | 3 | `3ddff51ffb`, `fe7aac0109`, `3b842b01ca` |
| Workspace protocol / spec | 1 | `f86cbf45f5` |
| Honcho/memory/workflow | 1 | `8b76dee159` |
| Generic MCP OAuth | 1 | `df294faa62` |

## Stash inventory

The four repository stashes were audited read-only in addition to branches and
worktrees. None is an unapplied release donor:

- `1e1e889357` (`quarantine-titan-late-self-mutation-20260820-1640`) contains
  three small follow-up surfaces. Native-Windows file-tool path handling is
  already present in the candidate through the newer upstream implementation
  and broader tests; its two guardrail initializers are superseded by the
  exact-signature redirect state and concurrency regression retained here.
- `0a410e34d8` and `3267956a00` are overlapping snapshots of the same live
  incomplete mutation; the former adds only two guardrail initializer lines.
  Their checkpoint/project context, storyboard fallback and Windows subprocess
  behavior are already represented by later committed implementations and the
  current conflict resolutions. Their blanket rejection of manual agent cron
  runs predates upstream's background-dispatch path and would disable a now
  bounded capability. The orphaned test-only expectation was reproduced as one
  failure, reconciled to the current implementation, and is now 13/13 green.
- `dbd2765e8d` (`hermes-install-autostash-20260723-165402`) changes 4,977 files
  only because of CRLF normalization. With CR-at-EOL ignored, only two product
  hunks remain: hidden Windows LSP launch and hidden WhatsApp bridge launch.
  Both are present in newer forms: the LSP client uses the canonical hidden
  flag and the current plugin-owned WhatsApp adapter uses the process broker.

The stashes remain recoverable until the published candidate and rollback refs
are proven reachable. They will not be applied, dropped or rewritten as part of
the merge.

Cross-cutting review is mandatory for `8b76dee159` and `8801464b95`; the two Git/PR/deploy commits are documentation/contracts only, not implementation of the closure workflow. Literal Read.ai integration and an in-core Jira writer are absent from this 42-commit range, so neither may be claimed from commit labels alone.

The 42 commits consist of 22 cherry-picks, 18 direct commits, one first-parent merge and one side commit carried by that merge. Six large commits account for approximately 82.5% of added lines, which is why commit count alone understates integration risk.

After a read-only fetch during this audit, `origin/main` advanced from candidate base `c584d15cdc` to `ec44116d59` (19 additional upstream commits). The current resolution remains preserved in the working tree, but final publication must reconcile this newer tip and rerun the affected gates.

## Runtime and Kanban baseline

Sanitized read-only snapshot at 2026-08-24 01:39:06 -03:00:

- Four gateway processes share the installed checkout: default/Titan, CEO Game, Exocortex and Project Factory. All execute `hermes_cli/main.py gateway run` from the clean branch `integrate/local-runtime-v2-20260820` at `66c2257b9f`; no process was stopped, reloaded or redirected during this audit.
- The governed 15-file runtime fingerprint is `f6f5c93e1b97905da65b94a2d2ecabaed2668c3369cbd3857846fd38e01c4916`. Because the default process started before the final commit while the same bytes already existed, the safe identity is the content fingerprint rather than an unqualified claim that every process loaded commit `66c2257`.
- The default profile configuration changed after its current process started and is therefore pending reload. The other three profiles loaded after their current artifacts. No synthetic Telegram or WhatsApp target message was sent by this audit.
- `main` is not a usable runtime baseline: local `main`, fork `main`, current upstream `main` and the installed runtime are four distinct refs. Thirteen of the 15 governed files differ between local `main` and the installed runtime.
- The consolidation still contains 19 manually combined unmerged files. Every working-tree resolution differs from stage 2, stage 3 and the merge base; each therefore requires code review and tests rather than a mechanical `git add`.

External ownership boundaries are confirmed:

- No tracked source from AOF, Intake Hub, DarkFactory or Titan is embedded in core. Titan is the default profile identity; DarkFactory remains a separate repository; Project OS is legitimate core behavior for this release candidate.
- Intake Hub remains a separate clean repository and its five installed package files match its source byte-for-byte. Core owns the raw WhatsApp transport; Project Factory owns the scheduled consumer. The rebuild risk is that the external package is installed in the live venv but not declared by core, so recreating that venv can silently remove it.
- The default profile has zero intake jobs. Project Factory has one enabled intake job with a successful read-only history at snapshot time, confirming the current split rather than an in-core Jira implementation.
- Project Factory is improperly configured against a mutable AOF source checkout. The live process loaded earlier AOF/runtime-registry revisions while the source and registry changed during this audit. This is a deployment/configuration boundary defect to correct in a separate, approved runtime change; it is not permission to import AOF source into this merge.
- Honcho is configured as the Hermes memory provider, but no listener was present on its configured loopback endpoint during the snapshot. Read.ai is configured as a separate on-demand bridge, with no resident process. AIRC, Claude TL, Codex Loop and Grok were not observed as an active descendant workflow of any gateway; their presence in docs/config must not be reported as live execution evidence.

Kanban inventory (read-only):

- 29 physical databases, 26 active boards and 87 rows / 79 unique tasks were observed: 59 done, 4 ready, 11 blocked and 5 todo.
- Sixteen anomalous boards use `new_chat_*` or sentence fragments; fifteen are empty. Evidence points to unknown Telegram topics being auto-registered with their topic name as a permanent project slug, with no TTL cleanup. This explains board clutter but is not yet a licensed cleanup action.
- Configuration/code support exists for steer, focus/activity, table rendering, topic binding and lifecycle events. Database rows and static code alone do not prove the full user-visible Telegram replay; that remains a semantic gate.

Honcho failure triage in the clean candidate environment:

- Zero product regressions were proven in the resolved Honcho code. Thirteen isolation/concurrency behaviors remain green, including project/profile separation and captured configuration across threads/session managers.
- Two local tests encode the superseded pre-upstream cache design; one adapted assertion hashes the credential without the production domain separator; eight upstream tests clear all Windows home variables and fail before exercising Honcho. These are test defects to repair without reverting the current upstream cache architecture.
- `test_client_identity_isolation.py` is skipped because the Honcho SDK is absent from the clean candidate environment. This is a declared validation gap, not a pass.
- Conflict review exposed `REG-2026-08-24-011`: a file-backed
  `honcho.base_url` was resolved only after cache lookup, so a long-lived
  gateway could reuse the client for the previous backend. Effective transport
  settings are now resolved once before lookup and shared by cache and factory;
  the exact endpoint-change reproduction is 1/1 and the focused Honcho matrix
  is 59 passed with 16 declared SDK-conditional skips.
- Windows process/code-execution triage found one real product gap: pywinpty
  reported an EOF operation as successful without closing ConPTY input. The
  candidate now fails closed on that unsupported operation; adjacent tests no
  longer launch an unmocked PTY or `taskkill` on the wrong OS branch. The
  focused result is 90 passed, 48 skipped and 5 subtests passed. Regression
  record: `REG-2026-08-24-002`.
- The final Windows gate then exposed `REG-2026-08-24-010`: the preserved
  rebased commit carried terminal-boundary tests without the production hunk
  that installs the broker before `LocalEnvironment` is imported. The exact
  reproduction is now 3/3 green; the broader broker, hidden-subprocess,
  terminal, gateway and static-footgun matrix is 38/38 green, followed by a
  clean-process import-order reproduction at 1/1. External desktop observation
  with `VisibleWindows=0` remains a top-level closeout gate.

The owner requested that Project OS later be evaluated as an independent framework/plugin. That proposal is recorded as a durable `proposed` decision and is explicitly outside this consolidation; no extraction/refactor is authorized now.

The accumulated-worktree failure is registered as `REG-2026-08-24-001`.
Root cause is not “insufficient cleanup”: Project OS marks a task done before
Git delivery, then runs cleanup best-effort with no retry owner. The correction
is a fail-closed, project-configured delivery state machine; implementation is
still pending and must not be represented as complete by this ledger.

## Fork PR #1 quarantine audit

The open `maikolb/hermes-agent` PR #1 was audited read-only rather than merged
by age or label. Its base is `49c632310d`, head is `46f02292a4`, and its 13
commits change 369 paths (`+54,285/-168`), including 341 Android paths. All 13
stable patch IDs are unique against both the installed-runtime lineage
`66c2257b9f` and the upstream-release lineage `c584d15cdc`; uniqueness does not
make the stale implementation compatible. Fourteen of its 17 modified
non-Android paths have since evolved independently on the release lineage,
`git diff --check` reports 11 diagnostics, and the PR has no review or CI
evidence for its final head.

Behavioral review found potentially valid historical requirements (durable
identity, idempotency, session linkage and multi-client fanout), but the PR
implements them against the rejected pre-`hermes.workspace` Project Ops
contract. Its portal bypasses current Project Router/workspace authorities.
The Android derivative is a coherent external client, but it imports 329 files
from `luinbytes/hermes-android`, pins the obsolete backend contract, and its
nested workflow cannot run as a monorepo GitHub workflow. The last five Android
fixes have no build receipt for the PR head.

Release decision: forward-port no hunk into core during this consolidation.
Keep the PR branch and all 13 commits as recoverable quarantine evidence; do
not rebase, squash-merge, cherry-pick, delete or silently close it in this
change-set. Any still-required server invariant must be rederived in a small PR
against current authorities. Portal and Android belong in separate
plugin/client repositories, consistent with the owner's future Project OS
extraction proposal.

## Behavioral traceability matrix

This matrix separates behavior present in the candidate, profile-owned policy,
external integrations and expectations that were never implemented. A local
test cannot promote any row to target acceptance; the installed runtime remains
unchanged and a later canary is still mandatory.

| User requirement | Authority and retained implementation | Deterministic evidence | Current verdict and remaining gate |
|---|---|---|---|
| Windows zero-visible-UI | Local Windows/runtime commit family; `hermes_cli/windows_process_broker.py`, process runner and zero-UI call sites | Focused broker/process/code-execution suites; exact-delivery repetitions exposed and fixed `REG-2026-08-24-008` | Subsystem `validated-local`; whole-candidate scan and final `VisibleWindows=0` verifier remain pending. No live runtime launch is authorized. |
| Telegram team → Topic/project → board/workspace binding | Project OS commit family; `gateway/project_router.py`, `gateway/run.py`, `tools/project_tools.py` | Router/config/provisioning/topic/project suites were 297/297 in the pre-gate candidate snapshot | Implementation retained. Post-upstream deterministic replay and later real Telegram canary remain pending. |
| A parallel ordinary request steers without aborting the primary task | Gateway steer rail plus local media/voice correction `e18609704b`; Project Factory live profile selects `display.busy_input_mode: steer` | Busy-session and multiplex-profile tests prove steer/no-interrupt/no-queue, including reply image and voice | Core transport behavior is implemented. The generic default is intentionally `interrupt`; the Project Factory profile override remains external and was not mutated. |
| An independent steered request becomes a separate card/worker | Project Factory `SOUL.md` owns the behavioral rule; Kanban dispatcher owns execution after a card exists | Steer and dispatcher/worker suites cover the two halves separately | Not structurally enforced end-to-end: classification/materialization depends on model adherence to the profile rule. A target semantic replay must prove primary-active → steer → second card/run without interrupt. |
| Show primary work, elapsed `Trabalhando ha...`, then advance through parallel workers | `gateway/run.py` activity indicator plus `gateway/kanban_watchers.py` worker-focus counter from `8801464b95` | Activity send/edit/topic isolation and worker-focus advance/rehydration tests | Elapsed indicator and worker alternation are implemented. Worker reasoning is not part of this surface; Project Factory has model reasoning display disabled. A later surface replay must confirm the user-visible sequence. |
| “Show the Kanban” returns a current compact table | Project routing prompt requires `kanban_list` and a GFM table; rich Telegram renderer carries an already-produced table | Prompt-contract, Kanban list and rich-table renderer tests | The components are implemented, but there is no deterministic natural-language E2E proving tool call → exact current rows → GFM response. Target semantic replay remains required. |
| Topic creation provisions folder, board, bind and private remote repo | Topic provisioning creates the binding, local workspace and board. `requires_repo=true` later initializes/registers a local Git repository | Router provisioning and `test_project_board_routing.py` prove local behavior and explicitly prove no remote is created | Folder/board/bind/local-repo behavior is retained. Private remote creation was never implemented or configured in the audited commits/runtime; it is a user requirement, not a regression claim. Adding host/owner/visibility automation is outside this consolidation and requires a separate high-risk contract. |
| Every code worktree closes through push → PR → green checks → merge → remote-main reachability before cleanup | New generic board policy, sealed PR/artifact request, remote verifier, completion fence and durable cleanup retry in `hermes_cli/git_delivery.py` / `hermes_cli/kanban_db.py` | Focused verifier, completion, tool, review and teardown suites; adversarial review is still searching direct writers/bypasses | `implemented-in-progress`. A swarm direct-`done` bypass was found after the first 41-test run and is being corrected; no green claim is allowed until the writer audit and affected reruns close. |
| Compaction/gateway/process loss preserves the exact task and delivery | Turn checkpoint, tool-effect fence, immutable delivery obligation and exact-final rail | Post-`REG-009` deterministic matrix: 350 passed/0 failed/1 declared Linux-only skip across 26 files; corrected exact-final matrix 224 passed/1 declared OS skip; crash-after-claim 20/20; tool-effect process boundary passed 20/20 and turn/compaction boundary passed 8/8 | Recovery subsystems are `validated-local` subject to final post-merge rerun. CAS-to-remote-send is not atomic; ambiguous remote acceptance remains fail-closed, never claimed exactly-once. |
| Honcho/memory continuity and full Honcho + Claude TL + Codex Loop + AIRC + Grok workflow | Hermes owns generic Honcho client/session integration and generic delegation/guardrails; external orchestration remains outside core | Honcho isolation/concurrency tests are partly green; SDK/service-dependent and target workflow evidence is absent | Core integration retained, but no claim that the full external workflow is active. Project Factory currently points at mutable external AOF state; correcting that runtime deployment is a separate contract. |
| AOF scope/guardrails and regression discipline | Generic tool guardrails plus canonical execution/revision documents; AOF product source remains external | Guardrail suite 23/23 and concurrency regression 20 consecutive passes in the pre-gate snapshot | Generic guardrails are locally proven; external AOF workflow, promotion protocol and live adherence need their own target evidence. |
| WhatsApp passive intake → Hermes PF → curated → Jira | Hermes core owns exact-JID passive raw transport and deny-egress; external Intake Hub and Project Factory consumer own curation/Jira | Core adapter 5/5 and bridge 7/7 green, plus 105/105 green file/register tests in the clean external `hermes-intake-hub` checkout | Boundary is preserved without absorbing the Hub into core. Natural group→Jira acceptance and rebuild/dependency proof remain target/external gates. |
| Read.ai meeting → spec/tasks | Read.ai is configured as an external on-demand bridge; no implementation exists in this 42-commit range | No candidate test can prove it | External gap, not a Hermes-core success claim. Locate and validate the bridge under a separate scoped run. |

Rollback refs for the pre-consolidation fork main, installed integration head
and the final candidate are still pending publication. No row above authorizes
branch/worktree deletion before exact remote reachability is recorded.

## Final upstream-first consolidation pass

The accumulated local runtime was sealed on the isolated release branch as
`8a9f136a9b37`. Official `origin/main` at `057dcdf236f8` was then merged in
full. Fourteen of the fifteen paths changed on both sides merged
automatically. The only textual conflict was
`tests/run_agent/test_identity_flush.py`; its two sides covered distinct
invariants, so both tests were retained instead of selecting one side.

One bounded post-merge floor matrix produced the following local evidence:

- recovery/checkpoint/process boundaries: 106 passed initially; the sole
  failure exposed a missing `name` -> `tool_name` persistence fallback, whose
  exact reproduction passed after the one-line correction (effective result:
  107/107);
- Project OS, Telegram routing, Kanban workers and workspace protocol: 234/234;
- Windows zero-UI, generic AOF guardrails and Honcho: 259 passed with 47
  declared environment/SDK-conditional skips;
- passive intake, Git ownership gates and meeting-action parsing: 18/18;
- the native WhatsApp bridge Node test command completed with exit code zero;
- `run_agent.py` compiles and the final diff check is clean.

Total Python result for this bounded final pass: 618 passed, 47 declared
skips, zero remaining failures. This promotes the consolidated candidate to
`validated-local`; PR/CI, remote reachability and any later target canary remain
separate readiness gates. The installed runtime was not modified or restarted.

## Publication state

- Current readiness: `validated-local`.
- Candidate branch is not pushed by this ledger update.
- Personal-fork `main` is not changed.
- Installed runtime is not changed.
