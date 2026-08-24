import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars
from tools import kanban_tools


@pytest.fixture(autouse=True)
def clean_project_context(monkeypatch):
    monkeypatch.delenv("HERMES_PROJECT_BOARD", raising=False)
    reset_session_vars()
    yield
    reset_session_vars()


def test_omitted_board_uses_bound_board(monkeypatch):
    from hermes_cli import kanban_db

    calls = []
    monkeypatch.setattr(kanban_db, "connect", lambda *, board=None: calls.append(board) or object())
    tokens = set_session_vars(project_board="alpha")
    try:
        module, connection = kanban_tools._connect()
    finally:
        clear_session_vars(tokens)

    assert module is kanban_db
    assert connection is not None
    assert calls == ["alpha"]


def test_matching_explicit_board_is_allowed(monkeypatch):
    from hermes_cli import kanban_db

    calls = []
    monkeypatch.setattr(kanban_db, "connect", lambda *, board=None: calls.append(board) or object())
    tokens = set_session_vars(project_board=" Alpha ")
    try:
        kanban_tools._connect(board="alpha")
    finally:
        clear_session_vars(tokens)
    assert calls == ["alpha"]


def test_divergent_explicit_board_raises_before_db_connect(monkeypatch):
    from hermes_cli import kanban_db

    calls = []
    monkeypatch.setattr(kanban_db, "connect", lambda *, board=None: calls.append(board))
    tokens = set_session_vars(project_board="alpha")
    try:
        with pytest.raises(ValueError, match="bound to board.*alpha.*refusing explicit board.*beta"):
            kanban_tools._connect(board="beta")
    finally:
        clear_session_vars(tokens)
    assert calls == []


def test_unbound_board_preserves_existing_resolver_call(monkeypatch):
    from hermes_cli import kanban_db

    calls = []
    monkeypatch.setattr(kanban_db, "connect", lambda *, board=None: calls.append(board) or object())
    kanban_tools._connect()
    kanban_tools._connect(board="explicit")
    assert calls == [None, "explicit"]


def test_kanban_gate_honors_current_platform_toolsets(monkeypatch):
    config = {
        "toolsets": ["hermes-cli"],
        "platform_toolsets": {
            "telegram": ["kanban"],
            "discord": ["web"],
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)

    telegram_tokens = set_session_vars(platform="telegram")
    try:
        assert kanban_tools._profile_has_kanban_toolset() is True
        assert kanban_tools._check_kanban_orchestrator_mode() is True
    finally:
        clear_session_vars(telegram_tokens)

    discord_tokens = set_session_vars(platform="discord")
    try:
        assert kanban_tools._profile_has_kanban_toolset() is False
        assert kanban_tools._check_kanban_orchestrator_mode() is False
    finally:
        clear_session_vars(discord_tokens)


def test_kanban_gate_preserves_global_toolset_without_session_platform(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"toolsets": ["kanban"]},
    )

    assert kanban_tools._profile_has_kanban_toolset() is True


def test_concurrent_contexts_route_to_separate_boards(monkeypatch):
    from hermes_cli import kanban_db

    calls = []
    monkeypatch.setattr(kanban_db, "connect", lambda *, board=None: calls.append(board) or board)

    async def connect(board):
        tokens = set_session_vars(project_board=board)
        await asyncio.sleep(0)
        try:
            return kanban_tools._connect()[1]
        finally:
            clear_session_vars(tokens)

    async def run():
        return await asyncio.gather(connect("alpha"), connect("beta"))

    assert asyncio.run(run()) == ["alpha", "beta"]
    assert sorted(calls) == ["alpha", "beta"]


def _capture_create_idempotency(monkeypatch, session_kwargs, args):
    captured = []

    class Connection:
        def close(self):
            pass

    class Kanban:
        @staticmethod
        def create_task(conn, **kwargs):
            captured.append(kwargs["idempotency_key"])
            return f"task-{len(set(captured))}"

        @staticmethod
        def get_task(conn, task_id):
            return SimpleNamespace(
                id=task_id,
                status="running",
                workspace_kind="scratch",
                workspace_path=None,
                project_id=None,
            )

    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (Kanban, Connection()))
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *args: False)
    tokens = set_session_vars(**session_kwargs)
    try:
        result = kanban_tools._handle_create({"assignee": "worker", **args})
    finally:
        clear_session_vars(tokens)
    assert '"ok": true' in result
    return captured[0]


def test_kanban_create_derives_stable_request_idempotency_key(monkeypatch):
    session = {
        "profile": "Team Blue",
        "platform": "telegram",
        "chat_id": "-1001",
        "thread_id": "42",
        "message_id": "900",
    }
    first = _capture_create_idempotency(
        monkeypatch,
        session,
        {"title": "  Ship   Alpha  ", "idempotency_key": "   "},
    )
    second = _capture_create_idempotency(monkeypatch, session, {"title": "ship alpha"})

    assert first == second
    assert first == "project-os:team-blue:telegram:-1001:42:900:kanban-create:ship-alpha"


