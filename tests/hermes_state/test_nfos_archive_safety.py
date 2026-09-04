import json
import time

from hermes_state import SessionDB


def _old_and_ended(db: SessionDB, *session_ids: str) -> None:
    old = time.time() - 120 * 86400
    placeholders = ",".join("?" for _ in session_ids)
    with db._lock:
        db._conn.execute(
            f"UPDATE sessions SET started_at=?, ended_at=?, end_reason='complete' "
            f"WHERE id IN ({placeholders})",
            (old, old + 1, *session_ids),
        )


def test_archive_plan_protects_dependency_closure(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    try:
        for session_id in (
            "free",
            "parent",
            "live-child",
            "delegate-parent",
            "delegate-child",
            "shared-old",
            "shared-live",
            "disk",
        ):
            db.create_session(session_id, "cli", system_prompt="shared" if session_id.startswith("shared") else None)
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET parent_session_id=? WHERE id=?",
                ("parent", "live-child"),
            )
            db._conn.execute(
                "UPDATE sessions SET parent_session_id=?, model_config=? WHERE id=?",
                ("delegate-parent", json.dumps({"_delegate_from": "delegate-parent"}), "delegate-child"),
            )
        _old_and_ended(
            db,
            "free",
            "parent",
            "delegate-parent",
            "delegate-child",
            "shared-old",
            "disk",
        )
        (sessions_dir / "disk.jsonl").write_text("{}\n", encoding="utf-8")

        plan = db.plan_physical_archive(older_than_days=90, sessions_dir=sessions_dir)

        assert plan["deletion_enabled"] is False
        assert [row["id"] for row in plan["eligible_sessions"]] == ["free"]
        assert plan["protected"]["parent"] == ["ancestor-of-retained-session"]
        assert "delegate-closure" in plan["protected"]["delegate-child"]
        assert plan["protected"]["shared-old"] == ["shared-system-prompt"]
        assert plan["protected"]["disk"] == ["disk-transcript"]
    finally:
        db.close()


def test_archive_plan_terminates_on_retained_lineage_cycle(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("retained-a", "cli")
        db.create_session("retained-b", "cli")
        db.create_session("old-independent", "cli")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET parent_session_id=? WHERE id=?",
                ("retained-b", "retained-a"),
            )
            db._conn.execute(
                "UPDATE sessions SET parent_session_id=? WHERE id=?",
                ("retained-a", "retained-b"),
            )
        _old_and_ended(db, "old-independent")

        plan = db.plan_physical_archive(older_than_days=90)

        assert plan["deletion_enabled"] is False
        assert [row["id"] for row in plan["eligible_sessions"]] == [
            "old-independent"
        ]
        assert "retained-a" not in plan["protected"]
        assert "retained-b" not in plan["protected"]
    finally:
        db.close()


def test_archive_plan_protects_indexed_external_reference_and_blocks_scan(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("candidate", "cli")
    _old_and_ended(db, "candidate")
    indexed = tmp_path / "indexed.db"
    unindexed = tmp_path / "unindexed.db"
    import sqlite3

    with sqlite3.connect(indexed) as external:
        external.execute("CREATE TABLE cards(id TEXT PRIMARY KEY, session_id TEXT)")
        external.execute("CREATE INDEX idx_cards_session ON cards(session_id)")
        external.execute("INSERT INTO cards VALUES('c1', 'candidate')")
    with sqlite3.connect(unindexed) as external:
        external.execute("CREATE TABLE cards(id TEXT PRIMARY KEY, session_id TEXT)")
        external.execute("INSERT INTO cards VALUES('c1', 'candidate')")
    try:
        referenced = db.plan_physical_archive(
            older_than_days=90, external_database_paths=[indexed]
        )
        assert referenced["protected"]["candidate"] == ["external-database-reference"]

        blocked = db.plan_physical_archive(
            older_than_days=90, external_database_paths=[unindexed]
        )
        assert blocked["eligible_count"] == 0
        assert blocked["blockers"][0].startswith("unindexed-session-reference:")
    finally:
        db.close()
