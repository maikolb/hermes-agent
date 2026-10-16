"""Teto de chunks por chamada (patch 10/09/2026)."""
from pathlib import Path

from hermes_cli import nfos_tool


def test_chunk_cap_has_a_sane_default():
    assert nfos_tool._chunk_cap() >= 4096


def test_spill_path_lives_outside_the_product_repo(tmp_path: Path):
    db = tmp_path / "hermes" / "kanban" / "boards" / "dovcrm" / "kanban.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    target = nfos_tool._spill_path(db, "t_abc", "call_1")
    assert target == tmp_path / "hermes" / "reports" / "dovcrm" / "tool-output" / "t_abc" / "call_1.log"
    assert target.parent.is_dir()


def test_spill_path_for_root_board(tmp_path: Path):
    db = tmp_path / "kanban.db"
    db.write_bytes(b"")
    target = nfos_tool._spill_path(db, "t_x", "c")
    assert target == tmp_path / "reports" / "default" / "tool-output" / "t_x" / "c.log"


def test_read_call_above_the_cap_names_the_overflow_file(tmp_path: Path, monkeypatch):
    # NFOS_CAMINHO_NO_PONTO_DE_USO_20261016: past the cap the database keeps the head and one marker; the rest is in a
    # file, and the answer of `call --call <id>` must say which.
    import sqlite3
    import sys

    from hermes_cli import nfos_delivery as delivery

    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as conn:
        conn.executescript('''
        CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,current_run_id INTEGER);
        CREATE TABLE task_runs(id INTEGER PRIMARY KEY,task_id TEXT,status TEXT);
        CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
            run_id INTEGER,kind TEXT,payload TEXT,created_at INTEGER);
        INSERT INTO tasks VALUES('t_one','running',7);
        INSERT INTO task_runs VALUES(7,'t_one','running');
        ''')
    monkeypatch.setattr(nfos_tool, "_chunk_cap", lambda: 4096)
    result = nfos_tool.run_command(board, task_id="t_one", run_id=7, call_id="big", cwd=tmp_path, timeout_seconds=20,
        argv=[sys.executable, "-u", "-c", "print('A' * 9000);print('Z' * 9000)"])
    assert result["status"] == "succeeded"

    with nfos_tool.connect(board) as conn:
        answer = delivery.read_call(conn, "big")

    expected = tmp_path / "reports" / "default" / "tool-output" / "t_one" / "big.log"
    assert answer["overflow_path"] == str(expected)
    assert expected.is_file() and "Z" * 9000 in expected.read_text()
    assert str(expected) in answer["output"]  # the marker the database keeps names the same file
