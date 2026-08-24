import asyncio

from gateway.session_context import (
    clear_project_topic_creator,
    clear_session_vars,
    get_project_context,
    get_project_topic_creator,
    get_session_env,
    reset_session_vars,
    set_project_topic_creator,
    set_session_vars,
)


PROJECT_VARS = (
    "HERMES_PROJECT_ID",
    "HERMES_PROJECT_BOARD",
    "HERMES_PROJECT_WORKDIR",
    "HERMES_PROJECT_ACCESS",
)


def test_project_vars_set_get_clear_and_existing_call_compatibility(monkeypatch):
    for name in PROJECT_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_session_vars()

    legacy_tokens = set_session_vars(platform="telegram", chat_id="123")
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_project_context() is None
    clear_session_vars(legacy_tokens)

    tokens = set_session_vars(
        project_id="project-1",
        project_board="alpha",
        project_workdir="C:/work/alpha",
        project_access="write",
    )
    assert get_project_context() == {
        "project_id": "project-1",
        "board": "alpha",
        "workdir": "C:/work/alpha",
        "access": "write",
    }
    clear_session_vars(tokens)
    assert get_project_context() is None
    assert all(get_session_env(name) == "" for name in PROJECT_VARS)


def test_project_vars_do_not_leak_between_asyncio_contexts(monkeypatch):
    for name in PROJECT_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_session_vars()

    async def bind(project_id, board):
        tokens = set_session_vars(project_id=project_id, project_board=board)
        await asyncio.sleep(0)
        result = get_project_context()
        clear_session_vars(tokens)
        return result

    async def run():
        return await asyncio.gather(bind("project-a", "alpha"), bind("project-b", "beta"))

    assert asyncio.run(run()) == [
        {"project_id": "project-a", "board": "alpha", "workdir": "", "access": ""},
        {"project_id": "project-b", "board": "beta", "workdir": "", "access": ""},
    ]


def test_subprocess_bridge_does_not_leak_between_engaged_contexts(monkeypatch):
    from tools.environments.local import _inject_session_context_env

    monkeypatch.setenv("HERMES_PROJECT_BOARD", "stale-board")

    async def bridged(board):
        tokens = set_session_vars(project_board=board)
        await asyncio.sleep(0)
        env = {"HERMES_PROJECT_BOARD": "stale-board"}
        _inject_session_context_env(env)
        clear_session_vars(tokens)
        return env["HERMES_PROJECT_BOARD"]

    async def run():
        return await asyncio.gather(bridged("alpha"), bridged("beta"))

    assert asyncio.run(run()) == ["alpha", "beta"]

    reset_session_vars()
    unbound_env = {"HERMES_PROJECT_BOARD": "stale-board"}
    _inject_session_context_env(unbound_env)
    assert "HERMES_PROJECT_BOARD" not in unbound_env


def test_project_topic_creator_is_task_local_and_clearable():
    async def bind(label):
        async def creator(**kwargs):
            return {"label": label}

        token = set_project_topic_creator(creator)
        await asyncio.sleep(0)
        assert await get_project_topic_creator()() == {"label": label}
        clear_project_topic_creator(token)
        return get_project_topic_creator()

    async def run():
        return await asyncio.gather(bind("first"), bind("second"))

    assert asyncio.run(run()) == [None, None]
    assert get_project_topic_creator() is None


def test_clear_session_vars_removes_project_topic_creator():
    async def creator(**kwargs):
        return kwargs

    set_project_topic_creator(creator)
    tokens = set_session_vars(platform="telegram")
    clear_session_vars(tokens)
    assert get_project_topic_creator() is None
