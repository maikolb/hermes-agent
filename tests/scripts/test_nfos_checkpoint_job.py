import sqlite3

import pytest

from scripts.nfos_checkpoint_job import checkpoint


def test_checkpoint_is_profile_scoped_full_and_bounded(tmp_path):
    home = tmp_path / "hermes-project-factory"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(x)")
    result = checkpoint(home, "hermes-project-factory", timeout_seconds=2.0)
    assert result["profile"] == "hermes-project-factory"
    assert result["elapsed_seconds"] <= 2.1


def test_checkpoint_rejects_profile_mismatch(tmp_path):
    home = tmp_path / "default"
    home.mkdir()
    (home / "state.db").touch()
    with pytest.raises(ValueError, match="exactly match"):
        checkpoint(home, "hermes-project-factory", timeout_seconds=1.0)
