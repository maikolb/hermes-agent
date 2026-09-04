#!/usr/bin/env python3
"""Run one bounded PASSIVE checkpoint against one explicit Hermes profile."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def checkpoint(
    profile_home: Path, profile_name: str, *, timeout_seconds: float
) -> dict[str, int | float | str]:
    if timeout_seconds <= 0:
        raise ValueError("timeout-seconds must be positive")
    home = profile_home.expanduser().resolve()
    if not profile_name.strip() or home.name != profile_name:
        raise ValueError("profile-home basename must exactly match profile-name")
    db_path = home / "state.db"
    if not db_path.is_file():
        raise ValueError(f"profile state database does not exist: {db_path}")
    started = time.monotonic()
    busy_timeout_ms = max(1, int(timeout_seconds * 1000))
    conn = sqlite3.connect(
        f"file:{db_path}?mode=rw", uri=True, timeout=0.0, isolation_level=None
    )
    try:
        # PASSIVE does not wait for readers to release WAL read marks. The
        # SQLite busy timeout bounds lock acquisition, which is the enforceable
        # timeout for this synchronous sqlite3 call.
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=FULL")
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        busy, log_pages, checkpointed_pages = conn.execute(
            "PRAGMA wal_checkpoint(PASSIVE)"
        ).fetchone()
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    if synchronous != 2:
        raise RuntimeError("synchronous is not FULL")
    return {
        "profile": profile_name,
        "mode": "PASSIVE",
        "busy_timeout_ms": busy_timeout_ms,
        "busy": int(busy),
        "log_pages": int(log_pages),
        "checkpointed_pages": int(checkpointed_pages),
        "elapsed_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()
    try:
        result = checkpoint(
            Path(args.profile_home), args.profile_name, timeout_seconds=args.timeout_seconds
        )
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
