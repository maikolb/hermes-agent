import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB
from scripts.nfos_state_maintenance import (
    archive_plan,
    compact_storage,
    create_archive_copy,
    create_backup,
)
from tests.test_hermes_state import (
    TestFTSExternalContentMigration as _LegacyFTSFixture,
)


def test_backup_converges_under_continuous_wal_writer(tmp_path):
    source = tmp_path / "state.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT)")
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(source, timeout=0.1)
        try:
            while not stop.is_set():
                conn.execute("INSERT INTO events(value) VALUES('x')")
                conn.commit()
                time.sleep(0.001)
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        destination = tmp_path / "backup.db"
        report = create_backup(source, destination, 5.0)
    finally:
        stop.set()
        thread.join(timeout=2)
    assert report["quick_check"] == "ok"
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 0


def test_archive_copy_preserves_active_read_behavior_and_exact_mode(tmp_path):
    source = tmp_path / "state.db"
    db = SessionDB(source)
    db.create_session("old", "cli")
    db.append_message("old", "user", "needle")
    old = time.time() - 120 * 86400
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=?, end_reason='complete' WHERE id='old'",
            (old, old + 1),
        )
    before_list = [row["id"] for row in db.search_sessions(limit=100)]
    before_resume = db.export_session("old")
    before_search = db.search_messages("needle")
    db.close()
    source.chmod(0o640)

    plan = archive_plan(
        source,
        older_than_days=90,
        sessions_dir=None,
        external_databases=[],
        max_broad_candidates=1,
    )
    archive = tmp_path / "archive.db"
    create_archive_copy(source, archive, timeout_seconds=5.0, plan=plan)

    reopened = SessionDB(source, read_only=True)
    try:
        assert [row["id"] for row in reopened.search_sessions(limit=100)] == before_list
        assert reopened.export_session("old") == before_resume
        assert reopened.search_messages("needle") == before_search
    finally:
        reopened.close()
    assert archive.stat().st_mode & 0o777 == source.stat().st_mode & 0o777
    assert archive.with_suffix(".db.manifest.json").is_file()


def test_already_compact_path_does_not_request_vacuum(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.close()
    result = compact_storage(tmp_path / "state.db")
    assert result["already_compact"] is True
    assert result["vacuumed"] is False


def test_noncompact_real_legacy_db_compacts_and_vacuums(tmp_path):
    source = tmp_path / "legacy-v22.db"
    _LegacyFTSFixture._build_v22_db(source)

    result = compact_storage(source)
    assert result["ok"] is True
    assert result["vacuumed"] is True
    assert result["already_compact"] is False

    reopened = SessionDB(source)
    try:
        assert reopened.fts_optimize_available() is False
        assert len(reopened.search_messages("deployment")) == 1
        assert len(reopened.search_messages("TOOLBLOB")) == 1
    finally:
        reopened.close()


def test_noncompact_rejects_missing_vacuum_confirmation(tmp_path, monkeypatch):
    source = tmp_path / "legacy-v22.db"
    _LegacyFTSFixture._build_v22_db(source)
    original = SessionDB.optimize_fts_storage

    def without_vacuum_confirmation(self, *, vacuum):
        result = original(self, vacuum=False)
        return {**result, "vacuumed": False}

    monkeypatch.setattr(SessionDB, "optimize_fts_storage", without_vacuum_confirmation)
    with pytest.raises(RuntimeError, match="did not complete VACUUM"):
        compact_storage(source)