def test_kanban_create_request_key_varies_by_title_thread_and_profile(monkeypatch):
    base = {"platform": "telegram", "chat_id": "chat", "message_id": "message"}
    keys = {
        _capture_create_idempotency(
            monkeypatch, {**base, "profile": profile, "thread_id": thread}, {"title": title}
        )
        for profile, thread, title in (
            ("one", "10", "Alpha"),
            ("one", "10", "Beta"),
            ("one", "11", "Alpha"),
            ("two", "10", "Alpha"),
        )
    }
    keys.add(_capture_create_idempotency(
        monkeypatch,
        {**base, "profile": "one", "thread_id": "10", "message_id": "other-message"},
        {"title": "Alpha"},
    ))
    assert len(keys) == 5


def test_kanban_create_same_message_and_normalized_title_returns_same_task(monkeypatch):
    import json

    tasks_by_key = {}

    class Connection:
        def close(self):
            pass

    class Kanban:
        @staticmethod
        def create_task(conn, **kwargs):
            key = kwargs["idempotency_key"]
            return tasks_by_key.setdefault(key, f"task-{len(tasks_by_key) + 1}")

        @staticmethod
        def get_task(conn, task_id):
            return SimpleNamespace(
                id=task_id, status="running", workspace_kind="scratch",
                workspace_path=None, project_id=None,
            )

    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (Kanban, Connection()))
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *args: False)
    tokens = set_session_vars(
        profile="default", platform="telegram", chat_id="-1001",
        thread_id="42", message_id="900",
    )
    try:
        first = json.loads(kanban_tools._handle_create({
            "title": " Ship   Alpha ", "assignee": "worker",
        }))
        second = json.loads(kanban_tools._handle_create({
            "title": "ship alpha", "assignee": "worker",
        }))
    finally:
        clear_session_vars(tokens)

    assert first["task_id"] == second["task_id"] == "task-1"
    assert len(tasks_by_key) == 1


def test_kanban_create_explicit_key_wins_unchanged(monkeypatch):
    explicit = "Caller Supplied Key / unchanged"
    captured = _capture_create_idempotency(
        monkeypatch,
        {
            "profile": "one",
            "platform": "telegram",
            "chat_id": "chat",
            "message_id": "message",
        },
        {"title": "Alpha", "idempotency_key": explicit},
    )
    assert captured == explicit


def test_kanban_create_without_session_message_preserves_none(monkeypatch):
    captured = _capture_create_idempotency(
        monkeypatch,
        {"profile": "one", "platform": "telegram", "chat_id": "chat"},
        {"title": "Alpha"},
    )
    assert captured is None


def test_kanban_create_unicode_titles_remain_distinct_and_key_is_bounded(monkeypatch):
    session = {
        "profile": "default", "platform": "telegram", "chat_id": "chat",
        "thread_id": "topic", "message_id": "message",
    }
    first = _capture_create_idempotency(monkeypatch, session, {"title": "项目一"})
    second = _capture_create_idempotency(monkeypatch, session, {"title": "项目二"})
    long_key = _capture_create_idempotency(monkeypatch, session, {"title": "x" * 1000})

    assert first != second
    assert len(long_key) <= 255


def test_bound_project_rejects_workspace_escape_before_db_connect(monkeypatch, tmp_path):
    project_root = tmp_path / "team" / "project"
    project_root.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    monkeypatch.setattr(
        kanban_tools,
        "_connect",
        lambda board=None: pytest.fail("DB must not be opened for an escaped path"),
    )
    tokens = set_session_vars(
        project_id="project-id",
        project_board="project-board",
        project_workdir=str(project_root),
    )
    try:
        result = kanban_tools._handle_create({
            "title": "unsafe",
            "assignee": "worker",
            "workspace_kind": "dir",
            "workspace_path": str(outside),
        })
    finally:
        clear_session_vars(tokens)

    assert "escapes the bound project workspace" in result


def test_bound_project_dir_defaults_to_canonical_workspace(monkeypatch, tmp_path):
    project_root = (tmp_path / "team" / "project").resolve()
    project_root.mkdir(parents=True)
    captured = {}

    class Connection:
        def close(self):
            pass

    class Kanban:
        @staticmethod
        def create_task(conn, **kwargs):
            captured.update(kwargs)
            return "task-1"

        @staticmethod
        def get_task(conn, task_id):
            return SimpleNamespace(
                id=task_id,
                status="running",
                workspace_kind=captured["workspace_kind"],
                workspace_path=captured["workspace_path"],
                project_id=captured["project_id"],
            )

    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (Kanban, Connection()))
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *args: False)
    tokens = set_session_vars(
        project_id="project-id",
        project_board="project-board",
        project_workdir=str(project_root),
    )
    try:
        result = kanban_tools._handle_create({
            "title": "safe",
            "assignee": "worker",
            "workspace_kind": "dir",
        })
    finally:
        clear_session_vars(tokens)

    assert '"ok": true' in result
    assert captured["workspace_path"] == str(project_root)


