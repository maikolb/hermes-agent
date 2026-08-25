# Hermes local commit inventory — 2026-08-24

This is a 42/42 inventory of the local integration range. Dates are author
dates in 2026 (`America/Sao_Paulo`). “Successor” is an evidence-backed overlap
or a review inference, not proof that the earlier commit may be discarded.

Priority: P0 primordial, P1 important, P2 support/evidence.

| # | SHA / date | Subject | Family / priority | Main surface and succession | Risk |
|---:|---|---|---|---|---|
| 1 | `eb27bd0194` · 08-12 15:49 | broker subprocesses on private desktop | Windows P0 | Windows broker/runner; refined by `8b76dee159` | high |
| 2 | `5d84d7006c` · 08-12 16:04 | persist native hidden gateway entrypoint | Windows P0 | gateway launcher; refined by `8b76dee159` | high |
| 3 | `905fbc543e` · 08-13 16:01 | Telegram Project OS routing | Project OS P0, mixed | router/run/session/Kanban; followed by `94f9`, `cc3`, `4aaa`, `993`, `22ef` | high |
| 4 | `94f9a627a2` · 08-13 19:52 | restore Telegram topic routing | Project OS P0 | router/run/adapter; followed by `cc3` and `4aaa` | high |
| 5 | `cc3a24dbf9` · 08-13 22:09 | isolate project topic context | Project OS P0 | gateway context; expanded by `4aaa`/`880` | medium |
| 6 | `4aaa048d46` · 08-13 23:38 | auto-provision topic workspaces | Project OS P0 | config/router/run; extended by `993`, `bb1`, `ae3`, `b739` | high |
| 7 | `6e72b689a7` · 08-14 10:06 | route detached notifications through active profile | delivery P0, mixed | gateway/run; followed by `6f60`, `db0`, `7d2` | medium |
| 8 | `99357b3884` · 08-18 18:43 | archive project boards with Telegram topics | Project OS P0 | router/run/DB/adapter; followed by leases/repo init | high |
| 9 | `52264aac2d` · 08-18 19:01 | record topic lifecycle target validation | docs P2 | lifecycle contract; corrected by `8e724b6838` | low |
| 10 | `8e724b6838` · 08-18 19:08 | correct lifecycle target evidence | docs P2 | lifecycle contract; later evidence supersedes claims | low |
| 11 | `50a48de7a6` · 08-18 22:33 | isolate Telegram status by topic | Telegram P0 | adapter/status; adapter later touched by `6f60`/`8b76` | medium |
| 12 | `6f60b26c44` · 08-19 00:12 | replay deferred topic deliveries | delivery P0, mixed | ledger/run/adapter; followed by checkpoint chain | high |
| 13 | `68d2a3b8b8` · 08-19 00:16 | record topic recovery evidence | docs P2 | recovery contract; followed by acceptance doc | low |
| 14 | `492fe4e01c` · 08-19 01:22 | record topic fix acceptance | docs P2 | recovery contract; current ledger is newer evidence | low |
| 15 | `bb1f85ba3f` · 08-19 02:33 | auto-bind unnamed topics | Project OS P0 | config/run; role semantics later in `22ef` | medium |
| 16 | `6f09a15c70` · 08-19 08:40 | defensive policy-block recovery | recovery/guardrail P0 | conversation loop; followed by `8b76`/`7d245` | medium |
| 17 | `22df0f6593` · 08-19 09:10 | preserve active user turn across compaction | recovery P0 | compression/provenance; followed by checkpoints | high |
| 18 | `fb3feed941` · 08-19 14:55 | preserve deliverable after verification | delivery P0 | verification stop; directly superseded by `5ff84559eb` | medium |
| 19 | `5ff84559eb` · 08-19 15:22 | preserve final deliverable across verification | delivery P0 | loop/verification; followed by checkpoints | high |
| 20 | `db0b476003` · 08-19 17:26 | persist turn state and owed deliveries | recovery P0 | checkpoint/compression/ledger/run; refined by `8b76`/`7d2` | high |
| 21 | `461bf11ae5` · 08-20 15:37 | block synchronous manual agent-cron runs | test-only P2 | cron test only; no local implementation in commit | medium |
| 22 | `8b76dee159` · 08-20 15:50 | preserve local runtime reliability work | Windows/Project OS/Honcho/AOF P0, mixed | 81 files; decomposed by focused successors; old Honcho cache design superseded by current resolution | high |
| 23 | `e18609704b` · 08-21 18:35 | steer busy replies with media context | workers P1 | gateway/run; overlaps but is not proven superseded by `880` | medium |
| 24 | `8801464b95` · 08-21 21:05 | local reliability and passive intake | focus/activity/intake/runtime P0, mixed | 26 files; intake/guardrail later decomposed | high |
| 25 | `7d245700be` · 08-21 22:50 | restore guardrail redirect lifecycle | AOF/guardrail P1 | tool guardrail/tests; integrated by merge container `a9c` | high |
| 26 | `3ddff51ffb` · 08-21 22:57 | persist passive intake media securely | intake P1 | bridge/raw boundary; finalized by `3b842` | high |
| 27 | `a9c727bfb6` · 08-21 22:58 | merge AOF guardrail runtime fix | merge-only P2 | carries `7d245`; no independent code | medium |
| 28 | `fe7aac0109` · 08-21 23:30 | record passive intake batch boundary | docs P2 | contract only; implementation finalized by `3b842` | low |
| 29 | `7ba6e8ebc8` · 08-21 23:39 | record AOF route-policy target validation | docs P2 | contract/revision protocol; later evidence updates it | low |
| 30 | `a3260da9ad` · 08-22 00:07 | refresh AOF target process evidence | docs P2 | no code; current ledger is newer authority | low |
| 31 | `4a1d478711` · 08-24 00:18 | authorize repository consolidation | docs P2, mixed | obsolete contract scope; replaced by current canonical contract | medium operational |
| 32 | `3b842b01ca` · 08-24 00:18 | finalize raw-only passive intake boundary | intake P1 | adapter/bridge/raw tests/regressions; final local intake step | high |
| 33 | `df294faa62` · 08-24 00:19 | serialize MCP OAuth refresh per loop | OAuth P1 | OAuth implementation/test; conflict resolution tested | high |
| 34 | `7d2eb9a6a4` · 08-24 00:19 | reconcile durable checkpoint delivery | recovery P0 | checkpoint/run/restart tests; focused successor of `db0` | high |
| 35 | `ae32722846` · 08-24 00:19 | enforce workspace ownership leases | Project OS P0 | Kanban DB/lease tests; complemented by repo init | high |
| 36 | `22ef28d318` · 08-24 00:19 | enforce explicit route roles | Project OS/ACL P0 | router/ACL/slash tests; focused successor | high |
| 37 | `5c6101a438` · 08-24 00:20 | normalize installed gateway identity | Windows P0 | status/runtime identity | medium |
| 38 | `6e6dbfa763` · 08-24 00:20 | cover passive API-server wake | test-only P2 | notifier test only; no implementation in commit | medium |
| 39 | `b7398c31d1` · 08-24 00:21 | initialize missing workspace repositories | Project OS P0 | Kanban tools/routing test; complements provisioning | medium |
| 40 | `04dc75530a` · 08-24 00:21 | record runtime regression protocols | docs P2 | revision protocol only | low |
| 41 | `f86cbf45f5` · 08-24 00:26 | expose durable workspace turn protocol | workspace/spec P1 | TUI workspace protocol/server/tests; not Read.ai | high |
| 42 | `66c2257b9f` · 08-24 00:27 | scope clean-main integration worktree | docs P2 | one contract line; superseded by current contract/ledger | low |

## Shape and exclusions

- 22 cherry-picks, 18 direct commits, one first-parent merge and one side
  commit carried by that merge.
- 13 commits contain no independent product implementation: 10 docs-only, two
  test-only and one merge container.
- Six large commits contribute approximately 82.5% of additions, so review is
  organized by behavior and conflict, not by commit count alone.
- The range contains no literal Read.ai implementation, no AIRC integration
  commit and no in-core Jira writer. Those capabilities cannot be claimed from
  labels or contracts.
