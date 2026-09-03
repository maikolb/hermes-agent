#!/usr/bin/env python3
"""Bounded, fail-closed NFOS state maintenance primitives.

This module deliberately has no source-row deletion surface.  Its physical
archive is a consistent copy plus an eligibility manifest, so every normal
Hermes read continues to use the active database unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.backup import _safe_copy_db
from hermes_state import SessionDB, apply_database_pragmas
from utils import _preserve_file_mode, _preserve_file_owner, _restore_file_mode, _restore_file_owner


def _absolute_existing_db(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("database path must be absolute")
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"database does not exist: {path}")
    return path


def _absolute_output(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    path = path.resolve()
    if not path.parent.is_dir():
        raise ValueError(f"output parent does not exist: {path.parent}")
    return path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        with partial.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def verify_database(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.0)
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchmany(1)
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        session_count = (
            int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            if "sessions" in table_names
            else None
        )
        message_count = (
            int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            if "messages" in table_names
            else None
        )
    finally:
        conn.close()
    if quick_check != "ok" or foreign_key_rows:
        raise RuntimeError("database verification failed")
    return {
        "quick_check": quick_check,
        "foreign_key_check": "ok",
        "session_count": session_count,
        "message_count": message_count,
        "sha256": _hash_file(path),
        "bytes": path.stat().st_size,
    }


def create_backup(source: Path, destination: Path, timeout_seconds: float) -> dict[str, Any]:
    if source == destination:
        raise ValueError("backup destination must differ from source")
    if not _safe_copy_db(source, destination, timeout_seconds=timeout_seconds):
        raise RuntimeError("bounded SQLite backup did not converge")
    return verify_database(destination)


def archive_plan(
    source: Path,
    *,
    older_than_days: float,
    sessions_dir: Path | None,
    external_databases: list[Path],
    max_broad_candidates: int,
) -> dict[str, Any]:
    db = SessionDB(source, read_only=True)
    try:
        plan = db.plan_physical_archive(
            older_than_days=older_than_days,
            sessions_dir=sessions_dir,
            external_database_paths=external_databases,
        )
    finally:
        db.close()
    if plan["broad_candidate_count"] > max_broad_candidates:
        plan["blockers"] = sorted(
            set(plan["blockers"] + ["broad-candidate-limit-exceeded"])
        )
        plan["eligible_count"] = 0
        plan["eligible_sessions"] = []
    return plan


def create_archive_copy(
    source: Path,
    destination: Path,
    *,
    timeout_seconds: float,
    plan: dict[str, Any],
) -> dict[str, Any]:
    if plan["blockers"]:
        raise RuntimeError("archive plan has blockers")
    # The first bounded implementation copies the complete consistent store.
    # The manifest records the stricter eligible set, but deletion is absent.
    # This keeps prompts, lineage, transcripts represented in SQLite, and all
    # existing read behavior exact while transparent multi-store reads remain
    # future work.
    verification = create_backup(source, destination, timeout_seconds)
    source_stat = source.stat()
    _restore_file_owner(destination, _preserve_file_owner(source))
    _restore_file_mode(destination, _preserve_file_mode(source))
    eligible_hashes = sorted(
        hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()
        for row in plan["eligible_sessions"]
    )
    protected_hashes = {
        hashlib.sha256(session_id.encode("utf-8")).hexdigest(): reasons
        for session_id, reasons in plan["protected"].items()
    }
    manifest_plan = {
        "deletion_enabled": False,
        "broad_candidate_count": plan["broad_candidate_count"],
        "eligible_count": plan["eligible_count"],
        "eligible_id_sha256": eligible_hashes,
        "protected_id_sha256": protected_hashes,
        "blockers": plan["blockers"],
    }
    manifest = {
        "schema_version": 1,
        "created_at_epoch": time.time(),
        "source": str(source),
        "archive": str(destination),
        "source_device": int(source_stat.st_dev),
        "deletion_enabled": False,
        "eligibility": manifest_plan,
        "verification": verification,
    }
    manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
    _atomic_json(manifest_path, manifest)
    return {"archive": str(destination), "manifest": str(manifest_path), **verification}


def pragma_readback(source: Path, role: str) -> dict[str, Any]:
    mode = "ro" if role == "reader" else "rw"
    conn = sqlite3.connect(f"file:{source}?mode={mode}", uri=True, timeout=0.0)
    try:
        apply_database_pragmas(conn, db_label="state.db", role=role)
        values = {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "synchronous", "busy_timeout", "wal_autocheckpoint")
        }
    finally:
        conn.close()
    if int(values["synchronous"]) != 2:
        raise RuntimeError("synchronous is not FULL")
    return values


def compact_storage(source: Path) -> dict[str, Any]:
    db = SessionDB(source)
    vacuum_calls = 0
    original_vacuum = db.vacuum

    def counted_vacuum() -> int:
        nonlocal vacuum_calls
        vacuum_calls += 1
        return original_vacuum()

    db.vacuum = counted_vacuum  # type: ignore[method-assign]
    try:
        if not db.fts_optimize_available():
            return {"ok": True, "already_compact": True, "vacuum_count": 0}
        result = db.optimize_fts_storage(vacuum=True)
    finally:
        db.close()
    if vacuum_calls != 1:
        raise RuntimeError(f"compact-storage executed {vacuum_calls} VACUUM paths")
    return {**result, "vacuum_count": vacuum_calls}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "archive-copy"):
        command = sub.add_parser(name)
        command.add_argument("--db", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--timeout-seconds", type=float, default=300.0)
    archive = sub.choices["archive-copy"]
    archive.add_argument("--older-than-days", type=float, required=True)
    archive.add_argument("--sessions-dir")
    archive.add_argument("--external-db", action="append", default=[])
    archive.add_argument("--max-broad-candidates", type=int, required=True)
    for name in ("verify", "verify-archive"):
        command = sub.add_parser(name)
        command.add_argument("--db", required=True)
    pragma = sub.add_parser("pragma-readback")
    pragma.add_argument("--db", required=True)
    pragma.add_argument("--role", choices=("writer", "reader", "maintenance"), required=True)
    compact = sub.add_parser("compact-storage")
    compact.add_argument("--db", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = _absolute_existing_db(args.db)
        if args.command == "backup":
            result = create_backup(source, _absolute_output(args.output), args.timeout_seconds)
        elif args.command == "archive-copy":
            sessions_dir = Path(args.sessions_dir).resolve() if args.sessions_dir else None
            external = [_absolute_existing_db(value) for value in args.external_db]
            plan = archive_plan(
                source,
                older_than_days=args.older_than_days,
                sessions_dir=sessions_dir,
                external_databases=external,
                max_broad_candidates=args.max_broad_candidates,
            )
            result = create_archive_copy(
                source,
                _absolute_output(args.output),
                timeout_seconds=args.timeout_seconds,
                plan=plan,
            )
        elif args.command in {"verify", "verify-archive"}:
            result = verify_database(source)
        elif args.command == "pragma-readback":
            result = pragma_readback(source, args.role)
        else:
            result = compact_storage(source)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
