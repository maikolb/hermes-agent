import asyncio
from types import SimpleNamespace

from gateway.run import (
    GatewayRunner,
    _append_principal_worker_activity,
    _append_worker_rotation_summary,
    _resolve_activity_indicator_settings,
    _render_activity_indicator_template,
    _upsert_activity_indicator_message,
)


class _FakeAdapter:
    def __init__(self, *, edit_results=None, send_results=None):
        self.edit_results = list(edit_results or [])
        self.send_results = list(send_results or [])
        self.edits = []
        self.sends = []

    async def edit_message(self, chat_id, message_id, content):
        self.edits.append((chat_id, message_id, content))
        result = self.edit_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def send(self, chat_id, content, *, metadata=None):
        self.sends.append((chat_id, content, metadata))
        return self.send_results.pop(0)


def _result(*, success, message_id=None, retryable=False, error=None):
    return SimpleNamespace(
        success=success,
        message_id=message_id,
        retryable=retryable,
        error=error,
    )


def test_activity_indicator_survives_pre_agent_initialization():
    """A live executor owns the turn before AIAgent construction completes."""
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}

    async def scenario():
        executor_task = asyncio.create_task(asyncio.sleep(30))
        try:
            assert runner._should_emit_long_running_notification(
                "telegram:-1001:77",
                None,
                executor_task,
            )
        finally:
            executor_task.cancel()
            try:
                await executor_task
            except asyncio.CancelledError:
                pass
        assert not runner._should_emit_long_running_notification(
            "telegram:-1001:77",
            None,
            executor_task,
        )

    asyncio.run(scenario())


def test_activity_indicator_defaults_preserve_existing_cadence():
    settings = _resolve_activity_indicator_settings({}, "telegram", 180)

    assert settings.initial_delay_seconds == 180
    assert settings.update_interval_seconds == 180
    assert settings.initial_text is None
    assert settings.elapsed_text is None
    assert settings.worker_rotation is False


def test_worker_rotation_resolves_global_platform_and_legacy_fallbacks():
    config = {
        "display": {
            "worker_rotation": "true",
            "platforms": {"telegram": {"worker_rotation": "off"}},
        },
        "kanban": {"worker_focus_handoff": True},
    }

    assert (
        _resolve_activity_indicator_settings(config, "telegram", 180).worker_rotation
        is False
    )
    assert (
        _resolve_activity_indicator_settings(config, "discord", 180).worker_rotation
        is True
    )
    assert _resolve_activity_indicator_settings(
        {"kanban": {"worker_focus_handoff": True}},
        "telegram",
        180,
    ).worker_rotation is True
    assert _resolve_activity_indicator_settings(
        {
            "display": {"worker_rotation": False},
            "kanban": {"worker_focus_handoff": True},
        },
        "telegram",
        180,
    ).worker_rotation is False


def test_principal_activity_aggregates_only_same_project_workers():
    runner = object.__new__(GatewayRunner)
    runner._active_profile_name = lambda: "hermes-project-factory"
    destination = (
        "project-factory",
        "telegram",
        "-1001",
        "77",
        "hermes-project-factory",
    )

    def row(task_id, *, project="project-a", session="session-a"):
        return {
            "task": SimpleNamespace(
                id=task_id,
                project_id=project,
                session_id=session,
            )
        }

    runner._kanban_worker_focus_active = {
        destination: {
            "t_oldest": row("t_oldest"),
            "t_next": row("t_next"),
            "t_other_project": row("t_other_project", project="project-b"),
        },
        (
            "other-board",
            "telegram",
            "-1001",
            "77",
            "hermes-project-factory",
        ): {"t_other_board": row("t_other_board")},
    }
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="-1001",
        thread_id="77",
        profile=None,
    )

    rendered = _append_principal_worker_activity(
        runner,
        source,
        "⏳ Trabalhando há 12 min",
        board="project-factory",
        project_id="project-a",
        session_id="session-a",
    )

    assert rendered == "⏳ Trabalhando há 12 min · principal + 2 workers"
    assert _append_worker_rotation_summary("unchanged", 0) == "unchanged"


