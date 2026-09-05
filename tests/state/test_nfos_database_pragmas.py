import sqlite3

import pytest

from hermes_state import apply_database_pragmas


@pytest.mark.parametrize(
    ("role", "expected_timeout"),
    [("writer", 1000), ("reader", 30000), ("maintenance", 30000)],
)
def test_nfos_role_pragmas_preserve_full(tmp_path, role, expected_timeout):
    config = {
        "database": {
            "synchronous": "full",
            "writer_busy_timeout_ms": 1000,
            "reader_busy_timeout_ms": 30000,
            "maintenance_busy_timeout_ms": 30000,
            "wal_autocheckpoint": 4000,
        }
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
        conn = sqlite3.connect(tmp_path / f"{role}.db")
        try:
            apply_database_pragmas(conn, role=role)
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == expected_timeout
            assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 4000
        finally:
            conn.close()


def test_absent_role_settings_preserve_connection_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    conn = sqlite3.connect(tmp_path / "neutral.db")
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=7000")
        apply_database_pragmas(conn, role="reader")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
    finally:
        conn.close()


def test_nfos_unknown_connection_role_fails_closed(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError, match="unsupported database connection role"):
            apply_database_pragmas(conn, role="placeholder")
    finally:
        conn.close()


def test_nfos_refuses_configured_durability_downgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"database": {"synchronous": "normal"}},
    )
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        apply_database_pragmas(conn, role="writer")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        conn.close()
