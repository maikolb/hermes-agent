"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (legacy ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good


    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_legacy_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"


    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file regardless of board= arg."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced


# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:



    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]



    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:






    @pytest.mark.parametrize("archive", [True, False])
    def test_removed_board_requires_explicit_recreation(self, fresh_home, archive):
        # Regression for #23833: poll loops that call connect(board=slug) right
        # after remove_board() recreate an empty kanban.db at the same path
        # (connect() does mkdir(exist_ok=True)). If _INITIALIZED_PATHS still
        # contains the resolved path, the CREATE TABLE pass is skipped and
        # downstream readers hit `no such table: task_events`.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kb.connect_closing(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # A late poll must not resurrect the removed board. Only the explicit
        # board-creation authority may clear the external tombstone.
        with pytest.raises(RuntimeError, match="was removed"):
            kb.connect(board="recycle")
        with pytest.raises(RuntimeError, match="was removed"):
            kb.connect(db_path=db_path)
        assert not kb.board_exists("recycle")

        kb.create_board("recycle")
        with kb.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")

    @staticmethod
    def _owned_worktree(conn, tmp_path: Path, title: str = "owned") -> str:
        task_id = kb.create_task(
            conn,
            title=title,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "placeholder"),
        )
        owned_path = (tmp_path / ".worktrees" / task_id).resolve()
        repo_root = tmp_path.resolve()
        ownership = {
            "schema_version": 1,
            "task_id": task_id,
            "repo_root": str(repo_root),
            "git_common_dir": str(repo_root / ".git"),
            "git_dir": str(repo_root / ".git" / "worktrees" / task_id),
            "canonical_worktree": str(owned_path),
            "branch": f"project/{task_id}-delivery",
            "creation_nonce": "a" * 32,
            "created_at": 1,
        }
        ownership_json = json.dumps(
            ownership, sort_keys=True, separators=(",", ":")
        )
        ownership_fingerprint = hashlib.sha256(
            ownership_json.encode("utf-8")
        ).hexdigest()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, branch_name = ? WHERE id = ?",
                (
                    str(owned_path),
                    f"project/{task_id}-delivery",
                    task_id,
                ),
            )
            conn.execute(
                "UPDATE task_git_delivery SET ownership_json = ?, "
                "ownership_fingerprint = ? WHERE task_id = ?",
                (ownership_json, ownership_fingerprint, task_id),
            )
        return task_id

    @pytest.mark.parametrize("archive", [True, False])
    @pytest.mark.parametrize("cleanup_state", ["not_requested", "pending"])
    def test_remove_board_blocks_owned_unresolved_worktree_atomically(
        self,
        fresh_home,
        tmp_path,
        archive,
        cleanup_state,
    ):
        slug = f"blocked-{int(archive)}-{cleanup_state}"
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = self._owned_worktree(conn, tmp_path)
            if cleanup_state != "not_requested":
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE task_git_delivery SET cleanup_state = ? "
                        "WHERE task_id = ?",
                        (cleanup_state, task_id),
                    )

        with pytest.raises(ValueError, match="still owns worktree task"):
            kb.remove_board(slug, archive=archive)
        assert kb.board_exists(slug)
        # The failed lifecycle action rolls its tombstone back atomically.
        with kb.connect_closing(board=slug) as conn:
            assert kb.create_task(conn, title="still writable")

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_board_allows_owned_worktree_only_after_cleanup_complete(
        self,
        fresh_home,
        tmp_path,
        archive,
        monkeypatch,
    ):
        slug = f"reaped-{int(archive)}"
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = self._owned_worktree(conn, tmp_path)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?",
                    (task_id,),
                )
        monkeypatch.setattr(
            kb,
            "_validate_cleanup_obligation",
            lambda *_args, **_kwargs: (True, {"task_id": task_id}, ""),
        )

        result = kb.remove_board(slug, archive=archive)
        assert result["action"] == ("archived" if archive else "deleted")
        assert result["preserved_worktrees"] == []

    def test_metadata_archive_used_by_topic_close_blocks_pending_cleanup(
        self,
        fresh_home,
        tmp_path,
    ):
        slug = "topic-close-pending"
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = self._owned_worktree(conn, tmp_path)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_git_delivery SET cleanup_state = 'pending' "
                    "WHERE task_id = ?",
                    (task_id,),
                )

        with pytest.raises(ValueError, match="without terminal cleanup proof"):
            kb.set_board_archived(slug, True)
        assert kb.read_board_metadata(slug)["archived"] is False
        with kb.connect_closing(board=slug) as conn:
            assert kb.create_task(conn, title="archive rollback stayed active")

    def test_metadata_archive_quiesces_existing_connections_until_reactivated(
        self,
        fresh_home,
        tmp_path,
        monkeypatch,
    ):
        slug = "topic-close-clean"
        kb.create_board(slug)
        writer = kb.connect(board=slug)
        try:
            task_id = self._owned_worktree(writer, tmp_path)
            with kb.write_txn(writer):
                writer.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?",
                    (task_id,),
                )
            monkeypatch.setattr(
                kb,
                "_validate_cleanup_obligation",
                lambda *_args, **_kwargs: (True, {"task_id": task_id}, ""),
            )
            meta = kb.set_board_archived(slug, True)
            assert meta["archived"] is True
            with pytest.raises(RuntimeError, match="archived or removal"):
                kb.create_task(writer, title="must stay quiesced")
        finally:
            writer.close()

        kb.set_board_archived(slug, False)
        with kb.connect_closing(board=slug) as conn:
            assert kb.create_task(conn, title="reactivated")

    def test_remove_board_tombstone_closes_check_to_rename_writer_race(
        self,
        fresh_home,
        tmp_path,
        monkeypatch,
    ):
        slug = "remove-race"
        kb.create_board(slug)
        writer = kb.connect(board=slug)
        task_id = self._owned_worktree(writer, tmp_path)
        with kb.write_txn(writer):
            writer.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (task_id,),
            )
        monkeypatch.setattr(
            kb,
            "_validate_cleanup_obligation",
            lambda *_args, **_kwargs: (True, {"task_id": task_id}, ""),
        )
        original_rename = Path.rename
        raced = {"old_writer_blocked": False, "recreate_blocked": False}

        def _rename_after_concurrent_attempt(path, target):
            if path == kb.board_dir(slug):
                try:
                    with pytest.raises(RuntimeError, match="removal is in progress"):
                        kb.create_task(
                            writer,
                            title="late concurrent worktree",
                            workspace_kind="worktree",
                        )
                    raced["old_writer_blocked"] = True
                finally:
                    writer.close()
                result = original_rename(path, target)
                with pytest.raises(RuntimeError, match="was removed"):
                    kb.connect(board=slug)
                raced["recreate_blocked"] = True
                return result
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", _rename_after_concurrent_attempt)
        result = kb.remove_board(slug, archive=True)
        assert result["action"] == "archived"
        assert raced == {"old_writer_blocked": True, "recreate_blocked": True}
        assert not kb.board_exists(slug)

    def test_remove_board_retry_resumes_after_crash_before_archive_move(
        self,
        fresh_home,
        monkeypatch,
    ):
        slug = "resume-before-move"
        kb.create_board(slug)
        original_rename = Path.rename
        crashed = {"once": False}

        def _crash_before_move(path, target):
            if path == kb.board_dir(slug) and not crashed["once"]:
                crashed["once"] = True
                raise RuntimeError("simulated crash before archive move")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", _crash_before_move)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kb.remove_board(slug, archive=True)
        with pytest.raises(RuntimeError, match="was removed"):
            kb.connect(board=slug)

        result = kb.remove_board(slug, archive=True)
        assert result["action"] == "archived"
        assert Path(result["new_path"]).is_dir()

    def test_remove_board_retry_recovers_after_move_completed_before_return(
        self,
        fresh_home,
        monkeypatch,
    ):
        slug = "resume-after-move"
        kb.create_board(slug)
        original_rename = Path.rename
        crashed = {"once": False}

        def _crash_after_move(path, target):
            result = original_rename(path, target)
            if path == kb.board_dir(slug) and not crashed["once"]:
                crashed["once"] = True
                raise RuntimeError("simulated crash after archive move")
            return result

        monkeypatch.setattr(Path, "rename", _crash_after_move)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kb.remove_board(slug, archive=True)

        result = kb.remove_board(slug, archive=True)
        assert result["action"] == "archived"
        assert Path(result["new_path"]).is_dir()

    def test_remove_board_retry_resumes_partial_hard_delete(
        self,
        fresh_home,
        monkeypatch,
    ):
        slug = "resume-hard-delete"
        kb.create_board(slug)
        original_rmtree = kb.shutil.rmtree
        crashed = {"once": False}

        def _crash_once(path, *args, **kwargs):
            if Path(path) == kb.board_dir(slug) and not crashed["once"]:
                crashed["once"] = True
                raise RuntimeError("simulated hard-delete crash")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(kb.shutil, "rmtree", _crash_once)
        with pytest.raises(RuntimeError, match="simulated hard-delete crash"):
            kb.remove_board(slug, archive=False)

        result = kb.remove_board(slug, archive=False)
        assert result == {
            "slug": slug,
            "action": "deleted",
            "new_path": "",
            "preserved_worktrees": [],
        }
        assert not kb.board_dir(slug).exists()

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_board_refuses_tampered_ownership_instead_of_forgetting_it(
        self,
        fresh_home,
        tmp_path,
        archive,
    ):
        slug = f"tampered-{int(archive)}"
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = self._owned_worktree(conn, tmp_path)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_git_delivery SET ownership_fingerprint = ? "
                    "WHERE task_id = ?",
                    ("f" * 64, task_id),
                )

        with pytest.raises(ValueError, match="invalid ownership"):
            kb.remove_board(slug, archive=archive)
        assert kb.board_exists(slug)
        with kb.connect_closing(board=slug) as conn:
            assert kb.get_task(conn, task_id) is not None

    def test_remove_board_treats_empty_nonnull_ownership_as_corruption(
        self,
        fresh_home,
        tmp_path,
    ):
        slug = "empty-ownership"
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = self._owned_worktree(conn, tmp_path)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_git_delivery SET ownership_json = '', "
                    "ownership_fingerprint = '' WHERE task_id = ?",
                    (task_id,),
                )

        with pytest.raises(ValueError, match="invalid ownership"):
            kb.remove_board(slug, archive=True)
        assert kb.board_exists(slug)

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_board_forgets_foreign_metadata_but_preserves_checkout(
        self,
        fresh_home,
        tmp_path,
        archive,
        monkeypatch,
    ):
        slug = f"foreign-{int(archive)}"
        checkout = tmp_path / f"manual-checkout-{int(archive)}"
        checkout.mkdir()
        kb.create_board(slug)
        with kb.connect_closing(board=slug) as conn:
            task_id = kb.create_task(
                conn,
                title="Manual foreign checkout",
                workspace_kind="worktree",
                workspace_path=str(checkout),
                branch_name="feature/manual",
            )
        monkeypatch.setattr(
            kb,
            "_cleanup_git",
            lambda *_args, **_kwargs: pytest.fail(
                "foreign checkout must never enter Git cleanup"
            ),
        )

        result = kb.remove_board(slug, archive=archive)
        assert checkout.is_dir()
        assert result["preserved_worktrees"] == [
            {
                "task_id": task_id,
                "workspace_path": str(checkout),
                "branch_name": "feature/manual",
            }
        ]


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kb.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kb.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kb.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kb.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kb.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kb.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kb.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kb.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kb.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kb._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the legacy default.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True


    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []
