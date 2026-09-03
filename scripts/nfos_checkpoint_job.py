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
    home = profile_home.expanduser().resolve()
    if not profile_name.strip() or home.name != profile_name:
        raise ValueError("profile-home basename must exactly match profile-name")
    db_path = home / "state.db"
    if not db_path.is_file():
        raise ValueError(f"profile state database does not exist: {db_path}")
    deadline = time.monotonic() + timeout_seconds
    conn = sqlite3.connect(
        f"file:{db_path}?mode=rw", uri=True, timeout=0.0, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout_seconds * 1000))}")
        conn.execute("PRAGMA synchronous=FULL")
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        busy, log_pages, checkpointed_pages = conn.execute(
            "PRAGMA wal_checkpoint(PASSIVE)"
        ).fetchone()
    finally:
        conn.close()
    elapsed = timeout_seconds - max(0.0, deadline - time.monotonic())
    if elapsed > timeout_seconds + 0.1:
        raise RuntimeError("checkpoint exceeded its deadline")
    if synchronous != 2:
        raise RuntimeError("synchronous is not FULL")
    return {
        "profile": profile_name,
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
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
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
