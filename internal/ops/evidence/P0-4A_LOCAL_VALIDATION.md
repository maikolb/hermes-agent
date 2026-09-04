# P0-4a local correction validation

Date: 2026-09-03 America/Sao_Paulo

## Result

Claude's implementation review invalidated the original `validated-local` claim. Findings F1 through F8 are corrected and pushed. The exact local suites are green, but required remote CI is not: Python failed and Nix remains queued. Readiness stays `implemented`. Production was not contacted or changed.

Archive deletion remains mechanically unavailable. `archive-copy` creates a complete SQLite copy plus an eligibility manifest, while all operational reads continue to use the unchanged active database. It reclaims zero session-row bytes.

## Correction evidence

- Real compact path: a v22 legacy SQLite fixture completed the actual FTS conversion and VACUUM with `ok=True` and `vacuumed=True`; search remained intact. A missing `vacuumed=True` confirmation now fails closed. An already-compact database reports `vacuumed=False` and does not request compaction.
- Cyclic lineage: two retained sessions with mutual parent IDs terminate safely; an independent old ended session remains the only eligible row.
- AOF runtime: the seven repository-local runtime files were removed. The card-specific RUN, events and evidence remain.
- Neutral defaults: absent role settings leave connection synchronous, busy timeout and WAL autocheckpoint values unchanged. Explicit NFOS settings read back FULL, 1000/30000/30000 ms and 4000 pages. A configured durability downgrade is refused and FULL is applied.
- Restore authority: `restore-check` runs `PRAGMA integrity_check` and `foreign_key_check` on the supplied isolated copy. The misleading `verify-archive` command was removed.
- Checkpoint: the only mode is PASSIVE. SQLite `busy_timeout` is the enforceable lock-wait bound; no false wall-clock deadline claim remains. No scheduler was installed.
- Packet v3 uses only candidate commands, keeps archive deletion disabled, requires restored-snapshot rehearsal, preserves profile-only pinning and keeps all decisive database checks before start.

## Test gates

- Claude's exact focused suite plus new repros: `25 passed in 22.80s`.
- Adjacent FTS migration, lock patience, WAL checkpoint and accepted-main compaction safety: `37 passed in 26.11s`.
- Ruff 0.15.10 over 11 changed Python files: passed.
- `py_compile` over the same 11 files: passed.
- `git diff --check` from accepted base: passed.
- Canonical external AOF contract Preflight, Final, RUN Definition and scope alignment: passed after the final evidence refresh.
- GitHub `Python tests / Run tests`: failed with 78 tests across 47 files on run `33822614809`.
- GitHub `nix flake check`: still queued on run `33822614270` after the bounded wait.

One diagnostic command initially named `tests/state/test_wal_checkpoint_strategy.py`; that path does not exist, so no tests ran in that attempt. The corrected existing path `tests/test_wal_checkpoint_strategy.py` is included in the 37-pass adjacent result.

## Canonical AOF validator authority

- Authority: `C:/Users/maiko/Projetos/AgentOperatingFramework` at Git commit `3fdf81f9588683145c20ddd6a4f48ea68581993e`.
- The `framework/runtime` subtree is clean at that commit. Unrelated files elsewhere in the authority repository are dirty and are not used by these validators.
- `validate_execution_contract.ps1`: SHA-256 `6aba7a7c9101dbc7a098d8aef4eb4c02f0bc15c28d80631c10b10c27aa2d3cf8`.
- `validate_agent_loop_run.ps1`: SHA-256 `2b823afa1eec5614768ccf50f978bee371b25a1d9a19487c185d5c1e801ce66a`.
- `emit_scope_manifest.py`: SHA-256 `0a754ee9badfadeb93fae6e2d3246551603c7c90f36158584c305b45c7c1424a`.
- `validate_scope_alignment.ps1`: SHA-256 `7ae2c226f0a9aace663db20319a87387c52ab6127882102cbcf8b5144d8c76c4`.
- Run schema: SHA-256 `bc0ab228351676dcf6800524d4abc89556d37c993d0ac4c54b1bc2101954c479`.
- Event schema: SHA-256 `1c54c91d6beb4ec66fe2ccc9a969c9b359dd24176033e688b0c382f12da53ebe`.

The contract validator runs from the canonical external script against the product's canonical contract. The card RUN validator runs with the AOF authority repository as its workspace root so its schema references resolve to the canonical runtime package. Scope alignment runs against the product checkout.

## Runtime provenance and limitations

The pinned Linux x86_64 CPython artifact hash is `0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461`; publisher metadata and the local download matched. Its build manifest pins SQLite 3.53.1.0 source hash `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`.

The Linux interpreter has not run on staging or the target. No restored production snapshot rehearsal has proved the below-1-GB gate. No systemd profile pin or Telegram canary has been validated. Production readiness is blocked regardless of local and CI results.

The Python failure is not green and blocks `validated-local`. For comparison, the accepted-base PR #78 run `33651194711` failed 76 tests across 46 files, and main run `33258501033` failed 71 across 45. Failed file membership changed between runs, consistent with the existing concurrent CI instability, but this packet does not waive the required Python gate. macOS also repeated the previously documented voice test failure. Nix has no result and is reported pending, not passed.