def test_bound_project_rejects_project_override(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(
        kanban_tools,
        "_connect",
        lambda board=None: pytest.fail("DB must not be opened for another project"),
    )
    tokens = set_session_vars(
        project_id="bound-project",
        project_board="project-board",
        project_workdir=str(project_root),
    )
    try:
        result = kanban_tools._handle_create({
            "title": "unsafe project",
            "assignee": "worker",
            "project": "different-project",
        })
    finally:
        clear_session_vars(tokens)

    assert "refusing explicit project override" in result


def test_bound_project_requires_repo_initializes_and_registers_project(
    monkeypatch, tmp_path
):
    from hermes_cli import projects_db

    project_root = (tmp_path / "team" / "project").resolve()
    project_root.mkdir(parents=True)
    project_db = tmp_path / "profile" / "projects.db"
    captured = {}

    class Connection:
        def close(self):
            pass

    class Kanban:
        @staticmethod
        def create_task(conn, **kwargs):
            captured.update(kwargs)
            return "task-1"

        @staticmethod
        def get_task(conn, task_id):
            return SimpleNamespace(
                id=task_id,
                status="running",
                workspace_kind="worktree",
                workspace_path=str(project_root / ".worktrees" / task_id),
                project_id=captured["project_id"],
            )

    original_connect_closing = projects_db.connect_closing
    monkeypatch.setattr(
        projects_db,
        "connect_closing",
        lambda: original_connect_closing(db_path=project_db),
    )
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (Kanban, Connection()))
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *args: False)
    tokens = set_session_vars(
        project_id="alpha",
        project_board="team-alpha",
        project_workdir=str(project_root),
    )
    try:
        result = json.loads(
            kanban_tools._handle_create(
                {
                    "title": "Implement feature",
                    "assignee": "worker",
                    "requires_repo": True,
                }
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert captured["workspace_kind"] == "scratch"
    assert captured["project_id"].startswith("p_")
    head = kanban_tools._run_project_git(project_root, ["rev-parse", "--verify", "HEAD"])
    assert head.returncode == 0
    with original_connect_closing(db_path=project_db) as conn:
        project = projects_db.get_project(conn, "alpha")
    assert project is not None
    assert Path(project.primary_path) == project_root
    assert project.board_slug == "team-alpha"


def test_bound_project_worktree_deterministically_requires_local_repo(
    monkeypatch, tmp_path
):
    from hermes_cli import projects_db

    project_root = (tmp_path / "team" / "worktree-project").resolve()
    project_root.mkdir(parents=True)
    project_db = tmp_path / "profile" / "projects.db"
    captured = {}

    class Connection:
        def close(self):
            pass

    class Kanban:
        @staticmethod
        def create_task(conn, **kwargs):
            captured.update(kwargs)
            return "task-worktree"

        @staticmethod
        def get_task(conn, task_id):
            return SimpleNamespace(
                id=task_id,
                status="running",
                workspace_kind="worktree",
                workspace_path=str(project_root / ".worktrees" / task_id),
                project_id=captured["project_id"],
            )

    original_connect_closing = projects_db.connect_closing
    monkeypatch.setattr(
        projects_db,
        "connect_closing",
        lambda: original_connect_closing(db_path=project_db),
    )
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (Kanban, Connection()))
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *args: False)
    tokens = set_session_vars(
        project_id="worktree-alpha",
        project_board="team-worktree-alpha",
        project_workdir=str(project_root),
    )
    try:
        result = json.loads(
            kanban_tools._handle_create(
                {
                    "title": "Implement in isolated checkout",
                    "assignee": "worker",
                    "workspace_kind": "worktree",
                }
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert captured["workspace_kind"] == "worktree"
    assert captured["project_id"].startswith("p_")
    head = kanban_tools._run_project_git(project_root, ["rev-parse", "--verify", "HEAD"])
    assert head.returncode == 0


def test_requires_repo_is_idempotent_and_does_not_create_remote(monkeypatch, tmp_path):
    from hermes_cli import projects_db

    project_root = (tmp_path / "project").resolve()
    project_root.mkdir()
    project_db = tmp_path / "profile" / "projects.db"
    original_connect_closing = projects_db.connect_closing
    monkeypatch.setattr(
        projects_db,
        "connect_closing",
        lambda: original_connect_closing(db_path=project_db),
    )

    first = kanban_tools._ensure_bound_project_repo(
        "alpha", "team-alpha", str(project_root)
    )
    second = kanban_tools._ensure_bound_project_repo(
        "alpha", "team-alpha", str(project_root)
    )

    assert first == second
    remotes = kanban_tools._run_project_git(project_root, ["remote"])
    assert remotes.returncode == 0
    assert remotes.stdout.strip() == ""


def test_requires_repo_fails_closed_outside_bound_project(monkeypatch):
    monkeypatch.setattr(
        kanban_tools,
        "_connect",
        lambda board=None: pytest.fail("board DB must not open without a project"),
    )
    result = kanban_tools._handle_create(
        {
            "title": "Implement feature",
            "assignee": "worker",
            "requires_repo": True,
        }
    )
    assert "only inside a bound project Topic" in result
