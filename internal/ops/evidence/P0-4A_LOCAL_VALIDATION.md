# P0-4a local candidate validation

Date: 2026-09-03 America/Sao_Paulo

## Result

Claude's implementation review invalidated the earlier `validated-local` claim. The original compact-storage test exercised a fake call graph and did not represent the real `SessionDB.optimize_fts_storage` path. The candidate remains `implemented` while the correction gates are running. Production was not contacted or changed.

Physical archive deletion remains disabled. `archive-copy` creates a consistent full database snapshot and an eligibility manifest. Existing list, resume, search and context paths therefore continue to read the unchanged active database. No transparent multi-database retrieval claim is made.

## Candidate lineage

- Base: `b89c5ca8af68e36a40af163c34da3af4532fc480`, fresh `maikolb/main` at branch creation.
- Accepted production-to-main commits included:
  - `58cc7b35dd`, CI fork runners.
  - `ee52044ab9`, merge of the CI fork runner repair.
  - `b89c5ca8af`, deferred-compaction oversized-turn safety stop.
- The two workflow YAML files parse successfully.
- `tests/run_agent/test_compression_lock_defer.py`: 10 passed.

## Objective gates from the superseded validation run

- The prior P0-4a focused result is retained only as historical evidence and is not a current readiness gate.
- Existing SQLite pragma tests: 6 passed.
- Existing lock patience tests: 6 passed.
- Existing compact-storage migration tests: 13 passed.
- Existing WAL checkpoint tests: 8 passed.
- Existing `_safe_copy_db` tests: 4 passed.
- Changed Python files: Ruff passed.
- Changed Python files: `py_compile` passed.
- `git diff --check`: passed.
- AOF v3 contract preflight, Runtime Definition and scope alignment passed before the original implementation. They must be rerun with the canonical external runtime after correction.

The combined adjacent diagnostic suite completed with 305 passed, 7 skipped and 3 failures. The failures are existing Windows-only assertion mismatches outside the P0-4a paths: a profile wrapper test expects a POSIX filename instead of the emitted Windows `.cmd`, a Kanban path assertion checks `/` against a Windows path, and a POSIX `0600` mode assertion is not representable by the observed NTFS mode. The P0-4a backup class passed 4 of 4. No product or test change was made to hide those unrelated platform failures.

## Backup and archive evidence

- A continuous WAL writer ran while one `pages=-1` SQLite backup completed.
- The published backup passed `PRAGMA quick_check` and `PRAGMA foreign_key_check`.
- Failure removes only the hidden partial and preserves a previously published final backup.
- The archive copy preserves the source file's mode where the host supports mode bits.
- Before and after archive-copy, active database list, single-session export/resume payload and FTS search results were equal.
- Open, pinned, retained-lineage ancestor, delegate, shared-prompt, disk-transcript and indexed external Kanban references are protected.
- An external session-reference column without an index blocks eligibility instead of triggering an unbounded scan.
- A freshly supplied maximum broad-candidate count is mandatory.
- Source-row deletion has no CLI flag or callable maintenance function.

## VACUUM and checkpoint evidence

- The maintenance wrapper calls the existing `optimize_fts_storage(vacuum=True)` path.
- The earlier fake `db.vacuum()` counter was invalid and has been removed.
- Current correction tests must prove that a real legacy database returns `ok=True` and `vacuumed=True`, that `vacuumed=False` fails closed, and that an already-compact database does not request another VACUUM.
- The checkpoint requires an absolute profile home whose basename exactly matches the named profile.
- It uses one `PRAGMA wal_checkpoint(PASSIVE)`, preserves FULL and enforces a bounded busy timeout.
- No scheduler, cron entry, service or production process was created or changed.

## Runtime provenance

The pinned Linux x86_64 CPython artifact and publisher API both report SHA-256 `0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461`. Its immutable build manifest pins SQLite 3.53.1.0 source hash `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`. The Windows host has no WSL runtime, so the Linux interpreter was not executed. Runtime execution remains a later staging and target gate.

## Canonical AOF validator authority

- Authority repository: `C:/Users/maiko/Projetos/AgentOperatingFramework`, Git commit `3fdf81f9588683145c20ddd6a4f48ea68581993e`.
- The `framework/runtime` subtree is clean at that commit. Unrelated files elsewhere in the authority repository are dirty and are not used by these validators.
- `validate_execution_contract.ps1`: SHA-256 `6aba7a7c9101dbc7a098d8aef4eb4c02f0bc15c28d80631c10b10c27aa2d3cf8`.
- `validate_agent_loop_run.ps1`: SHA-256 `2b823afa1eec5614768ccf50f978bee371b25a1d9a19487c185d5c1e801ce66a`.
- `emit_scope_manifest.py`: SHA-256 `0a754ee9badfadeb93fae6e2d3246551603c7c90f36158584c305b45c7c1424a`.
- `validate_scope_alignment.ps1`: SHA-256 `7ae2c226f0a9aace663db20319a87387c52ab6127882102cbcf8b5144d8c76c4`.
- Agent-loop run schema: SHA-256 `bc0ab228351676dcf6800524d4abc89556d37c993d0ac4c54b1bc2101954c479`.
- Agent-loop event schema: SHA-256 `1c54c91d6beb4ec66fe2ccc9a969c9b359dd24176033e688b0c382f12da53ebe`.
- The seven repository-local runtime copies were removed. Validation executes the scripts above from the canonical external authority.

## Limitations

- Readiness is `implemented`, not `validated-local`, until all correction gates and required CI complete.
- No active database row is deleted, so this candidate alone does not reclaim session-row space.
- The archive copy is a full consistent snapshot, not a transparent second live store.
- No production or Telegram interface validation occurred.
- No Linux systemd profile drop-in was built or validated in this local Windows change.
- The checkpoint surface is not scheduled.