def test_activity_indicator_platform_config_overrides_global_config():
    config = {
        "display": {
            "activity_indicator": {
                "initial_delay_seconds": 20,
                "update_interval_seconds": 120,
                "elapsed_text": "Global {elapsed_human}",
            },
            "platforms": {
                "telegram": {
                    "activity_indicator": {
                        "initial_delay_seconds": 10,
                        "update_interval_seconds": 60,
                        "initial_text": "⏳ Trabalhando…",
                        "elapsed_text": "⏳ Trabalhando há {elapsed_human}…",
                    }
                }
            },
        }
    }

    settings = _resolve_activity_indicator_settings(config, "telegram", 180)

    assert settings.initial_delay_seconds == 10
    assert settings.update_interval_seconds == 60
    assert settings.initial_text == "⏳ Trabalhando…"
    assert settings.elapsed_text == "⏳ Trabalhando há {elapsed_human}…"


def test_activity_indicator_invalid_values_fall_back_safely():
    config = {
        "display": {
            "platforms": {
                "telegram": {
                    "activity_indicator": {
                        "initial_delay_seconds": -1,
                        "update_interval_seconds": "not-a-number",
                        "initial_text": 123,
                        "elapsed_text": [],
                    }
                }
            }
        }
    }

    settings = _resolve_activity_indicator_settings(config, "telegram", 180)

    assert settings.initial_delay_seconds == 180
    assert settings.update_interval_seconds == 180
    assert settings.initial_text is None
    assert settings.elapsed_text is None


def test_activity_indicator_templates_render_initial_and_elapsed_text():
    settings = _resolve_activity_indicator_settings(
        {
            "display": {
                "platforms": {
                    "telegram": {
                        "activity_indicator": {
                            "initial_delay_seconds": 10,
                            "update_interval_seconds": 60,
                            "initial_text": "⏳ Trabalhando…",
                            "elapsed_text": "⏳ Trabalhando há {elapsed_human}…",
                        }
                    }
                }
            }
        },
        "telegram",
        180,
    )

    assert (
        _render_activity_indicator_template(
            settings,
            first_update=True,
            elapsed_seconds=10,
        )
        == "⏳ Trabalhando…"
    )
    assert (
        _render_activity_indicator_template(
            settings,
            first_update=False,
            elapsed_seconds=130,
        )
        == "⏳ Trabalhando há 2 min…"
    )


def test_activity_indicator_unknown_placeholder_falls_back_to_default_text():
    settings = _resolve_activity_indicator_settings(
        {
            "display": {
                "platforms": {
                    "telegram": {
                        "activity_indicator": {
                            "elapsed_text": "Bad {unknown_placeholder}",
                        }
                    }
                }
            }
        },
        "telegram",
        180,
    )

    assert (
        _render_activity_indicator_template(
            settings,
            first_update=False,
            elapsed_seconds=60,
        )
        is None
    )


def test_activity_indicator_first_send_then_edit_same_message():
    adapter = _FakeAdapter(
        edit_results=[_result(success=True, message_id="101")],
        send_results=[_result(success=True, message_id="101")],
    )

    async def scenario():
        owned, created = await _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id=None,
            content="⏳ Trabalhando…",
            metadata={"thread_id": "77"},
        )
        assert (owned, created) == ("101", "101")

        owned, created = await _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id=owned,
            content="⏳ Trabalhando há 1 min…",
            metadata={"thread_id": "77"},
        )
        assert (owned, created) == ("101", None)

    asyncio.run(scenario())

    assert len(adapter.sends) == 1
    assert adapter.edits == [
        ("-1001", "101", "⏳ Trabalhando há 1 min…")
    ]


def test_activity_indicator_retryable_edit_failure_does_not_send_duplicate():
    adapter = _FakeAdapter(
        edit_results=[
            _result(
                success=False,
                message_id="101",
                retryable=True,
                error="temporary network error",
            )
        ],
        send_results=[_result(success=True, message_id="202")],
    )

    owned, created = asyncio.run(
        _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id="101",
            content="⏳ Trabalhando há 2 min…",
            metadata={"thread_id": "77"},
        )
    )

    assert (owned, created) == ("101", None)
    assert adapter.sends == []


