"""Real create/readback regression for configured executable-task ownership."""


def test_create_ready_task_resolves_configured_default(tmp_path, monkeypatch):
    import json

    test_home = tmp_path / "home"
    hermes_home = test_home / ".hermes"
    executor_home = hermes_home / "profiles" / "executor"
    executor_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "kanban:\n  default_assignee: executor\n", encoding="utf-8",
    )
    (executor_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name, value in {
        "HOME": test_home, "USERPROFILE": test_home,
        "LOCALAPPDATA": test_home / "AppData" / "Local",
        "HERMES_HOME": hermes_home, "HERMES_KANBAN_HOME": hermes_home / "kanban",
    }.items():
        monkeypatch.setenv(name, str(value))

    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import profile_exists

    token = set_hermes_home_override(hermes_home)
    try:
        assert profile_exists("executor")
        db_path = hermes_home / "assignee-regression.db"
        with kb.connect_closing(db_path=db_path) as conn:
            task_id = kb.create_task(conn, title="Configured executor", assignee=None)
        with kb.connect_closing(db_path=db_path) as conn:
            task = kb.get_task(conn, task_id)
            assert task.status == "ready"
            assert task.assignee == "executor"
            print(json.dumps({
                "module": kb.__file__, "task_id": task.id,
                "status": task.status, "assignee": task.assignee,
                "db_path": str(db_path), "worker_spawned": False,
            }))
    finally:
        reset_hermes_home_override(token)
