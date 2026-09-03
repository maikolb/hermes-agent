# P0-4a local candidate validation

Date: 2026-09-03 America/Sao_Paulo

## Result

The local candidate preserves `synchronous=FULL`, adds role-specific connection waits, raises WAL auto-checkpoint to 4000 pages, makes SQLite backup publication atomic and bounded, adds dependency-closed archive planning with source deletion mechanically absent, reuses the existing compact-storage VACUUM, and adds one explicit profile checkpoint script. Production was not contacted or changed.

Physical archive deletion remains disabled. `archive-copy` creates a consistent full database snapshot and an eligibility manifest. Existing list, resume, search and context paths therefore continue to read the unchanged active database. No transparent multi-database retrieval claim is made.

## Candidate lineage

- Base: `b89c5ca8af68e36a40af163c34da3af4532fc480`, fresh `maikolb/main` at branch creation.
- Accepted production-to-main commits included:
  - `58cc7b35dd`, CI fork runners.
  - `ee52044ab9`, merge of the CI fork runner repair.
  - `b89c5ca8af`, deferred-compaction oversized-turn safety stop.
- The two workflow YAML files parse successfully.
- `tests/run_agent/test_compression_lock_defer.py`: 10 passed.

## Objective gates

- P0-4a focused tests: 15 passed.
- Existing SQLite pragma tests: 6 passed.
- Existing lock patience tests: 6 passed.
- Existing compact-storage migration tests: 13 passed.
- Existing WAL checkpoint tests: 8 passed.
- Existing `_safe_copy_db` tests: 4 passed.
- Changed Python files: Ruff passed.
- Changed Python files: `py_compile` passed.
- `git diff --check`: passed.
- AOF v3 contract preflight: passed before implementation.
- AOF runtime Definition and scope alignment: passed before implementation.

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
- A noncompact fixture records exactly one call to the existing VACUUM owner.
- A repeated already-compact fixture records zero VACUUM calls.
- The checkpoint requires an absolute profile home whose basename exactly matches the named profile.
- It uses one `PRAGMA wal_checkpoint(PASSIVE)`, preserves FULL and enforces a bounded busy timeout.
- No scheduler, cron entry, service or production process was created or changed.

## Runtime provenance

The pinned Linux x86_64 CPython artifact and publisher API both report SHA-256 `0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461`. Its immutable build manifest pins SQLite 3.53.1.0 source hash `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`. The Windows host has no WSL runtime, so the Linux interpreter was not executed. Runtime execution remains a later staging and target gate.

## Limitations

- No active database row is deleted, so this candidate alone does not reclaim session-row space.
- The archive copy is a full consistent snapshot, not a transparent second live store.
- No production or Telegram interface validation occurred.
- No Linux systemd profile drop-in was built or validated in this local Windows change.
- The checkpoint surface is not scheduled.