def test_activity_indicator_edit_exception_does_not_send_duplicate():
    adapter = _FakeAdapter(
        edit_results=[TimeoutError("temporary timeout")],
        send_results=[_result(success=True, message_id="202")],
    )

    owned, created = asyncio.run(
        _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id="101",
            content="⏳ Trabalhando há 2 min…",
            metadata={"thread_id": "77"},
        )
    )

    assert (owned, created) == ("101", None)
    assert adapter.sends == []


def test_activity_indicator_permanent_edit_failure_transfers_ownership():
    adapter = _FakeAdapter(
        edit_results=[
            _result(
                success=False,
                message_id="101",
                retryable=False,
                error="editing not supported",
            )
        ],
        send_results=[_result(success=True, message_id="202")],
    )

    owned, created = asyncio.run(
        _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id="101",
            content="⏳ Trabalhando há 2 min…",
            metadata={"thread_id": "77"},
        )
    )

    assert (owned, created) == ("202", "202")
    assert len(adapter.sends) == 1


def test_activity_indicator_three_topics_keep_independent_message_ids():
    adapter = _FakeAdapter(
        edit_results=[
            _result(success=True, message_id="101"),
            _result(success=True, message_id="202"),
            _result(success=True, message_id="303"),
        ]
    )

    async def scenario():
        return await asyncio.gather(
            *(
                _upsert_activity_indicator_message(
                    adapter,
                    chat_id="-1001",
                    message_id=message_id,
                    content=f"topic {thread_id}",
                    metadata={"thread_id": thread_id},
                )
                for message_id, thread_id in (
                    ("101", "11"),
                    ("202", "22"),
                    ("303", "33"),
                )
            )
        )

    assert asyncio.run(scenario()) == [
        ("101", None),
        ("202", None),
        ("303", None),
    ]
    assert adapter.sends == []
    assert {message_id for _, message_id, _ in adapter.edits} == {
        "101",
        "202",
        "303",
    }


def test_activity_indicator_falls_back_to_new_message_after_edit_failure_streak():
    """A network flap must not mute the display forever (27/08 incident)."""
    from gateway.run import _ACTIVITY_EDIT_FAIL_STREAKS

    _ACTIVITY_EDIT_FAIL_STREAKS.clear()
    adapter = _FakeAdapter(
        edit_results=[
            TimeoutError("flap 1"),
            TimeoutError("flap 2"),
            TimeoutError("flap 3"),
        ],
        send_results=[_result(success=True, message_id="909")],
    )

    async def scenario():
        owned = "101"
        for _ in range(2):
            owned, created = await _upsert_activity_indicator_message(
                adapter,
                chat_id="-1001",
                message_id=owned,
                content="⏳ Trabalhando…",
                metadata=None,
            )
            assert (owned, created) == ("101", None)
        owned, created = await _upsert_activity_indicator_message(
            adapter,
            chat_id="-1001",
            message_id=owned,
            content="⏳ Trabalhando…",
            metadata=None,
        )
        assert (owned, created) == ("909", "909")
        assert len(adapter.sends) == 1

    asyncio.run(scenario())


def test_activity_indicator_edit_success_resets_failure_streak():
    from gateway.run import _ACTIVITY_EDIT_FAIL_STREAKS

    _ACTIVITY_EDIT_FAIL_STREAKS.clear()
    adapter = _FakeAdapter(
        edit_results=[
            TimeoutError("flap 1"),
            TimeoutError("flap 2"),
            _result(success=True),
            TimeoutError("flap 3"),
        ],
    )

    async def scenario():
        for expected_created in (None, None, None, None):
            owned, created = await _upsert_activity_indicator_message(
                adapter,
                chat_id="-1001",
                message_id="101",
                content="⏳ Trabalhando…",
                metadata=None,
            )
            assert (owned, created) == ("101", expected_created)
        assert not adapter.sends

    asyncio.run(scenario())
