"""Regression coverage for schema-v25 content-addressed system prompts."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_state import SCHEMA_VERSION, SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _prompt_count(db: SessionDB) -> int:
    return int(
        db._conn.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0]
    )


def test_fresh_db_deduplicates_and_hydrates_session_readers(db):
    prompt = "You are Hermes.\n" + ("Follow the profile policy.\n" * 5)
    db.create_session(
        "gateway",
        "telegram",
        session_key="agent:main:telegram:dm:test",
        user_id="user",
        chat_id="test",
        chat_type="dm",
        system_prompt=prompt,
    )
    db.append_message("gateway", "user", "hello")
    db.set_session_title("gateway", "Gateway test")
    db.request_handoff("gateway", "telegram")
    db.create_session("cli", "cli", system_prompt=prompt)
    db.create_session("cron_job_1", "cron", system_prompt=prompt)

    version = db._conn.execute(
        "SELECT version FROM schema_version LIMIT 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION == 25
    assert _prompt_count(db) == 1
    raw = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions ORDER BY id"
    ).fetchall()
    assert all(row["system_prompt"] is None for row in raw)
    assert len({row["system_prompt_hash"] for row in raw}) == 1

    assert db.get_session("gateway")["system_prompt"] == prompt
    assert db.get_session_by_title("Gateway test")["system_prompt"] == prompt
    assert {row["system_prompt"] for row in db.list_sessions_rich()} == {prompt}
    assert {row["system_prompt"] for row in db.search_sessions()} == {prompt}
    assert db.export_session("gateway")["system_prompt"] == prompt
    assert db.list_gateway_sessions()[0]["system_prompt"] == prompt
    assert db.find_latest_gateway_session_for_peer(
        source="telegram",
        session_key="agent:main:telegram:dm:test",
    )["system_prompt"] == prompt
    assert db.list_pending_handoffs()[0]["system_prompt"] == prompt
    assert db.list_cron_job_runs("job")[0]["system_prompt"] == prompt
    assert db.list_unlinked_telegram_sessions_for_user(
        chat_id="test", user_id="user", limit=10
    )[0]["system_prompt"] == prompt


def test_v23_inline_prompts_migrate_to_content_addressed_storage(tmp_path):
    db_path = tmp_path / "legacy-v23.db"
    legacy_prompt = "Legacy system prompt\n" + ("same policy\n" * 20)

    legacy = SessionDB(db_path=db_path)
    legacy.create_session("s1", "cli")
    legacy.create_session("s2", "telegram")
    legacy._conn.execute(
        "UPDATE sessions SET system_prompt = ?, system_prompt_hash = NULL",
        (legacy_prompt,),
    )
    legacy._conn.execute("DELETE FROM system_prompts")
    legacy._conn.execute("UPDATE schema_version SET version = 23")
    legacy._conn.commit()
    legacy.close()

    migrated = SessionDB(db_path=db_path)
    try:
        assert migrated.get_session("s1")["system_prompt"] == legacy_prompt
        assert migrated.get_session("s2")["system_prompt"] == legacy_prompt
        assert _prompt_count(migrated) == 1
        rows = migrated._conn.execute(
            "SELECT system_prompt, system_prompt_hash FROM sessions ORDER BY id"
        ).fetchall()
        assert all(row["system_prompt"] is None for row in rows)
        assert len({row["system_prompt_hash"] for row in rows}) == 1
        assert migrated._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()[0] == SCHEMA_VERSION
    finally:
        migrated.close()


def test_mixed_v25_rows_use_fallback_then_reconcile_on_reopen(tmp_path):
    db_path = tmp_path / "mixed-v25.db"
    db = SessionDB(db_path=db_path)
    db.create_session("hashed", "cli", system_prompt="hashed prompt")
    db.create_session("inline", "cli")
    db._conn.execute(
        "UPDATE sessions SET system_prompt = 'inline fallback' WHERE id = 'inline'"
    )
    db._conn.execute(
        "UPDATE sessions SET system_prompt = 'stale inline' WHERE id = 'hashed'"
    )
    db._conn.commit()

    # A live mixed DB remains readable before the next initialization pass.
    assert db.get_session("hashed")["system_prompt"] == "hashed prompt"
    assert db.get_session("inline")["system_prompt"] == "inline fallback"
    exported = {row["id"]: row for row in db.export_all()}
    assert exported["hashed"]["system_prompt"] == "hashed prompt"
    assert exported["inline"]["system_prompt"] == "inline fallback"
    db.close()

    # Reopening a DB already marked v25 proactively promotes inline-only rows
    # and clears stale inline copies without replacing an authoritative hash.
    reconciled = SessionDB(db_path=db_path)
    try:
        assert reconciled.get_session("hashed")["system_prompt"] == "hashed prompt"
        assert reconciled.get_session("inline")["system_prompt"] == "inline fallback"
        rows = reconciled._conn.execute(
            "SELECT system_prompt, system_prompt_hash FROM sessions ORDER BY id"
        ).fetchall()
        assert all(row["system_prompt"] is None for row in rows)
        assert all(row["system_prompt_hash"] is not None for row in rows)
        assert _prompt_count(reconciled) == 2
    finally:
        reconciled.close()


def test_updates_and_imports_never_write_inline_prompt(tmp_path, db):
    db.create_session("updated", "cli")
    db.update_system_prompt("updated", "updated prompt")
    raw = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'updated'"
    ).fetchone()
    assert raw["system_prompt"] is None
    assert raw["system_prompt_hash"] is not None

    exported = db.export_session("updated")
    target = SessionDB(db_path=tmp_path / "imported.db")
    try:
        result = target.import_sessions([exported])
        assert result["ok"] is True
        imported = target._conn.execute(
            "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'updated'"
        ).fetchone()
        assert imported["system_prompt"] is None
        assert imported["system_prompt_hash"] is not None
        assert target.get_session("updated")["system_prompt"] == "updated prompt"
    finally:
        target.close()


def test_prompt_cleanup_preserves_live_references_and_covers_delete_paths(db):
    shared = "shared prompt"
    db.create_session("shared-1", "cli", system_prompt=shared)
    db.create_session("shared-2", "cli", system_prompt=shared)
    assert db.delete_session("shared-1") is True
    assert _prompt_count(db) == 1
    assert db.get_session("shared-2")["system_prompt"] == shared
    assert db.set_session_archived("shared-2", True) is True
    assert _prompt_count(db) == 1
    assert db.delete_session("shared-2") is True
    assert _prompt_count(db) == 0

    db.create_session("single-empty", "cli", system_prompt="single")
    assert db.delete_session_if_empty("single-empty") is True
    assert _prompt_count(db) == 0

    db.create_session("bulk", "cli", system_prompt="bulk")
    assert db.delete_sessions(["bulk"]) == 1
    assert _prompt_count(db) == 0

    db.create_session("ended-empty", "cli", system_prompt="ended")
    db.end_session("ended-empty", "user_exit")
    assert db.delete_empty_sessions() == 1
    assert _prompt_count(db) == 0

    db.create_session("pruned", "cli", system_prompt="pruned")
    db.end_session("pruned", "user_exit")
    assert db.prune_sessions(
        older_than_days=None, started_before=time.time() + 1
    ) == 1
    assert _prompt_count(db) == 0

    db.create_session("ghost", "tui", system_prompt="ghost")
    db.end_session("ghost", "user_exit")
    db._conn.execute("UPDATE sessions SET started_at = 0 WHERE id = 'ghost'")
    db._conn.commit()
    assert db.prune_empty_ghost_sessions() == 1
    assert _prompt_count(db) == 0


def test_model_and_route_reset_clear_hash_and_collect_orphan(db):
    db.create_session("model", "cli", system_prompt="model prompt")
    db.update_session_model("model", "new-model")
    row = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'model'"
    ).fetchone()
    assert row["system_prompt"] is None
    assert row["system_prompt_hash"] is None
    assert _prompt_count(db) == 0

    db.update_system_prompt("model", "route prompt")
    db.update_session_billing_route(
        "model", provider="test", base_url="https://example.test/v1"
    )
    row = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'model'"
    ).fetchone()
    assert row["system_prompt"] is None
    assert row["system_prompt_hash"] is None
    assert _prompt_count(db) == 0


def test_compact_rows_omit_hash_and_do_not_read_shared_prompt(db):
    db.create_session("s1", "cli", system_prompt="never materialize me")

    def deny_prompt_reads(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table == "system_prompts":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db._conn.set_authorizer(deny_prompt_reads)
    try:
        rows = db.list_sessions_rich(
            compact_rows=True, order_by_last_active=True
        )
        rich = db._get_session_rich_row("s1", compact_rows=True)
    finally:
        db._conn.set_authorizer(None)

    assert rows[0]["id"] == "s1"
    assert rich["id"] == "s1"
    assert "system_prompt" not in rows[0]
    assert "system_prompt_hash" not in rows[0]
    assert "system_prompt" not in rich
    assert "system_prompt_hash" not in rich
