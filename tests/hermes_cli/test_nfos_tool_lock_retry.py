"""BLOCK_LESS5_20260910: o journal do nfos_tool espera um lock transitório em vez de falhar a entrega."""
import sqlite3
import threading
import time

import pytest

from hermes_cli import nfos_tool


def _db(tmp_path):
    path = tmp_path / "j.db"
    conn = sqlite3.connect(str(path), timeout=0.05)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_transaction_waits_for_a_transient_lock(tmp_path):
    path = _db(tmp_path)
    holder = sqlite3.connect(str(path), timeout=0.05, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")
    threading.Timer(1.2, holder.commit).start()
    writer = sqlite3.connect(str(path), timeout=0.05)
    started = time.monotonic()
    with nfos_tool._transaction(writer):
        writer.execute("INSERT INTO t VALUES (2)")
    elapsed = time.monotonic() - started
    assert writer.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    assert 1.0 <= elapsed < 15


def test_transaction_gives_up_with_a_transient_message(tmp_path, monkeypatch):
    path = _db(tmp_path)
    holder = sqlite3.connect(str(path), timeout=0.05)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(nfos_tool.time, "sleep", lambda s: None)
    writer = sqlite3.connect(str(path), timeout=0.05)
    with pytest.raises(nfos_tool.ToolExecutionError, match="transient"):
        with nfos_tool._transaction(writer):
            pass
    holder.rollback()


def test_non_lock_errors_propagate_unchanged(tmp_path):
    path = _db(tmp_path)
    writer = sqlite3.connect(str(path), timeout=0.05)
    with pytest.raises(sqlite3.OperationalError):
        with nfos_tool._transaction(writer):
            writer.execute("INSERT INTO missing VALUES (1)")
