"""Non-repository intake retains project context and real artifact delivery."""


def test_report_in_project_board_completes_with_preserved_artifact(tmp_path, monkeypatch):
    import hashlib
    import json
    from pathlib import Path

    home = tmp_path / "home"
    kanban_home = tmp_path / "kanban"
    (home / "profiles" / "executor").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  default_assignee: executor\n  auto_subscribe_on_create: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "report-audit")

    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_cli import kanban_db as kb, projects_db as pdb
    from hermes_cli.profiles import profile_exists
    from tools import kanban_tools as kt

    home_token = set_hermes_home_override(home)
    session_tokens = []
    try:
        assert profile_exists("executor")
        repo = tmp_path / "project-repo"
        repo.mkdir()
        with pdb.connect_closing() as conn:
            project_id = pdb.create_project(
                conn, name="Report audit", primary_path=str(repo),
            )
        kb.create_board(slug="report-audit", name="Report audit")
        policy = {
            "required": True, "head_remote": "origin", "base_remote": "upstream",
            "expected_head_remote_url": "https://example.invalid/repo.git",
            "expected_base_remote_url": "https://example.invalid/repo.git",
            "head_repository": "example/repo", "base_repository": "example/repo",
            "base_branch": "main", "required_checks": ["tests"],
        }
        kb.write_board_metadata("report-audit", project_id=project_id, git_delivery=policy)
        board_before = kb.board_metadata_path("report-audit").read_bytes()
        session_tokens = set_session_vars(
            profile="executor", project_id=project_id,
            project_board="report-audit", project_workdir=str(repo),
        )
        created = json.loads(kt._handle_create({
            "title": "Read-only audit report", "assignee": "executor",
            "requires_repo": False, "project_id": project_id, "board": "report-audit",
        }))
        assert created.get("ok"), created
        task_id = created["task_id"]
        with kb.connect_closing(board="report-audit") as conn:
            task = kb.get_task(conn, task_id)
            assert task.workspace_kind == "scratch"
            assert task.project_id == project_id
            assert kb.get_git_delivery_contract(conn, task_id) is None
            workspace = kb.resolve_workspace(task, board="report-audit")
            kb.set_workspace_path(conn, task_id, workspace)
            assert kb.claim_task(conn, task_id) is not None
        report = workspace / "audit.md"
        content = b"# Audit result\nRead-only findings verified from the supplied fixture.\n"
        report.write_bytes(content)
        completed = json.loads(kt._handle_complete({
            "task_id": task_id, "board": "report-audit",
            "summary": "Delivered the read-only audit report; artifact preserved for review.",
            "artifacts": [str(report)],
        }))
        assert completed.get("ok"), completed
        with kb.connect_closing(board="report-audit") as conn:
            assert kb.get_task(conn, task_id).status == "done"
            attachments = kb.list_attachments(conn, task_id)
            assert len(attachments) == 1
            saved = Path(attachments[0].stored_path)
            assert saved.read_bytes() == content
            assert not saved.is_relative_to(workspace)
            assert kb.get_git_delivery_contract(conn, task_id) is None
        assert kb.board_metadata_path("report-audit").read_bytes() == board_before
        print(json.dumps({
            "module": kb.__file__, "task_id": task_id, "status": "done",
            "workspace_kind": "scratch", "artifact": str(saved),
            "artifact_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
            "git_obligation": None, "board_policy_unchanged": True, "worker_spawned": False,
        }))
    finally:
        if session_tokens:
            clear_session_vars(session_tokens)
        reset_hermes_home_override(home_token)
