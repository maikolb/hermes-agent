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
